"""Data for explaining why a constraint bound: shadow prices, desk notes, generator
impacts from shift factors (across several output vendors), generator outages, zonal load,
and tie flows.

Used by ``mcp_server.py``. Like ``lib/outage_search.py``, nothing here imports
Streamlit, and functions that can hit a recoverable database error take a ``warn``
callable so a failed query is reported rather than silently dropped.

Shift factor sign convention (checked against desk notes for PJM on 2026-10-08):
a NEGATIVE factor means more output from that generator LOADS the constraint. So the
flow a generator adds to the constraint is ``-factor * MW``; ``load_mw`` below uses
that sign, with positive meaning loading and negative meaning relieving.

Factors come from ``{market}_constraint_congestion_factor_master_details``, which
EnergyCore fits from recent binding intervals, so they reflect the topology (outages)
at the time of the fit, not necessarily today's.
"""

import datetime
import re

import pandas as pd
from sqlalchemy import text

from lib import outage_search as osearch

KINDS = {"rt": "rt_constraint_shadow_prices", "da": "da_constraint_shadow_prices"}

# Generator output sources, as {name: settings}: the pnode_generator_mappings
# generator_type suffix, the plants table (ID, name, and fuel columns), and the hourly
# output table (plant ID, MW, and readings-count columns; None when it has no count).
OUTPUT_SOURCES = {
    "muse": {"gtype": "MusePlant", "plants": "muse_plants", "id": "id", "name": "COALESCE(p.label, p.name)",
             "fuel": "p.fuel", "outputs": "muse_plant_hourly_outputs", "out_id": "plant_id", "mw": "avg_output_mw",
             "readings": "num_readings"},
    "gs": {"gtype": "GsPlant", "plants": "gs_plants", "id": "plant_id", "name": "p.plant_name",
           "fuel": "p.generation_type", "outputs": "gs_plant_hourly_outputs", "out_id": "plant_id",
           "mw": "avg_output_mw", "readings": "num_readings"},
    "lpi": {"gtype": "LpiGenUnit", "plants": "lpi_gen_units", "id": "lpi_genunit_id", "name": "p.name",
            "fuel": "p.fuel", "outputs": "lpi_gen", "out_id": "lpi_genunit_id", "mw": "avg_mw",
            "readings": "num_readings"},
    "meteologica": {"gtype": "MeteologicaResourceNode", "plants": "meteologica_resource_nodes", "id": "id",
                    "name": "p.name", "fuel": "p.resource_type",
                    "outputs": "meteologica_resource_node_observation_estimates", "out_id": "resource_node_id",
                    "mw": "estimated_mw", "readings": None},
}
# "auto" combines the sources above, per pnode (see generator_impacts). LPI's plant
# numbers are inferred from line-flow monitoring and can be badly off where plants share
# lines (on 2026-10-08 LPI showed PJM Peach Bottom nuclear swinging 450 MW with Muddy Run,
# and Wildcat at 0 while it ramped), so it is only used where no other source has the plant.
GENERATOR_SOURCES = ("auto",) + tuple(OUTPUT_SOURCES)
AUTO_PRIMARY_SOURCES = ("muse", "gs", "meteologica")
AUTO_FALLBACK_SOURCES = ("lpi",)

# Two sources disagree on a pnode's output change when they differ by more than this many
# MW and this share of the larger change.
DISAGREE_MW = 10.0
DISAGREE_SHARE = 0.25

# Fuels whose output the market redispatches to manage a binding constraint. A plant
# fueled only by these that moves in the relieving direction while the constraint binds
# may be responding to the constraint rather than causing the change. Plants that also
# list nuclear, solar, wind, or hydro (e.g. "Nuclear, Natural Gas") are not flagged,
# since the output source does not say which part moved.
DISPATCHABLE_FUEL = re.compile(r"gas|coal|oil", re.IGNORECASE)
NON_DISPATCHABLE_FUEL = re.compile(r"nuclear|solar|wind|hydro", re.IGNORECASE)

