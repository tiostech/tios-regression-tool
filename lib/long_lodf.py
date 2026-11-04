"""EnergyCore's "Long-LODF" search: which monitored elements an outage loads, from the
PowerWorld LODFs, with the RT shadow prices those elements' constraints had while the outage
was out. A port of tios-core ``OutageEffectAnalysisV2`` as the outage pages call it
(``outage_effects#v3``, ``transmission_outages: true``).

Used by ``mcp_server.py``. Nothing here imports Streamlit.

How EnergyCore runs it (and what this module reproduces):

- **Monitored elements**: every element with a constraint active (a row in the DA or RT shadow
  prices or the RT shadow forecasts) between ``the_current_date - lodf_link_lookback_days``
  (market parameter; 6 months when unset) and ``the_current_date``. A constraint without an
  element is matched to one through ``rt_descriptive_name``. EnergyCore lists every active
  constraint in the market and passes their elements to the LODF query; this module runs the
  LODF query for the outage first and then checks only the elements it returns, which gives the
  same rows for a fraction of the cost.
- **LODF rows** (``#lodfs``): ``{market}_pw_lodfs_YYYYMMDD`` joined to
  ``{market}_pw_branches_YYYYMMDD`` twice (monitored and outage branch, by ``branch_id``), to the
  netmap monitored-element and outage-element tables by bus numbers and circuit (``version_dt``
  = the model date, ``defer <> 1``), and to the outage table by equipment ID. The model date is
  the ``{market}_netmap_versions`` row with ``tool_use_default = 1``. In markets with
  ``supports_active_outage_tools`` (MISO) the active outages are added with a UNION.
  delta = sign x ROUND(|post - original| x 100 / line limit, 2); max delta = ROUND(sign x
  |outage limit / monitored limit x LODF|, 2); the sign is -1 when the post-outage flow is
  smaller in magnitude than the original. Rows sort by ROUND(delta) DESC, lodf DESC.
- **RT total shadows** (``#filter_by_rt_shadows``, only with ``use_total_rt_shadows``): for
  each row, ``timeseries__constraint_shadows`` over ``the_current_date - 5 years`` to
  ``the_current_date + 1 day``, limited to the hours the outage was out, summed over the
  element's active constraints and their related constraints (``ConstraintRelationTracker``
  default options). "The outage" is the equipment's groups (every non-removed member of each
  group and of its associated groups, scheduled and active outages) when it is grouped, else the
  equipment's scheduled outages (or active, for an active-outage row). Rows below
  ``threshold_min_total_rt_shadows`` are dropped (an empty threshold counts as 0).
"""

import datetime
import time

import numpy as np
import pandas as pd
import yaml
from sqlalchemy import text

from lib import autoflow

# OutageEffectAnalysisV2::LODF_FILTER_KEYS
FILTER_KEYS = ["threshold_min_lodf", "threshold_min_post_outage_flow_pct_of_limit",
               "threshold_min_flow_delta_pct_of_limit", "threshold_min_flow_max_delta_pct_of_limit",
               "threshold_min_total_rt_shadows", "use_total_rt_shadows"]

DEFAULT_LOOKBACK_MONTHS = 6          # outage pages: lodf_link_lookback_days || 6.months
SHADOW_LOOKBACK_YEARS = 5            # OutageEffectAnalysisV2#link_date_window
UNIVERSAL_END = pd.Timestamp("2200-01-01")  # UNIVERSAL_NODECHANGE_STOPDATE: an open-ended outage


def _no_warn(_message):
    pass


def _in(prefix: str, values) -> tuple[str, dict]:
    values = list(values)
    return ", ".join(f":{prefix}{i}" for i in range(len(values))), {f"{prefix}{i}": v for i, v in enumerate(values)}


def _chunks(values, size=1000):
    values = list(values)
    for i in range(0, len(values), size):
        yield values[i:i + size]


_tables = {"loaded": 0.0, "names": set()}


def _table_names(engine) -> set:
    if time.time() - _tables["loaded"] > 3600:
        with engine.connect() as conn:
            _tables["names"] = {r[0] for r in conn.execute(text("SHOW TABLES")).fetchall()}
        _tables["loaded"] = time.time()
    return _tables["names"]


# --- MARKET PARAMETERS ---