MARKDOWN_LINK = re.compile(r"\]\(https?://[^)]+\)")


def _no_warn(_message):
    pass


def _check_market(market: str) -> None:
    if market not in osearch.MARKETS:
        raise ValueError(f"Unknown market '{market}'. Use one of: {', '.join(osearch.MARKETS)}")


def _check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"Unknown kind '{kind}'. Use one of: {', '.join(KINDS)}")


def _in_params(prefix: str, values) -> tuple[str, dict]:
    values = list(values)
    return (", ".join(f":{prefix}{i}" for i in range(len(values))),
            {f"{prefix}{i}": v for i, v in enumerate(values)})


# --- SHADOW PRICES ---

def binding_constraints(engine, market: str, day: datetime.date, kind: str = "rt", limit: int = 15) -> pd.DataFrame:
    """Constraints with shadow prices on ``day``, largest absolute daily total first.

    One row per (monitored element, constraint) with the daily total, hours bound, first
    and last binding hour, and the peak hour and price. RT rows are often preliminary for
    the current day, and only run through the latest published hour.
    """
    _check_market(market)
    _check_kind(kind)
    prelim = "MAX(s.preliminary)" if kind == "rt" else "0"
    with engine.connect() as conn:
        df = pd.read_sql(text(f"""
            SELECT s.official_monitored_element_id AS me_id, s.official_constraint_id AS constraint_id,
                   MAX(s.constraint_name) AS name, SUM(s.shadow_price) AS total,
                   COUNT(DISTINCT s.hr) AS hours, MIN(s.hr) AS first_hr, MAX(s.hr) AS last_hr,
                   MAX(ABS(s.shadow_price)) AS peak, {prelim} AS preliminary
            FROM {market}_{KINDS[kind]} s
            WHERE s.dt = :day
            GROUP BY s.official_monitored_element_id, s.official_constraint_id
            ORDER BY ABS(SUM(s.shadow_price)) DESC
            LIMIT {int(limit)}
        """), conn, params={"day": day})
    if df.empty:
        return df

    hourly = constraint_hourly(engine, market, day, df["constraint_id"].tolist(), kind)
    peaks = hourly.loc[hourly.groupby("constraint_id")["shadow_price"].apply(lambda s: s.abs().idxmax())]
    df = df.merge(peaks[["constraint_id", "hr"]].rename(columns={"hr": "peak_hr"}), on="constraint_id", how="left")
    return df


def constraint_hourly(engine, market: str, day: datetime.date, constraint_ids, kind: str = "rt") -> pd.DataFrame:
    """Hourly shadow prices (summed over intervals) for the given constraints on ``day``."""
    _check_market(market)
    _check_kind(kind)
    ids = list(constraint_ids)
    if not ids:
        return pd.DataFrame(columns=["constraint_id", "hr", "shadow_price"])
    placeholders, params = _in_params("c", ids)
    params["day"] = day
    with engine.connect() as conn:
        return pd.read_sql(text(f"""
            SELECT official_constraint_id AS constraint_id, hr, SUM(shadow_price) AS shadow_price
            FROM {market}_{KINDS[kind]}
            WHERE dt = :day AND official_constraint_id IN ({placeholders})
            GROUP BY official_constraint_id, hr
            ORDER BY official_constraint_id, hr
        """), conn, params=params)


def constraint_info(engine, market: str, day: datetime.date, constraint_id: int, kind: str = "rt") -> tuple:
    """(monitored element ID, constraint name) for a constraint with shadow prices on ``day``."""
    _check_market(market)
    _check_kind(kind)
    with engine.connect() as conn:
        row = conn.execute(text(f"""
            SELECT MAX(official_monitored_element_id), MAX(constraint_name)
            FROM {market}_{KINDS[kind]}
            WHERE dt = :day AND official_constraint_id = :c
        """), {"day": day, "c": constraint_id}).fetchone()
    return row[0], (row[1] or "").strip()


def binding_history(engine, market: str, me_ids, day: datetime.date, kind: str = "rt") -> pd.DataFrame:
    """Per monitored element, over the 3 years before ``day``: days bound, total, largest
    single day, total for the 30 days before ``day``, and the last day it bound."""
    _check_market(market)
    _check_kind(kind)
    # Constraints not yet mapped to a monitored element (common for the current day) have no ID.
    ids = sorted({int(m) for m in me_ids if pd.notna(m)})
    if not ids:
        return pd.DataFrame(columns=["me_id", "days_bound", "total_3y", "max_day", "total_30d", "last_bound"])
    placeholders, params = _in_params("m", ids)
    params.update({"day": day, "start": day - datetime.timedelta(days=3 * 365),
                   "recent": day - datetime.timedelta(days=30)})
    with engine.connect() as conn:
        return pd.read_sql(text(f"""
            SELECT me_id, COUNT(*) AS days_bound, SUM(d) AS total_3y, MAX(ABS(d)) AS max_day,
                   SUM(CASE WHEN dt >= :recent THEN d ELSE 0 END) AS total_30d, MAX(dt) AS last_bound
            FROM (
                SELECT official_monitored_element_id AS me_id, dt, SUM(shadow_price) AS d
                FROM {market}_{KINDS[kind]}
                WHERE official_monitored_element_id IN ({placeholders}) AND dt >= :start AND dt < :day
                GROUP BY official_monitored_element_id, dt
            ) x
            GROUP BY me_id
        """), conn, params=params)


def default_hours(hourly: pd.DataFrame) -> tuple[int | None, int | None]:
    """(base_hr, compare_hr) for one constraint's hourly shadow prices.

    compare_hr is the peak hour. base_hr is the latest hour before the peak with the
    lowest shadow price (hours that did not bind count as 0), so changes are measured
    from before the constraint started binding where possible. That keeps the market's
    own redispatch, which happens after binding starts, out of the baseline.
    Returns (None, peak) when the peak is HE1.
    """
    if hourly.empty:
        return None, None
    by_hr = hourly.set_index("hr")["shadow_price"]
    compare_hr = int(by_hr.abs().idxmax())
    before = [(abs(by_hr.get(h, 0.0)), -h) for h in range(1, compare_hr)]
    if not before:
        return None, compare_hr
    return -min(before)[1], compare_hr


# --- DESK NOTES ---

def element_notes(engine, market: str, me_ids, since: datetime.date, until: datetime.date) -> pd.DataFrame:
    """Quick notes on the given monitored elements dated ``since`` through ``until``, newest first.

    Markdown links are reduced to their text, so ``[NAME e:123](https://...)`` becomes
    ``[NAME e:123]``.
    """
    _check_market(market)
    ids = sorted({int(m) for m in me_ids if pd.notna(m)})
    if not ids:
        return pd.DataFrame(columns=["me_id", "name", "dt", "context", "body"])
    placeholders, params = _in_params("m", ids)
    params.update({"since": since, "until": until})
    with engine.connect() as conn:
        df = pd.read_sql(text(f"""
            SELECT qn.official_monitored_element_id AS me_id, ome.monitored_element_name AS name,
                   qn.dt, qn.context, qn.body
            FROM {market}_monelem_quick_notes qn
            LEFT JOIN {market}_official_monitored_elements ome ON ome.id = qn.official_monitored_element_id
            WHERE qn.official_monitored_element_id IN ({placeholders}) AND qn.dt BETWEEN :since AND :until
            ORDER BY qn.dt DESC
        """), conn, params=params)
    df["body"] = df["body"].fillna("").map(lambda b: MARKDOWN_LINK.sub("]", b.replace("\r", "").replace("\n", " ")))
    return df


# --- SHIFT FACTORS AND GENERATOR IMPACTS ---