def _symbol_keys(value):
    """Ruby YAML writes symbol keys and values as ':name'; strip the colon."""
    if isinstance(value, dict):
        return {str(k).lstrip(":"): _symbol_keys(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_symbol_keys(v) for v in value]
    if isinstance(value, str) and value.startswith(":") and len(value) > 1 and " " not in value:
        return value[1:]
    return value


def market_param(engine, market: str, name: str):
    """A market parameter's value (YAML in market_parameters.value), or None when unset."""
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT p.value FROM market_parameters p JOIN markets m ON m.id = p.market_id
            WHERE m.urn = :m AND p.name = :n ORDER BY p.id LIMIT 1
        """), {"m": market, "n": name}).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        return _symbol_keys(yaml.safe_load(row[0]))
    except yaml.YAMLError:
        return row[0]


def _as_bool(value) -> bool:
    return value in (True, 1, "true", "TRUE", "True", "1")


def _as_float(value):
    """ParamsValsExtractor#extract_float: blank is nil."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return float(value)


def default_filters(engine, market: str) -> dict:
    """The market's saved Long-LODF preset (lodf_default_filters), as the outage pages pass it."""
    saved = market_param(engine, market, "lodf_default_filters") or {}
    if not isinstance(saved, dict):
        saved = {}
    out = {k: _as_float(saved.get(k)) for k in FILTER_KEYS if k != "use_total_rt_shadows"}
    out["use_total_rt_shadows"] = _as_bool(saved.get("use_total_rt_shadows"))
    return out


def lookback_days(engine, market: str) -> int | None:
    value = market_param(engine, market, "lodf_link_lookback_days")
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def current_date(engine, market: str) -> datetime.date:
    """Market#the_current_date: today in the market's time zone."""
    tz = autoflow.market_time_zone(engine, market)
    return datetime.datetime.now(tz).date() if tz else datetime.date.today()


def default_version(engine, market: str) -> datetime.date | None:
    """OutageEffectAnalysisV2#default_version_dt: the netmap version marked tool_use_default."""
    with engine.connect() as conn:
        row = conn.execute(text(f"""
            SELECT version_dt FROM {market}_netmap_versions WHERE tool_use_default = 1 ORDER BY id LIMIT 1
        """)).fetchone()
    return row[0] if row else None


def model_tables_exist(engine, market: str, version: datetime.date) -> bool:
    suffix = version.strftime("%Y%m%d")
    names = _table_names(engine)
    return f"{market}_pw_lodfs_{suffix}" in names and f"{market}_pw_branches_{suffix}" in names


def supports_active_outages(engine, market: str) -> bool:
    return (_as_bool(market_param(engine, market, "supports_active_outage_tools"))
            and f"{market}_active_outages" in _table_names(engine))


# --- REGRESSION IGNORE RULES ---

def regression_exclusion_sql(engine, market: str, table: str, params: dict) -> str | None:
    """``regression_exclusion_arel`` for the regression_ignore_rules market parameter: each
    rule excludes rows whose column equals (or, with %, is LIKE) a value. As in SQL, a NULL
    column fails every rule, so such rows are excluded too."""
    rules = market_param(engine, market, "regression_ignore_rules") or {}
    if not isinstance(rules, dict):
        return None
    parts = []
    for col, val in rules.items():
        column = f"`{table}`.`{col}`"
        if isinstance(val, list):
            if any(isinstance(v, str) and "%" in v for v in val):
                sub = []
                for v in val:
                    key = f"rx{len(params)}"
                    params[key] = v
                    sub.append(f"{column} NOT LIKE :{key}" if isinstance(v, str) and "%" in v else f"{column} != :{key}")
                parts.append("(" + " AND ".join(sub) + ")")
            elif val:
                keys = []
                for v in val:
                    key = f"rx{len(params)}"
                    params[key] = v
                    keys.append(f":{key}")
                parts.append(f"{column} NOT IN ({', '.join(keys)})")
        else:
            key = f"rx{len(params)}"
            params[key] = val
            parts.append(f"{column} NOT LIKE :{key}" if isinstance(val, str) and "%" in val else f"{column} != :{key}")
    return " AND ".join(parts) if parts else None


# --- LODF ROWS ---

def lodf_rows(engine, market: str, version: datetime.date, equipment_ids, request_ids=(),
              threshold_min_lodf=None, threshold_min_post_outage_flow_pct_of_limit=None,
              threshold_min_flow_delta_pct_of_limit=None, threshold_min_flow_max_delta_pct_of_limit=None,
              apply_regression_ignore_rules: bool = False, include_active: bool = False) -> pd.DataFrame:
    """OutageEffectAnalysisV2#lodfs for these outage equipment IDs, without the monitored
    element filter (see ``elements_with_active_constraints``). One row per outage record and
    branch pairing, as EnergyCore returns them."""
    suffix = version.strftime("%Y%m%d")
    lodf_t, br_t = f"{market}_pw_lodfs_{suffix}", f"{market}_pw_branches_{suffix}"
    nm_me, nm_out = f"{market}_netmap_monitored_elements", f"{market}_netmap_outage_elements"

    sign = "IF((ABS(lodf.post_outage_flow_mw) - ABS(lodf.original_flow_mw)) < 0, -1, 1)"
    flow_increase = (f"IF(lodf.line_limit_mw IS NULL, NULL, ROUND(((ABS(lodf.post_outage_flow_mw - lodf.original_flow_mw) * 100) "
                     f"/ lodf.line_limit_mw) * {sign}, 2))")
    flow_increase_max = f"ROUND({sign} * ABS(((o_pwb.line_limit_mw / m_pwb.line_limit_mw) * lodf.lodf)), 2)"

    eq_placeholders, params = _in("e", sorted({int(e) for e in equipment_ids}))
    params["v"] = version

    # EnergyCore joins the outage table into the LODF query. An equipment ID has hundreds of
    # outage records (one per report), which multiplies every LODF row and made PJM searches
    # take 40 seconds, so the LODF rows and the outage records are read separately and joined
    # here. Same inner join, same rows.
    lodf_where = [f"nm_out.outage_equipment_id IN ({eq_placeholders})"]
    if threshold_min_lodf is not None:
        lodf_where.append("ABS(lodf.lodf) >= :t_lodf")
        params["t_lodf"] = threshold_min_lodf
    if threshold_min_post_outage_flow_pct_of_limit is not None:
        lodf_where.append("lodf.post_outage_limit_mw_pct >= :t_post")
        params["t_post"] = threshold_min_post_outage_flow_pct_of_limit
    if threshold_min_flow_delta_pct_of_limit is not None:
        lodf_where.append(f"(lodf.line_limit_mw IS NOT NULL AND ABS({flow_increase}) >= :t_delta)")
        params["t_delta"] = threshold_min_flow_delta_pct_of_limit
    if threshold_min_flow_max_delta_pct_of_limit is not None:
        lodf_where.append(f"ABS({flow_increase_max}) >= :t_maxdelta")
        params["t_maxdelta"] = threshold_min_flow_max_delta_pct_of_limit
    lodf_sql = f"""
        SELECT nm_out.outage_equipment_id,
               lodf.lodf, lodf.original_flow_mw, lodf.post_outage_flow_mw, lodf.line_limit_mw,
               lodf.post_outage_limit_mw_pct, m_pwb.short_name AS mon_br_shortname,
               o_pwb.short_name AS out_br_shortname, nm_me.official_monitored_element_id,
               m_pwb.line_limit_mw AS monelem_line_limit_mw, o_pwb.line_limit_mw AS outage_line_limit_mw,
               {flow_increase} AS flow_increase_pct_of_limit,
               {flow_increase_max} AS flow_increase_max_pct_of_limit
        FROM `{lodf_t}` lodf
        INNER JOIN `{br_t}` m_pwb ON lodf.monitored_branch_id = m_pwb.branch_id
        INNER JOIN `{nm_me}` nm_me ON nm_me.pw_from_bus_num = m_pwb.bus1_num AND nm_me.pw_to_bus_num = m_pwb.bus2_num
               AND nm_me.pw_line_circuit = m_pwb.line_circuit AND nm_me.version_dt = :v AND nm_me.`defer` != 1
        INNER JOIN `{br_t}` o_pwb ON lodf.outage_branch_id = o_pwb.branch_id
        INNER JOIN `{nm_out}` nm_out ON nm_out.pw_from_bus_num = o_pwb.bus1_num AND nm_out.pw_to_bus_num = o_pwb.bus2_num
               AND nm_out.pw_line_circuit = o_pwb.line_circuit AND nm_out.version_dt = :v AND nm_out.`defer` != 1
        WHERE {" AND ".join(lodf_where)}
    """

    outage_frames = []
    modes = ["sched"] + (["active"] if include_active else [])
    with engine.connect() as conn:
        lodfs = pd.read_sql(text(lodf_sql), conn, params=params)
        for mode in modes:
            outage_t = f"{market}_transmission_outages" if mode == "sched" else f"{market}_active_outages"
            o_params = {k: v for k, v in params.items() if k.startswith("e")}
            where = [f"`{outage_t}`.outage_equipment_id IN ({eq_placeholders})"]
            if mode == "sched" and apply_regression_ignore_rules:
                rx = regression_exclusion_sql(engine, market, outage_t, o_params)
                if rx:
                    where.append(f"({rx})")
            if request_ids:
                rq, rq_params = _in(f"r{mode}", request_ids)
                o_params.update(rq_params)
                where.append(f"`{outage_t}`.outage_request_id IN ({rq})")
            sched = mode == "sched"
            outage_frames.append(pd.read_sql(text(f"""
                SELECT `{outage_t}`.outage_equipment_id, `{outage_t}`.outage_request_id,
                       `{outage_t}`.outage_equipment_identifier,
                       {f"`{outage_t}`.request_status" if sched else "NULL"} AS request_status,
                       `{outage_t}`.actual_start, `{outage_t}`.actual_end,
                       {f"`{outage_t}`.planned_start" if sched else "NULL"} AS planned_start,
                       {f"`{outage_t}`.planned_end" if sched else "NULL"} AS planned_end,
                       '{mode}' AS outage_source
                FROM `{outage_t}`
                WHERE {" AND ".join(where)}
            """), conn, params=o_params))
    nonempty = [f for f in outage_frames if not f.empty]
    outages = pd.concat(nonempty, ignore_index=True) if len(nonempty) > 1 else (nonempty or outage_frames)[0]
    df = outages.merge(lodfs, on="outage_equipment_id", how="inner")
    if len(outage_frames) > 1:
        df = df.drop_duplicates()  # EnergyCore UNIONs the scheduled and active queries, which drops identical rows.
    cols = ["outage_equipment_id", "outage_request_id", "outage_equipment_identifier", "request_status", "actual_start",
            "actual_end", "planned_start", "planned_end"] + [c for c in lodfs.columns if c != "outage_equipment_id"] \
        + ["outage_source"]
    df = df[cols]
    # EnergyCore's order: ROUND(flow_increase_pct_of_limit) DESC, lodf DESC, outage_source DESC
    # (MySQL rounds half away from zero, NULLs last in DESC). Element ID makes ties repeatable.
    fi = df["flow_increase_pct_of_limit"].astype(float)
    df = df.assign(_r=np.sign(fi) * np.floor(fi.abs() + 0.5))
    df = df.sort_values(["_r", "lodf", "outage_source", "official_monitored_element_id"],
                        ascending=[False, False, False, True], na_position="last", kind="mergesort")
    df = df.drop(columns="_r").reset_index(drop=True)
    for c in ("outage_equipment_id", "official_monitored_element_id"):
        df[c] = df[c].astype("Int64")
    return df


def mapped_equipment(engine, market: str, version: datetime.date, equipment_ids) -> set[int]:
    """Equipment IDs with a usable netmap row (defer <> 1) for this model version."""
    ids = sorted({int(e) for e in equipment_ids})
    found = set()
    for chunk in _chunks(ids):
        ph, params = _in("e", chunk)
        params["v"] = version
        with engine.connect() as conn:
            found |= {int(r[0]) for r in conn.execute(text(f"""
                SELECT DISTINCT outage_equipment_id FROM {market}_netmap_outage_elements
                WHERE version_dt = :v AND outage_equipment_id IN ({ph}) AND `defer` != 1
            """), params).fetchall()}
    return found


# --- MONITORED ELEMENTS AND THEIR CONSTRAINTS ---

def _shadow_tables(market: str) -> list[str]:
    return [f"{market}_da_constraint_shadow_prices", f"{market}_rt_constraint_shadow_prices",
            f"{market}_rt_constraint_shadow_price_forecasts"]


def _constraints_for_elements(engine, market: str, me_ids) -> pd.DataFrame:
    """Constraints that resolve to these elements: their own (official_monitored_element_id) and
    element-less constraints whose rt_descriptive_name matches one of those (EnergyCore's
    RT-name fallback). Columns: constraint_id, me_id."""
    frames = []
    for chunk in _chunks(sorted({int(m) for m in me_ids})):
        ph, params = _in("m", chunk)
        with engine.connect() as conn:
            frames.append(pd.read_sql(text(f"""
                SELECT id AS constraint_id, official_monitored_element_id AS me_id
                FROM {market}_official_constraints WHERE official_monitored_element_id IN ({ph})
                UNION
                SELECT c_no.id, c_with.official_monitored_element_id
                FROM {market}_official_constraints c_with
                INNER JOIN {market}_official_constraints c_no
                    ON c_no.rt_descriptive_name = c_with.rt_descriptive_name AND c_no.official_monitored_element_id IS NULL
                WHERE c_with.official_monitored_element_id IN ({ph}) AND c_with.rt_descriptive_name IS NOT NULL
            """), conn, params=params))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["constraint_id", "me_id"])