def factor_set(engine, market: str, constraint_id: int) -> tuple[dict | None, pd.DataFrame]:
    """The constraint's current master factor set: (metadata, DataFrame of pnode factors).

    Metadata is None and the DataFrame empty when EnergyCore has no factors for it.
    """
    _check_market(market)
    with engine.connect() as conn:
        meta = conn.execute(text(f"""
            SELECT reading_time, source_type, updated_at, max_factor, min_factor
            FROM {market}_constraint_congestion_factor_masters
            WHERE official_constraint_id = :c
        """), {"c": constraint_id}).mappings().fetchone()
        factors = pd.read_sql(text(f"""
            SELECT official_pnode_id, factor
            FROM {market}_constraint_congestion_factor_master_details
            WHERE official_constraint_id = :c
        """), conn, params={"c": constraint_id})
    return (dict(meta) if meta else None), factors


def _source_mapping(engine, market: str, source: str) -> pd.DataFrame:
    """[official_pnode_id, plant_id, share, name, fuel] for one output source. ``share`` is
    the plant's mapping proportion at that pnode, normalized so a plant's shares sum to 1."""
    cfg = OUTPUT_SOURCES[source]
    with engine.connect() as conn:
        df = pd.read_sql(text(f"""
            SELECT m.official_pnode_id, m.generator_id AS plant_id, COALESCE(m.proportion, 1) AS prop,
                   {cfg["name"]} AS name, {cfg["fuel"]} AS fuel
            FROM {market}_pnode_generator_mappings m
            JOIN {market}_{cfg["plants"]} p ON p.{cfg["id"]} = m.generator_id
            WHERE m.generator_type = :gtype
        """), conn, params={"gtype": market.capitalize() + cfg["gtype"]})
    df["share"] = df["prop"] / df.groupby("plant_id")["prop"].transform("sum")
    return df.drop(columns="prop")


def plant_factors(engine, market: str, factors: pd.DataFrame, source: str = "muse") -> pd.DataFrame:
    """Shift factor per plant: the mapping-share-weighted mean of its pnodes' factors."""
    _check_market(market)
    joined = _source_mapping(engine, market, source).merge(factors, on="official_pnode_id")
    if joined.empty:
        return pd.DataFrame(columns=["plant_id", "name", "fuel", "sf"])
    joined["weighted"] = joined["factor"] * joined["share"]
    grouped = joined.groupby(["plant_id", "name", "fuel"], dropna=False)
    return (grouped["weighted"].sum() / grouped["share"].sum()).rename("sf").reset_index()


def plant_output(engine, market: str, day: datetime.date, plant_ids, hours, source: str = "muse") -> pd.DataFrame:
    """Average hourly output (MW) per plant for the given hours: plants as rows, hours as columns."""
    _check_market(market)
    ids, hrs = list(plant_ids), [int(h) for h in hours]
    if not ids or not hrs:
        return pd.DataFrame()
    cfg = OUTPUT_SOURCES[source]
    p_ph, params = _in_params("p", ids)
    h_ph, h_params = _in_params("h", hrs)
    params.update(h_params)
    params["day"] = day
    # Rows with no readings are placeholders (e.g. Muse shows a flat nameplate-like value
    # for MISO New Frontier Wind while Genscape shows it at 0), so they are left out.
    readings = f"AND {cfg['readings']} > 0" if cfg["readings"] else ""
    with engine.connect() as conn:
        df = pd.read_sql(text(f"""
            SELECT {cfg["out_id"]} AS plant_id, hr, AVG({cfg["mw"]}) AS mw
            FROM {market}_{cfg["outputs"]}
            WHERE dt = :day AND {cfg["out_id"]} IN ({p_ph}) AND hr IN ({h_ph}) {readings}
            GROUP BY {cfg["out_id"]}, hr
        """), conn, params=params)
    return df.pivot_table(index="plant_id", columns="hr", values="mw")


def _flag_redispatch(df: pd.DataFrame, compare_hr: int, binding_hours) -> pd.Series:
    fuel = df["fuel"].fillna("").astype(str)
    return ((compare_hr in set(binding_hours)) & (df["load_mw"] < 0) & fuel.str.contains(DISPATCHABLE_FUEL)
            & ~fuel.str.contains(NON_DISPATCHABLE_FUEL))


def _single_source_impacts(engine, market, day, factors, base_hr, compare_hr, source, min_abs_factor, warn):
    plants = plant_factors(engine, market, factors, source)
    plants = plants[plants["sf"].abs() >= min_abs_factor]
    if plants.empty:
        warn(f"No {source} plants are mapped to pnodes with |factor| >= {min_abs_factor}.")
        return pd.DataFrame()
    output = plant_output(engine, market, day, plants["plant_id"], [base_hr, compare_hr], source)
    output = output.reindex(columns=[base_hr, compare_hr])
    for hr in (base_hr, compare_hr):
        if output[hr].isna().all():
            warn(f"No {source} plant output for HE{hr} on {day} (it may not have happened or been published yet).")
    # Plants without output for both hours are dropped rather than counted as 0 MW, which
    # would show a full-capacity swing that did not happen.
    df = plants.merge(output.dropna().rename(columns={base_hr: "base_mw", compare_hr: "compare_mw"}),
                      left_on="plant_id", right_index=True, how="inner")
    df["d_mw"] = df["compare_mw"] - df["base_mw"]
    df["load_mw"] = -df["sf"] * df["d_mw"]
    df["sources"] = source
    df["disagreement"] = ""
    return df


def _auto_impacts(engine, market, day, factors, base_hr, compare_hr, min_abs_factor, warn):
    """Per pnode, output from every source that has it at both hours; the median change
    across sources is used, and differences between sources are reported."""
    factors = factors[factors["factor"].abs() >= min_abs_factor]
    per_source = []
    names = {}
    for source in AUTO_PRIMARY_SOURCES + AUTO_FALLBACK_SOURCES:
        try:
            mapping = _source_mapping(engine, market, source).merge(factors, on="official_pnode_id")
        except Exception as e:
            warn(f"Could not load {source} generator mappings: {e}")
            continue
        if mapping.empty:
            continue
        output = plant_output(engine, market, day, mapping["plant_id"].unique(), [base_hr, compare_hr], source)
        output = output.reindex(columns=[base_hr, compare_hr]).dropna()
        if output.empty:
            continue
        joined = mapping.merge(output, left_on="plant_id", right_index=True)
        joined["base"] = joined[base_hr] * joined["share"]
        joined["compare"] = joined[compare_hr] * joined["share"]
        agg = joined.groupby("official_pnode_id").agg(base=("base", "sum"), compare=("compare", "sum"))
        agg["source"] = source
        per_source.append(agg.reset_index())
        for pnode, g in joined.groupby("official_pnode_id"):
            names.setdefault(pnode, (" + ".join(dict.fromkeys(g["name"].astype(str))),
                                     " / ".join(dict.fromkeys(g["fuel"].dropna().astype(str)))))
    if not per_source:
        warn("No generator output from any source for these hours.")
        return pd.DataFrame()

    long = pd.concat(per_source, ignore_index=True)
    long["d"] = long["compare"] - long["base"]
    rows = []
    for pnode, g in long.groupby("official_pnode_id"):
        d = g.set_index("source")["d"]
        spread = d.max() - d.min()
        disagree = len(d) > 1 and spread > DISAGREE_MW and spread > DISAGREE_SHARE * d.abs().max()
        used = g[g["source"].isin(AUTO_PRIMARY_SOURCES)]
        if used.empty:
            used = g
        name, fuel = names.get(pnode, (str(pnode), ""))
        rows.append({"official_pnode_id": pnode, "name": name, "fuel": fuel,
                     "base_mw": used["base"].median(), "compare_mw": used["compare"].median(),
                     "d_mw": used["d"].median(), "sources": ",".join(used["source"]),
                     "disagreement": " / ".join(f"{s} {v:+.0f}" for s, v in d.items()) if disagree else ""})
    df = pd.DataFrame(rows).merge(factors, on="official_pnode_id")
    df["load_mw"] = -df["factor"] * df["d_mw"]

    # One row per plant name: plants split across several pnodes are summed.
    grouped = df.groupby(["name", "fuel"], as_index=False).agg(
        base_mw=("base_mw", "sum"), compare_mw=("compare_mw", "sum"), d_mw=("d_mw", "sum"),
        load_mw=("load_mw", "sum"), sf=("factor", "mean"),
        sources=("sources", lambda x: ",".join(dict.fromkeys(",".join(x).split(",")))),
        disagreement=("disagreement", lambda x: "; ".join(dict.fromkeys(v for v in x if v))))
    return grouped