def _active_constraint_ids(engine, market: str, constraint_ids, start: datetime.date, stop: datetime.date) -> set[int]:
    """Which of these constraints have a DA or RT shadow price or an RT shadow forecast between
    start and stop (``active_constraint_ids_for_date_range``)."""
    active = set()
    ids = sorted({int(c) for c in constraint_ids})
    names = _table_names(engine)
    for table in _shadow_tables(market):
        if table not in names:
            continue
        for chunk in _chunks(ids, 500):
            ph, params = _in("c", chunk)
            params.update(s=start, e=stop)
            with engine.connect() as conn:
                active |= {int(r[0]) for r in conn.execute(text(f"""
                    SELECT DISTINCT official_constraint_id FROM {table}
                    WHERE official_constraint_id IN ({ph}) AND dt BETWEEN :s AND :e
                """), params).fetchall()}
    return active


def elements_with_active_constraints(engine, market: str, me_ids, start: datetime.date, stop: datetime.date) -> set[int]:
    """The subset of these elements EnergyCore would analyze: those an active constraint in the
    window resolves to (``active_constraints_for_date_range_as_monitored_elements``)."""
    cons = _constraints_for_elements(engine, market, me_ids)
    if cons.empty:
        return set()
    active = _active_constraint_ids(engine, market, cons["constraint_id"], start, stop)
    return {int(m) for m in cons[cons["constraint_id"].isin(active)]["me_id"]}