def generator_impacts(engine, market: str, day: datetime.date, constraint_id: int, base_hr: int, compare_hr: int,
                      binding_hours=(), source: str = "auto", min_abs_factor: float = 0.02,
                      warn=_no_warn) -> tuple[dict | None, pd.DataFrame]:
    """How generator output changes between ``base_hr`` and ``compare_hr`` moved flow on the constraint.

    ``source`` is one output source (muse, gs, lpi, meteologica) or ``auto``: every source,
    combined per pnode by taking the median output change of the sources that report it, so
    one vendor's bad data (a flat placeholder, a missed outage) does not decide the answer.

    Returns (factor set metadata, DataFrame) with one row per plant whose |factor| is at least
    ``min_abs_factor``, sorted by |load_mw|:

    - ``sf``: the shift factor (negative = more output loads the constraint)
    - ``base_mw``, ``compare_mw``, ``d_mw``: output at each hour and the change
    - ``load_mw``: flow the change added to the constraint (``-sf * d_mw``; + loading, - relieving)
    - ``sources``: which sources reported it; ``disagreement``: each source's change when
      they differ by more than DISAGREE_MW and DISAGREE_SHARE
    - ``possible_redispatch``: gas, coal, or oil plants that moved in the relieving direction
      while the constraint was binding at ``compare_hr``. These may be the market responding
      to the constraint rather than a cause of it. (Generator offer data, which could show
      which units were marginal, is months stale in the database, so it is not used.)
    """
    if source not in GENERATOR_SOURCES:
        raise ValueError(f"Unknown source '{source}'. Use one of: {', '.join(GENERATOR_SOURCES)}")
    meta, factors = factor_set(engine, market, constraint_id)
    if factors.empty:
        warn(f"No shift factors found for {market.upper()} constraint {constraint_id}.")
        return meta, pd.DataFrame()

    if source == "auto":
        df = _auto_impacts(engine, market, day, factors, base_hr, compare_hr, min_abs_factor, warn)
    else:
        df = _single_source_impacts(engine, market, day, factors, base_hr, compare_hr, source, min_abs_factor, warn)
    if df.empty:
        return meta, df
    df["possible_redispatch"] = _flag_redispatch(df, compare_hr, binding_hours)
    df = df.reindex(df["load_mw"].abs().sort_values(ascending=False).index).reset_index(drop=True)
    return meta, df