def constraints_by_element(engine, market: str, me_ids, start: datetime.date, stop: datetime.date) -> dict[int, list[int]]:
    """``active_constraint_ids_in_date_range_for_monitored_element_ids``: per element, the
    constraints with shadow rows (DA, RT, RT forecast) in the window that name the element, plus
    element-less constraints matched by RT name. Looked up through the element's constraints
    (index on constraint and date) instead of scanning the window."""
    cons = _constraints_for_elements(engine, market, me_ids)
    out: dict[int, list[int]] = {}
    if cons.empty:
        return out
    wanted = {int(m) for m in me_ids}
    own = set(cons[cons["me_id"].isin(wanted)]["constraint_id"].astype(int))
    names = _table_names(engine)
    pairs = set()
    for table in _shadow_tables(market):
        if table not in names:
            continue
        for chunk in _chunks(sorted(own), 500):
            ph, params = _in("c", chunk)
            params.update(s=start, e=stop)
            with engine.connect() as conn:
                rows = conn.execute(text(f"""
                    SELECT DISTINCT official_monitored_element_id, official_constraint_id FROM {table}
                    WHERE official_constraint_id IN ({ph}) AND dt BETWEEN :s AND :e
                """), params).fetchall()
            for me, c in rows:
                if me is not None and int(me) in wanted:
                    pairs.add((int(me), int(c)))
                elif me is None:
                    # An element-less shadow row counts for every element its constraint's RT name matches.
                    for m in cons[cons["constraint_id"] == int(c)]["me_id"]:
                        if int(m) in wanted:
                            pairs.add((int(m), int(c)))
    for me, c in sorted(pairs):
        out.setdefault(me, []).append(c)
    return out


def backup_constraint(engine, market: str, me_id: int) -> int | None:
    with engine.connect() as conn:
        row = conn.execute(text(f"""
            SELECT id FROM {market}_official_constraints WHERE official_monitored_element_id = :m ORDER BY id LIMIT 1
        """), {"m": int(me_id)}).fetchone()
    return int(row[0]) if row else None


def constraint_names(engine, market: str, constraint_ids) -> dict[int, str]:
    ids = sorted({int(c) for c in constraint_ids})
    out = {}
    for chunk in _chunks(ids):
        ph, params = _in("c", chunk)
        with engine.connect() as conn:
            for cid, rt, da, me_name, ctg in conn.execute(text(f"""
                SELECT id, rt_descriptive_name, da_descriptive_name, monitored_element_name, contingency_element_name
                FROM {market}_official_constraints WHERE id IN ({ph})
            """), params).fetchall():
                name = rt or da or " / ".join(x for x in (me_name, ctg) if x)
                out[int(cid)] = name or ""
    return out


# --- RELATED CONSTRAINTS (ConstraintRelationTracker default options) ---

def relation_options(engine, market: str) -> dict:
    opts = {"family": _as_bool(market_param(engine, market, "include_family_constraints_by_default")),
            "monelem": True, "rtname": True, "temprtname": True}
    if market == "miso":
        opts["rtname"] = False
    return opts


_family_cache = {}
# Constraint families are edited by the desk; re-read them after this long.
FAMILY_CACHE_SECONDS = 10 * 60


def _family_relatives(engine, market: str) -> dict[int, set[int]]:
    hit = _family_cache.get(market)
    if hit and time.time() - hit[0] < FAMILY_CACHE_SECONDS:
        return hit[1]
    rel: dict[int, set[int]] = {}
    if f"{market}_constraint_family_members" in _table_names(engine):
        with engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT m.constraint_family_id, m.official_constraint_id FROM {market}_constraint_family_members m
                INNER JOIN {market}_constraint_families f ON f.id = m.constraint_family_id
            """)).fetchall()
        fams: dict[int, set[int]] = {}
        for fid, cid in rows:
            fams.setdefault(int(fid), set()).add(int(cid))
        for members in fams.values():
            for c in members:
                rel.setdefault(c, set()).update(members - {c})
    _family_cache[market] = (time.time(), rel)
    return rel


class RelationGraph:
    """``ConstraintRelationTracker#find_relationships!`` with the default options, for many
    starting sets at once. Its breadth-first search reaches (``solved_constraint_ids_all``) every
    existing constraint linked by: a constraint family (when include_family_constraints_by_default),
    the same monitored element, the same RT name among non-deleted constraints (not in MISO), and
    for an RT name, the non-deleted temporary constraints without an element. The links are
    looked up in batches and cached, then each starting set's closure is walked in memory."""

    def __init__(self, engine, market: str):
        self.engine, self.market = engine, market
        self.opts = relation_options(engine, market)
        self.family = _family_relatives(engine, market) if self.opts["family"] else {}
        self.info: dict[int, tuple] = {}       # constraint -> (me, rt name); absent = does not exist
        self.missing: set[int] = set()
        self.by_me: dict[int, set[int]] = {}
        self.by_rt: dict[str, set[int]] = {}
        self.by_temprt: dict[str, set[int]] = {}

    def _load_info(self, ids) -> None:
        todo = sorted({int(i) for i in ids} - set(self.info) - self.missing)
        for chunk in _chunks(todo):
            ph, params = _in("c", chunk)
            with self.engine.connect() as conn:
                rows = conn.execute(text(f"""
                    SELECT id, official_monitored_element_id, rt_descriptive_name FROM {self.market}_official_constraints
                    WHERE id IN ({ph})
                """), params).fetchall()
            for cid, me, rt in rows:
                self.info[int(cid)] = (int(me) if me is not None else None, rt or None)
            self.missing |= set(chunk) - {int(r[0]) for r in rows}

    def _load_links(self, ids) -> None:
        mes = {self.info[i][0] for i in ids if i in self.info and self.info[i][0] is not None} - set(self.by_me)
        rts = {self.info[i][1] for i in ids if i in self.info and self.info[i][1]}
        lookups = []
        if self.opts["monelem"] and mes:
            lookups.append(("me", sorted(mes), f"SELECT official_monitored_element_id, id FROM {self.market}_official_constraints "
                                              f"WHERE official_monitored_element_id IN ({{ph}})", self.by_me))
        if self.opts["rtname"] and rts - set(self.by_rt):
            lookups.append(("rt", sorted(rts - set(self.by_rt)), f"SELECT rt_descriptive_name, id FROM {self.market}_official_constraints "
                                                                 f"WHERE rt_descriptive_name IN ({{ph}}) AND deleted_at IS NULL", self.by_rt))
        if self.opts["temprtname"] and rts - set(self.by_temprt):
            lookups.append(("tr", sorted(rts - set(self.by_temprt)),
                            f"SELECT rt_descriptive_name, id FROM {self.market}_official_constraints WHERE rt_descriptive_name IN ({{ph}}) "
                            f"AND deleted_at IS NULL AND official_monitored_element_id IS NULL AND `temporary` = 1", self.by_temprt))
        for prefix, keys, sql, target in lookups:
            for k in keys:
                target.setdefault(k, set())
            for chunk in _chunks(keys, 500):
                ph, params = _in(prefix, chunk)
                with self.engine.connect() as conn:
                    for k, cid in conn.execute(text(sql.format(ph=ph)), params).fetchall():
                        target.setdefault(int(k) if prefix == "me" else k, set()).add(int(cid))

    def _neighbors(self, cid: int) -> set[int]:
        me, rt = self.info[cid]
        out = set(self.family.get(cid, ()))
        if self.opts["monelem"] and me is not None:
            out |= self.by_me.get(me, set())
        if rt and self.opts["rtname"]:
            out |= self.by_rt.get(rt, set())
        if rt and self.opts["temprtname"]:
            out |= self.by_temprt.get(rt, set())
        out.discard(cid)
        return out

    def expand(self, seeds) -> None:
        """Loads every link reachable from the seeds, one batch per breadth-first level."""
        seen = set()
        frontier = {int(s) for s in seeds}
        while frontier:
            self._load_info(frontier)
            live = [c for c in frontier if c in self.info]
            self._load_links(live)
            seen |= frontier
            nxt = set()
            for c in live:
                nxt |= self._neighbors(c)
            frontier = nxt - seen

    def closure(self, seeds) -> list[int]:
        self.expand(seeds)
        solved, stack = set(), [int(s) for s in seeds]
        while stack:
            c = stack.pop()
            if c in solved or c not in self.info:
                continue
            solved.add(c)
            stack.extend(self._neighbors(c) - solved)
        return sorted(solved)