def generator_outages(engine, market: str, day: datetime.date, constraint_id: int, min_abs_factor: float = 0.02,
                      warn=_no_warn) -> pd.DataFrame:
    """Generator outages and derates in effect on ``day`` (IIR) at plants that matter for the
    constraint, with the flow each would add if the unit would otherwise have run at that
    output: ``load_mw = sf * capacity_offline`` (taking out a plant with sf > 0, which
    relieves, loads the constraint).

    Columns: [unit_name, plant_name, prim_fuel, capacity_offline, sf, load_mw, outage_type,
    status, start, end, cause, comments], largest |load_mw| first.
    """
    _check_market(market)
    _, factors = factor_set(engine, market, constraint_id)
    if factors.empty:
        return pd.DataFrame()
    with engine.connect() as conn:
        df = pd.read_sql(text(f"""
            SELECT o.unit_id, o.unit_name, o.plant_name, o.prim_fuel, o.cap_offlin AS capacity_offline,
                   o.outage_typ AS outage_type, o.outage_sta AS status, o.ta_start AS start, o.ta_end AS end,
                   o.out_cause AS cause, LEFT(o.comments, 200) AS comments, m.official_pnode_id,
                   COALESCE(m.proportion, 1) AS prop
            FROM {market}_iir_outages o
            JOIN {market}_pnode_generator_mappings m ON m.generator_id = o.unit_id AND m.generator_type = :gtype
            WHERE o.ta_start <= :d AND o.ta_end >= :d AND o.outage_sta <> 'Cancelled'
        """), conn, params={"d": day, "gtype": market.capitalize() + "IirUnit"})
    if df.empty:
        return df
    df = df.merge(factors, on="official_pnode_id")
    df["weighted"] = df["factor"] * df["prop"]
    keys = ["unit_id", "unit_name", "plant_name", "prim_fuel", "capacity_offline", "outage_type", "status", "start",
            "end", "cause", "comments"]
    grouped = df.groupby(keys, dropna=False)
    out = (grouped["weighted"].sum() / grouped["prop"].sum()).rename("sf").reset_index()
    out = out[out["sf"].abs() >= min_abs_factor]
    out["load_mw"] = out["sf"] * out["capacity_offline"].astype(float)
    return out.reindex(out["load_mw"].abs().sort_values(ascending=False).index).drop(columns="unit_id").reset_index(drop=True)


# --- LOAD AND INTERCHANGE ---

def zonal_load(engine, market: str, day: datetime.date, base_hr: int, compare_hr: int) -> pd.DataFrame:
    """Load by zone at ``base_hr`` and ``compare_hr`` on ``day`` and the change, largest change first.
    Zones without load for both hours are left out."""
    _check_market(market)
    with engine.connect() as conn:
        df = pd.read_sql(text(f"""
            SELECT zone, hr, load_mwh
            FROM {market}_hourly_load
            WHERE dt = :day AND hr IN (:b, :c)
        """), conn, params={"day": day, "b": base_hr, "c": compare_hr})
    if df.empty:
        return df
    df["load_mwh"] = df["load_mwh"].astype(float)
    pivot = df.pivot_table(index="zone", columns="hr", values="load_mwh").reindex(columns=[base_hr, compare_hr]).dropna()
    pivot.columns = ["base_mw", "compare_mw"]
    pivot["d_mw"] = pivot["compare_mw"] - pivot["base_mw"]
    return pivot.reindex(pivot["d_mw"].abs().sort_values(ascending=False).index).reset_index()


def tie_flows(engine, market: str, day: datetime.date, base_hr: int, compare_hr: int, warn=_no_warn) -> pd.DataFrame:
    """Actual minus scheduled flow per tie at ``base_hr`` and ``compare_hr``: a rough measure
    of unscheduled (loop) flow until autoflow data is available. Empty when the market has
    no tie flow table."""
    _check_market(market)
    try:
        with engine.connect() as conn:
            df = pd.read_sql(text(f"""
                SELECT tie_flow_name AS tie, hr, AVG(actual_mw) AS actual_mw, AVG(scheduled_mw) AS scheduled_mw
                FROM {market}_tie_flows
                WHERE dt = :day AND hr IN (:b, :c)
                GROUP BY tie_flow_name, hr
            """), conn, params={"day": day, "b": base_hr, "c": compare_hr})
    except Exception as e:
        warn(f"Could not load {market.upper()} tie flows: {e}")
        return pd.DataFrame()
    if df.empty:
        return df
    df["unscheduled_mw"] = df["actual_mw"] - df["scheduled_mw"]
    pivot = df.pivot_table(index="tie", columns="hr", values="unscheduled_mw").reindex(columns=[base_hr, compare_hr]).dropna()
    pivot.columns = ["base_unscheduled_mw", "compare_unscheduled_mw"]
    pivot["d_mw"] = pivot["compare_unscheduled_mw"] - pivot["base_unscheduled_mw"]
    return pivot.reindex(pivot["d_mw"].abs().sort_values(ascending=False).index).reset_index()