# --- OUTAGE HOURS ---

def group_members_for_shadows(engine, market: str, group_id) -> list[int]:
    """OutageEquipmentCombination#extract_group_members for a ':comb:' group: the group's
    non-removed members plus its associated groups' non-removed members."""
    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT outage_equipment_id FROM {market}_outage_equipment_group_members
            WHERE outage_equipment_group_id = :g AND (removed = 0 OR removed IS NULL)
            UNION
            SELECT m.outage_equipment_id FROM {market}_outage_group_associations a
            INNER JOIN {market}_outage_equipment_group_members m ON m.outage_equipment_group_id = a.associated_group_id
            WHERE a.group_id = :g AND (m.removed = 0 OR m.removed IS NULL)
        """), {"g": int(group_id)}).fetchall()
    return sorted({int(r[0]) for r in rows if r[0] is not None})


def groups_for_equipment(engine, market: str, equipment_ids) -> dict[int, list[int]]:
    """``groups_for_equipment_ids``: the groups each equipment is a (removed = 0) member of."""
    out: dict[int, list[int]] = {}
    ids = sorted({int(e) for e in equipment_ids})
    if not ids or f"{market}_outage_equipment_group_members" not in _table_names(engine):
        return out
    for chunk in _chunks(ids):
        ph, params = _in("e", chunk)
        with engine.connect() as conn:
            for e, g in conn.execute(text(f"""
                SELECT m.outage_equipment_id, m.outage_equipment_group_id
                FROM {market}_outage_equipment_group_members m
                INNER JOIN {market}_outage_equipment_groups g ON g.id = m.outage_equipment_group_id
                WHERE m.outage_equipment_id IN ({ph}) AND m.removed = 0
                ORDER BY m.outage_equipment_group_id
            """), params).fetchall():
                out.setdefault(int(e), []).append(int(g))
    return out


def _to_hour_ranges(df: pd.DataFrame) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """``to_date_range`` then DateHour.from: [actual_start, actual_end - 1 minute if on the hour]
    (open-ended to 2200), both floored to the hour. Each hour h in a range is hour-ending h+1."""
    out = []
    for start, end in zip(df["actual_start"], df["actual_end"]):
        if start is None or pd.isna(start):
            continue
        start = pd.Timestamp(start)
        if end is None or pd.isna(end):
            end = UNIVERSAL_END
        else:
            end = pd.Timestamp(end)
            if end == end.floor("h"):
                end -= pd.Timedelta(minutes=1)
        out.append((start.floor("h"), end.floor("h")))
    return out


def _max_last_update(engine, market: str) -> datetime.datetime | None:
    with engine.connect() as conn:
        row = conn.execute(text(f"SELECT MAX(last_update_for_date) FROM {market}_transmission_outages")).fetchone()
    return row[0] if row else None


def transmission_outage_hours(engine, market: str, equipment_ids, stale_type: str, stale_days: int,
                              dropped_offset: int, relative_dt: datetime.date) -> list:
    """Hours the equipment's transmission outages were out (``get_rangelists_by_outage_id`` with
    its defaults: started outages only, the regression ignore rules applied, a stale open outage
    ended at its planned end or, for dropped_outage staleness, when it left the file)."""
    ids = sorted({int(e) for e in equipment_ids})
    if not ids:
        return []
    table = f"{market}_transmission_outages"
    cutoff = datetime.datetime.combine(relative_dt, datetime.time()) - datetime.timedelta(days=stale_days)
    params = {}
    if stale_type == "dropped_outage":
        max_lufd = _max_last_update(engine, market)
        stale = "last_update_for_date < :maxlufd" if max_lufd else "1=0"
        params["maxlufd"] = max_lufd
        stale_end = f"DATE_ADD(last_update_for_date, INTERVAL {int(dropped_offset)} SECOND)"
    elif stale_type == "planned_end":
        stale, stale_end = "planned_end < :cutoff", "planned_end"
        params["cutoff"] = cutoff
    else:
        stale, stale_end = "last_update_for_date < :cutoff", "planned_end"
        params["cutoff"] = cutoff
    where = ["actual_start IS NOT NULL", "COALESCE(actual_end, planned_end) > actual_start"]
    rx = regression_exclusion_sql(engine, market, table, params)
    if rx:
        where.append(f"({rx})")
    frames = []
    for chunk in _chunks(ids):
        ph, p = _in("e", chunk)
        with engine.connect() as conn:
            frames.append(pd.read_sql(text(f"""
                SELECT outage_equipment_id, actual_start,
                       CASE WHEN actual_start IS NOT NULL AND actual_end IS NULL AND ({stale}) THEN {stale_end}
                            ELSE actual_end END AS actual_end
                FROM {table}
                WHERE outage_equipment_id IN ({ph}) AND {" AND ".join(where)}
            """), conn, params={**params, **p}))
    return _to_hour_ranges(pd.concat(frames, ignore_index=True))


def active_outage_hours(engine, market: str, equipment_ids) -> list:
    ids = sorted({int(e) for e in equipment_ids})
    if not ids or f"{market}_active_outages" not in _table_names(engine):
        return []
    frames = []
    for chunk in _chunks(ids):
        ph, p = _in("e", chunk)
        with engine.connect() as conn:
            frames.append(pd.read_sql(text(f"""
                SELECT outage_equipment_id, actual_start, actual_end FROM {market}_active_outages
                WHERE outage_equipment_id IN ({ph}) AND actual_start IS NOT NULL
            """), conn, params=p))
    return _to_hour_ranges(pd.concat(frames, ignore_index=True))


def equipment_with_outages(engine, market: str, table: str, equipment_ids) -> set[int]:
    ids = sorted({int(e) for e in equipment_ids})
    if not ids or table not in _table_names(engine):
        return set()
    found = set()
    for chunk in _chunks(ids):
        ph, p = _in("e", chunk)
        with engine.connect() as conn:
            found |= {int(r[0]) for r in conn.execute(text(
                f"SELECT DISTINCT outage_equipment_id FROM {table} WHERE outage_equipment_id IN ({ph})"), p).fetchall()}
    return found


class OutageHours:
    """Outage hours per filter key, cached for one search. Keys: ('group', id), ('sched', id),
    ('active', id)."""

    def __init__(self, engine, market: str, relative_dt: datetime.date):
        self.engine, self.market, self.relative_dt = engine, market, relative_dt
        self.stale_type = str(market_param(engine, market, "outage_stale_type") or "last_update_for_date")
        self.stale_days = int(market_param(engine, market, "outage_stale_days") or 90)
        self.dropped_offset = int(market_param(engine, market, "dropped_outage_end_date_offset") or 3600)
        self._cache = {}

    def _tx(self, ids):
        return transmission_outage_hours(self.engine, self.market, ids, self.stale_type, self.stale_days,
                                         self.dropped_offset, self.relative_dt)

    def ranges(self, key) -> list:
        if key not in self._cache:
            kind, ident = key
            if kind == "group":
                members = group_members_for_shadows(self.engine, self.market, ident)
                act = equipment_with_outages(self.engine, self.market, f"{self.market}_active_outages", members)
                tx = equipment_with_outages(self.engine, self.market, f"{self.market}_transmission_outages", members)
                self._cache[key] = self._tx(tx) + active_outage_hours(self.engine, self.market, act)
            elif kind == "active":
                self._cache[key] = active_outage_hours(self.engine, self.market, [ident])
            else:
                self._cache[key] = self._tx([ident])
        return self._cache[key]


# --- RT TOTAL SHADOWS ---

def _hour_mask(hours: np.ndarray, ranges: list) -> np.ndarray:
    """True for each hour-start timestamp inside any [start, end] range (inclusive)."""
    if not ranges or len(hours) == 0:
        return np.zeros(len(hours), dtype=bool)
    rs = sorted(ranges)
    merged = [list(rs[0])]
    for s, e in rs[1:]:
        if s <= merged[-1][1] + pd.Timedelta(hours=1):
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    starts = np.array([m[0] for m in merged], dtype="datetime64[ns]")
    ends = np.array([m[1] for m in merged], dtype="datetime64[ns]")
    idx = np.searchsorted(starts, hours, side="right") - 1
    ok = idx >= 0
    out = np.zeros(len(hours), dtype=bool)
    out[ok] = hours[ok] <= ends[idx[ok]]
    return out


class RtShadows:
    """RT shadow prices per constraint set over the shadow window, cached for one search."""

    def __init__(self, engine, market: str, start: datetime.date, stop: datetime.date):
        self.engine, self.market, self.start, self.stop = engine, market, start, stop
        self._loaded: set[int] = set()
        self._all = pd.DataFrame({"official_constraint_id": pd.Series(dtype=int),
                                  "hour": pd.Series(dtype="datetime64[ns]"), "shadow_price": pd.Series(dtype=float)})

    def prefetch(self, constraint_ids) -> None:
        """Loads the RT shadow prices of all these constraints over the window in one pass."""
        todo = sorted({int(c) for c in constraint_ids} - self._loaded)
        frames = []
        for chunk in _chunks(todo, 500):
            ph, params = _in("c", chunk)
            params.update(s=self.start, e=self.stop)
            with self.engine.connect() as conn:
                frames.append(pd.read_sql(text(f"""
                    SELECT official_constraint_id, dt, hr, shadow_price FROM {self.market}_rt_constraint_shadow_prices
                    WHERE official_constraint_id IN ({ph}) AND dt BETWEEN :s AND :e
                """), conn, params=params))
        self._loaded |= set(todo)
        if frames:
            df = pd.concat(frames, ignore_index=True)
            df["hour"] = (pd.to_datetime(df["dt"]) + pd.to_timedelta(df["hr"].astype(int) - 1, unit="h")).values
            df["shadow_price"] = df["shadow_price"].astype(float)
            self._all = pd.concat([self._all, df[["official_constraint_id", "hour", "shadow_price"]]], ignore_index=True)

    def frame(self, constraint_ids) -> pd.DataFrame:
        self.prefetch(constraint_ids)
        return self._all[self._all["official_constraint_id"].isin([int(c) for c in constraint_ids])]

    def overlaps(self, ranges) -> bool:
        lo = pd.Timestamp(self.start)
        hi = pd.Timestamp(self.stop) + pd.Timedelta(hours=23)
        return any(s <= hi and e >= lo for s, e in ranges)

    def total(self, constraint_ids, ranges) -> tuple[float, str]:
        """(total, how). As in EnergyCore, an outage with no outage hours at all is not a filter:
        the total covers every hour of the window ("all hours"). Outage hours that all fall
        outside the window make EnergyCore's page fail; here they give 0 ("no overlap")."""
        df = self.frame(constraint_ids)
        if not ranges:
            return (float(df["shadow_price"].sum()) if not df.empty else 0.0), "all hours"
        if not self.overlaps(ranges):
            return 0.0, "no overlap"
        if df.empty:
            return 0.0, "outage hours"
        mask = _hour_mask(df["hour"].values.astype("datetime64[ns]"), ranges)
        return float(df.loc[mask, "shadow_price"].sum()), "outage hours"


def shadow_window(today: datetime.date) -> tuple[datetime.date, datetime.date]:
    start = (pd.Timestamp(today) - pd.DateOffset(years=SHADOW_LOOKBACK_YEARS)).date()
    return start, today + datetime.timedelta(days=1)


def lookback_start(today: datetime.date, days: int | None = None, months: int | None = None) -> datetime.date:
    if days is not None:
        return today - datetime.timedelta(days=days)
    return (pd.Timestamp(today) - pd.DateOffset(months=months if months is not None else DEFAULT_LOOKBACK_MONTHS)).date()


# --- THE SEARCH ---

def search(engine, market: str, equipment_ids, request_ids=(), version: datetime.date | None = None,
           start: datetime.date | None = None, stop: datetime.date | None = None, filters: dict | None = None,
           apply_regression_ignore_rules: bool = False, warn=_no_warn) -> dict:
    """Runs the Long-LODF search for these outage equipment IDs. ``filters`` holds the
    FILTER_KEYS values (None = no threshold). Returns rows (one per element, equipment, branch
    pairing, and outage source; outage records of the same equipment are folded together) plus
    what was used."""
    filters = dict(filters or {})
    today = current_date(engine, market)
    version = version or default_version(engine, market)
    if version is None:
        raise ValueError(f"{market.upper()} has no default network model version (netmap_versions.tool_use_default).")
    if not model_tables_exist(engine, market, version):
        raise ValueError(f"No {market.upper()} LODF/branch tables for model version {version}.")
    include_active = supports_active_outages(engine, market)

    rows = lodf_rows(engine, market, version, equipment_ids, request_ids,
                     threshold_min_lodf=filters.get("threshold_min_lodf"),
                     threshold_min_post_outage_flow_pct_of_limit=filters.get("threshold_min_post_outage_flow_pct_of_limit"),
                     threshold_min_flow_delta_pct_of_limit=filters.get("threshold_min_flow_delta_pct_of_limit"),
                     threshold_min_flow_max_delta_pct_of_limit=filters.get("threshold_min_flow_max_delta_pct_of_limit"),
                     apply_regression_ignore_rules=apply_regression_ignore_rules, include_active=include_active)
    used = {"version": version, "today": today, "start": start, "stop": stop, "include_active": include_active,
            "lodf_rows_before_element_filter": len(rows)}
    if rows.empty:
        used["mapped_equipment"] = mapped_equipment(engine, market, version, equipment_ids)
        return {"rows": rows, **used}

    me_ids = rows["official_monitored_element_id"].dropna().astype(int).unique().tolist()
    keep = elements_with_active_constraints(engine, market, me_ids, start, stop)
    rows = rows[rows["official_monitored_element_id"].isin(keep)].copy()
    if rows.empty:
        return {"rows": rows, **used}

    # Fold outage records of one equipment (several requests, or several versions of a request)
    # into the first row in EnergyCore's order; their LODF values are the same.
    key = ["official_monitored_element_id", "outage_equipment_id", "mon_br_shortname", "out_br_shortname", "outage_source"]
    rows["_order"] = range(len(rows))
    agg = rows.groupby(key, sort=False, dropna=False).agg(
        request_ids=("outage_request_id", lambda s: list(dict.fromkeys(str(x) for x in s if x is not None and not pd.isna(x)))),
        outage_records=("outage_request_id", "size"),
        latest_start=("actual_start", "max"))
    rows = rows.drop_duplicates(key, keep="first").join(agg, on=key).sort_values("_order").drop(columns="_order")

    cons = constraints_by_element(engine, market, rows["official_monitored_element_id"].astype(int).unique(), start, stop)
    rows["constraint_ids"] = [cons.get(int(m)) or [c for c in [backup_constraint(engine, market, int(m))] if c is not None]
                              for m in rows["official_monitored_element_id"]]
    groups = groups_for_equipment(engine, market, rows["outage_equipment_id"].astype(int).unique())
    rows["groups"] = [groups.get(int(e), []) for e in rows["outage_equipment_id"]]

    rows["total_rt_shadows"] = np.nan
    rows["rt_total_basis"] = ""
    if filters.get("use_total_rt_shadows"):
        s_start, s_stop = shadow_window(today)
        used["shadow_window"] = (s_start, s_stop)
        hours = OutageHours(engine, market, s_stop)
        shadows = RtShadows(engine, market, s_start, s_stop)
        graph = RelationGraph(engine, market)
        csets = {tuple(sorted(c)) for c in rows["constraint_ids"] if c}
        graph.expand({c for cset in csets for c in cset})
        related = {cset: graph.closure(cset) for cset in csets}
        shadows.prefetch({c for v in related.values() for c in v})
        totals, bases = [], []
        for r in rows.itertuples():
            cset = tuple(sorted(r.constraint_ids))
            if not cset:
                totals.append(0.0)
                bases.append("no constraint")
                continue
            if r.groups:
                ranges = [rg for g in r.groups for rg in hours.ranges(("group", g))]
            else:
                ranges = hours.ranges(("active" if r.outage_source == "active" else "sched", int(r.outage_equipment_id)))
            total, basis = shadows.total(related[cset], ranges)
            totals.append(total)
            bases.append(basis)
        rows["total_rt_shadows"] = totals
        rows["rt_total_basis"] = bases
        threshold = filters.get("threshold_min_total_rt_shadows") or 0.0
        used["rows_before_rt_filter"] = len(rows)
        rows = rows[rows["total_rt_shadows"] >= threshold]
        used["related_constraints"] = related
    return {"rows": rows.reset_index(drop=True), **used}
