"""Transmission outages and a binding constraint: which outages were in effect, how much
each one loads the constraint in the network model, when they started or ended relative
to the 5-minute shadow prices, and what EnergyCore's own outage matching and the desk's
outage notes say about them.

Impacts use the PowerWorld case through ``lib.network``:

- ``individual``: change in post-contingency flow on the monitored element when only this
  outage is added (the desk's "FIDi"-style number)
- ``in_combination``: change when this outage is removed from the full set of significant
  outages in effect (closer to "FIDn": what it adds on top of everything else)

Both are flow changes in the constraint's binding direction, so positive loads it. The
direction comes from the monitored element name (<+1>/<-1>, MISO) or, failing that, from the
direction of the case's post-contingency flow. They are also given as % of the limit, and
``check`` marks in-combination numbers much larger than the outage's own impact, which
usually means the outages together split off part of the network model. Outages are screened first with a first-order estimate so
that only those that can matter (at least ``MIN_SCREEN_MW``) get the exact calculation.
"""

import datetime
import re

import pandas as pd
from sqlalchemy import text

from lib import network
from lib import outage_search as osearch
from lib import upcoming_outages

MIN_SCREEN_MW = 1.0
# Outage equipment names for devices that switch or compensate rather than carry flow.
NON_BRANCH_DEVICE = re.compile(r"\b(CAP\d*|CB|GCB|OCB|BRK|BRKR|BKR|DIS|DISC|SW|SWITCH|REAC|REACTOR|SC\d*|LOAD|LD)\b",
                               re.IGNORECASE)
# Unmapped outages below this voltage are not shown as timing matches.
MIN_UNMAPPED_TIMING_KV = 100.0
MAX_EXACT_OUTAGES = 30
EXCLUDED_STATUSES = ("Cancelled", "CANCELLED", "Denied", "DENIED", "Withdrawn", "WITHDRAWN")

ACTIVE_NAME_SQL = {
    "miso": "CONCAT_WS(' ', kv, from_station, to_station, equipment_name)",
    "pjm": "CONCAT_WS(' ', b1, kv, b3)",
}


def _no_warn(_message):
    pass


# --- OUTAGES IN EFFECT ---

def _active_name_sql(market: str) -> str:
    return ACTIVE_NAME_SQL.get(market, "CAST(outage_equipment_id AS CHAR)")


def active_outages(engine, market: str, start: datetime.datetime, end: datetime.datetime) -> pd.DataFrame:
    """Rows of ``{market}_active_outages`` (the ISO's real-time outage list) out at any time
    between ``start`` and ``end`` (market local time).

    MISO rows carry ``last_update_for_date``; open-ended rows not refreshed since the day
    before ``start`` are treated as stale and left out.
    """
    stale = ""
    params = {"s": start, "e": end}
    if market == "miso":
        stale = "AND (actual_end IS NOT NULL OR last_update_for_date >= :fresh)"
        params["fresh"] = start - datetime.timedelta(days=1)
    with engine.connect() as conn:
        return pd.read_sql(text(f"""
            SELECT outage_equipment_id, {_active_name_sql(market)} AS name, actual_start, actual_end
            FROM {market}_active_outages
            WHERE actual_start <= :e AND (actual_end IS NULL OR actual_end >= :s) {stale}
        """), conn, params=params)


def planned_outages(engine, market: str, start: datetime.datetime, end: datetime.datetime,
                    warn=_no_warn) -> pd.DataFrame:
    """Current transmission outage report rows in effect between ``start`` and ``end``,
    using actual times where known, EnergyCore's regression ignore rules, and leaving out
    cancelled, denied, and withdrawn requests."""
    table = f"{market}_transmission_outages"
    name_cols = osearch.OUTAGE_NAME_COLUMNS.get(market, [])
    with engine.connect() as conn:
        columns = {r[0] for r in conn.execute(text(f"SHOW COLUMNS FROM {table}")).fetchall()}
    clauses, params = upcoming_outages._exclusion_sql(upcoming_outages.ignore_rules(engine, market), columns, "t", warn)
    status_ph = ", ".join(f":st{i}" for i in range(len(EXCLUDED_STATUSES)))
    params.update({f"st{i}": s for i, s in enumerate(EXCLUDED_STATUSES)})
    params.update({"s": start, "e": end})
    select_names = ", ".join(f"t.`{c}`" for c in name_cols if c in columns)
    where = ["t.most_recent = 1", f"t.request_status NOT IN ({status_ph})",
             "COALESCE(t.actual_start, t.planned_start) <= :e", "COALESCE(t.actual_end, t.planned_end) >= :s"] + clauses
    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT t.outage_equipment_id, t.request_status, t.planned_start, t.planned_end, t.actual_start,
                   t.actual_end{', ' + select_names if select_names else ''}
            FROM {table} t WHERE {" AND ".join(where)}
        """), params).mappings().fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return pd.DataFrame(columns=["outage_equipment_id", "name", "request_status", "start", "end"])
    df["name"] = [osearch.format_outage_name(market, r) for r in rows]
    df["start"] = df["actual_start"].fillna(df["planned_start"])
    df["end"] = df["actual_end"].fillna(df["planned_end"])
    return (df.sort_values("start").groupby("outage_equipment_id", as_index=False)
            .agg(name=("name", "first"), request_status=("request_status", "last"), start=("start", "min"),
                 end=("end", "max")))


def outages_in_effect(engine, market: str, start: datetime.datetime, end: datetime.datetime,
                      warn=_no_warn) -> pd.DataFrame:
    """One row per outage equipment ID out between ``start`` and ``end``, from the active
    table, the outage report, or both: [outage_equipment_id, name, in_active, request_status,
    start, end]."""
    active = active_outages(engine, market, start, end)
    planned = planned_outages(engine, market, start, end, warn)
    a = (active.groupby("outage_equipment_id", as_index=False)
         .agg(active_name=("name", "first"), active_start=("actual_start", "min"), active_end=("actual_end", "max")))
    df = a.merge(planned, on="outage_equipment_id", how="outer")
    df["in_active"] = df["active_name"].notna()
    df["name"] = df["name"].fillna(df["active_name"])
    df["start"] = df["active_start"].fillna(df["start"]) if "active_start" in df else df["start"]
    df["end"] = df["active_end"].combine_first(df["end"]) if "active_end" in df else df["end"]
    df["request_status"] = df["request_status"].fillna("active table only")
    return df[["outage_equipment_id", "name", "in_active", "request_status", "start", "end"]]


# --- IMPACTS ---

# Other markets whose outages can matter for a market's constraints, and that the network
# model can map (MISO maps neighbors' outages through miso_tiospf_outage_branch_mappings).
NEIGHBOR_MARKETS = {"miso": ("spp", "pjm")}


def _outages_with_branches(engine, market: str, version, start, end, warn) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Outages in effect in the market and its mapped neighbors, and their model branches.
    Returns (outages, mapping), keyed by ``outage_key`` = "market:equipment_id"."""
    frames, maps = [], []
    for src in (market,) + NEIGHBOR_MARKETS.get(market, ()):
        try:
            o = outages_in_effect(engine, src, start, end, warn)
        except Exception as e:
            warn(f"Could not load {src.upper()} outages: {e}")
            continue
        if o.empty:
            continue
        if src == market:
            mp = network.outage_branches(engine, market, version, o["outage_equipment_id"])
        else:
            mp = network.neighbor_outage_branches(engine, market, version, src, o["outage_equipment_id"], warn)
        o["market"] = src
        o["outage_key"] = src + ":" + o["outage_equipment_id"].astype("int64").astype(str)
        mp = mp.assign(outage_key=src + ":" + mp["outage_equipment_id"].astype("int64").astype(str))
        frames.append(o)
        maps.append(mp[["outage_key", "pw_branch_id"]])
    outages = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    mapping = pd.concat(maps, ignore_index=True) if maps else pd.DataFrame(columns=["outage_key", "pw_branch_id"])
    return outages, mapping


def outage_impacts(engine, market: str, constraint_id: int, start: datetime.datetime, end: datetime.datetime,
                   day: datetime.date, peak_hr: int | None = None, warn=_no_warn) -> tuple[dict, pd.DataFrame]:
    """How much each outage in effect loads the constraint in the network model.

    Returns (summary, impacts). The summary has the case version, monitored branch,
    contingency branches, limit, base pre- and post-contingency flows, and the flow with all
    significant outages out. ``impacts`` has one row per significant outage (outages that
    map to the same model branches, e.g. the same work in the outage report and the active
    table, are one row), largest first: [market, outage_equipment_id, name, in_active,
    request_status, start, end, pre_ctg_lodf_pct, individual_mw, individual_pct,
    in_combination_mw, in_combination_pct].
    """
    summary = {"version": None}
    defn = network.constraint_definition(engine, market, constraint_id, warn)
    summary.update(defn)
    if defn["me_id"] is None:
        warn(f"Constraint {constraint_id} has no monitored element, so outage impacts cannot be computed.")
        return summary, pd.DataFrame()

    version = network.latest_case(engine, market)
    summary["version"] = version
    if version is None:
        warn(f"No PowerWorld case with LODFs for {market.upper()}.")
        return summary, pd.DataFrame()

    mon = network.monitored_branches(engine, market, version, defn["me_id"])
    if mon.empty:
        warn(f"Monitored element {defn['me_id']} is not mapped to the {version} network model.")
        return summary, pd.DataFrame()
    m = int(mon["pw_branch_id"].iloc[0])
    ctg = [b for b in network.contingency_branches(engine, market, version, defn["contingency"], warn) if b != m]

    outages, mapping = _outages_with_branches(engine, market, version, start, end, warn)
    summary["outages_in_effect"] = len(outages)
    summary["outages_mapped"] = mapping["outage_key"].nunique()
    if outages.empty:
        return summary, pd.DataFrame()
    names = outages.set_index("outage_key")["name"].astype(str)

    ctg_out = mapping[mapping["pw_branch_id"].isin(ctg)]["outage_key"].unique()
    if len(ctg_out):
        warn("Part of the contingency is itself on outage: " + ", ".join(names.reindex(ctg_out).fillna("?")) + ".")
    mapping = mapping[~mapping["pw_branch_id"].isin(ctg + [m])]
    branch_sets = mapping.groupby("outage_key")["pw_branch_id"].apply(lambda x: tuple(sorted({int(b) for b in x})))

    all_branches = sorted({b for bs in branch_sets for b in bs})
    info = network.branch_info(engine, market, version, [m] + ctg + all_branches).set_index("branch_id")
    flows = info["line_mw"].astype(float)
    summary["monitored_branch"] = f"{info.loc[m, 'name']} (branch {m})" if m in info.index else str(m)
    summary["contingency_branches"] = [info.loc[c, "name"] if c in info.index else str(c) for c in ctg]

    limit = network.constraint_limit(engine, market, defn["me_id"], day, peak_hr, version, m)
    summary["limit"] = limit
    limit_mw = limit["limit_mw"]

    # Screen with a first-order estimate: (L(m,k) + sum_c L(m,c) L(c,k)) * f_k, per distinct branch set.
    distinct = pd.Series(sorted(set(branch_sets)), dtype=object)
    screen = network.lodf_matrix(engine, market, version, [m] + ctg, ctg + all_branches)
    direct = screen.loc[m, all_branches]
    via_ctg = (screen.loc[m, ctg].to_numpy() @ screen.loc[ctg, all_branches].to_numpy()) if ctg else 0.0
    first_order = (direct + via_ctg) * flows.reindex(all_branches).fillna(0.0)
    est = distinct.apply(lambda bs: float(first_order.reindex(list(bs)).abs().sum()))
    sig_sets = distinct[est >= MIN_SCREEN_MW].reindex(est[est >= MIN_SCREEN_MW].sort_values(ascending=False).index)
    sig_sets = list(sig_sets.head(MAX_EXACT_OUTAGES))
    summary["outages_screened_in"] = len(sig_sets)

    base_pre = float(flows.get(m, 0.0))
    needed = sorted({b for bs in sig_sets for b in bs})
    full = network.lodf_matrix(engine, market, version, [m] + ctg + needed, ctg + needed)
    base_post = network.flow_after_outages(flows, full, m, ctg, warn) if ctg else base_pre
    every = ctg + needed
    post_all = network.flow_after_outages(flows, full, m, every, warn)
    direction = defn["direction"]
    summary["direction_source"] = "monitored element name"
    if direction is None:
        direction = 1 if base_post >= 0 else -1
        summary["direction_source"] = "case post-contingency flow"
    summary["direction"] = direction
    # Flows reported in the binding direction: positive means toward the limit.
    summary.update(base_pre_mw=direction * base_pre, base_post_mw=direction * base_post,
                   post_all_mw=direction * post_all)

    rows = []
    for bs in sig_sets:
        keys = branch_sets[branch_sets == bs].index.tolist()
        group = outages.set_index("outage_key").loc[keys]
        lead = group.sort_values("in_active", ascending=False).iloc[0]
        post_k = network.flow_after_outages(flows, full, m, ctg + list(bs), warn)
        without_k = network.flow_after_outages(flows, full, m, [b for b in every if b not in bs], warn)
        rows.append({
            "market": lead["market"],
            "outage_equipment_id": int(lead["outage_equipment_id"]),
            "name": " / ".join(dict.fromkeys(group["name"].astype(str))),
            "in_active": bool(group["in_active"].any()),
            "request_status": ", ".join(dict.fromkeys(group["request_status"].astype(str))),
            "start": group["start"].min(),
            "end": group["end"].max(),
            "pre_ctg_lodf_pct": 100.0 * float(full.loc[m, list(bs)].abs().max()),
            "individual_mw": direction * (post_k - base_post),
            "in_combination_mw": direction * (post_all - without_k),
        })
    impacts = pd.DataFrame(rows)
    if impacts.empty:
        return summary, impacts
    if limit_mw:
        impacts["individual_pct"] = 100.0 * impacts["individual_mw"] / limit_mw
        impacts["in_combination_pct"] = 100.0 * impacts["in_combination_mw"] / limit_mw
    impacts["check"] = ((impacts["in_combination_mw"].abs() > 2 * impacts["individual_mw"].abs()
                         + 0.1 * (limit_mw or 100.0)))
    impacts = impacts.reindex(impacts["individual_mw"].abs().sort_values(ascending=False).index).reset_index(drop=True)
    return summary, impacts


# --- TIMING ---

def interval_shadow_prices(engine, market: str, constraint_id: int, day: datetime.date) -> pd.DataFrame:
    """5-minute RT shadow prices for the constraint on ``day``: [time, shadow_price]
    (``time`` is the interval start, market time)."""
    with engine.connect() as conn:
        return pd.read_sql(text(f"""
            SELECT date_time_equivalent AS time, SUM(shadow_price) AS shadow_price
            FROM {market}_rt_interval_constraint_shadow_prices
            WHERE dt = :d AND official_constraint_id = :c
            GROUP BY date_time_equivalent ORDER BY date_time_equivalent
        """), conn, params={"d": day, "c": constraint_id})


def binding_spells(intervals: pd.DataFrame, gap_minutes: int = 15) -> list[tuple]:
    """(first interval, last interval, total $) for each run of binding intervals, allowing
    gaps of up to ``gap_minutes``."""
    if intervals.empty:
        return []
    times = intervals[intervals["shadow_price"] != 0].sort_values("time")
    spells, cur = [], None
    for _, r in times.iterrows():
        if cur and (r["time"] - cur[1]) <= pd.Timedelta(minutes=gap_minutes):
            cur = (cur[0], r["time"], cur[2] + r["shadow_price"])
        else:
            if cur:
                spells.append(cur)
            cur = (r["time"], r["time"], r["shadow_price"])
    if cur:
        spells.append(cur)
    return spells


def active_events(engine, market: str, start: datetime.datetime, end: datetime.datetime) -> pd.DataFrame:
    """Outages in the active table that went out or came back between ``start`` and ``end``:
    [time, event, outage_equipment_id, kv, name]."""
    with engine.connect() as conn:
        df = pd.read_sql(text(f"""
            SELECT actual_start AS time, 'out' AS event, outage_equipment_id, kv, {_active_name_sql(market)} AS name
            FROM {market}_active_outages WHERE actual_start BETWEEN :s AND :e
            UNION ALL
            SELECT actual_end AS time, 'back' AS event, outage_equipment_id, kv, {_active_name_sql(market)} AS name
            FROM {market}_active_outages WHERE actual_end BETWEEN :s AND :e
        """), conn, params={"s": start, "e": end})
    return df.sort_values("time").reset_index(drop=True)


def event_lineup(intervals: pd.DataFrame, events: pd.DataFrame, minutes: int = 15) -> pd.DataFrame:
    """Adds the average 5-minute shadow price in the ``minutes`` before and after each event,
    and ``lines_up``: an outage starting as binding starts or rises, or ending as it stops or
    falls (a change of at least $10 and 25%)."""
    if events.empty:
        return events.assign(before=[], after=[], lines_up=[])
    sp = intervals.set_index("time")["shadow_price"] if not intervals.empty else pd.Series(dtype=float)
    before, after = [], []
    for t in events["time"]:
        window_before = sp[(sp.index >= t - pd.Timedelta(minutes=minutes)) & (sp.index < t)]
        window_after = sp[(sp.index >= t) & (sp.index < t + pd.Timedelta(minutes=minutes))]
        before.append(float(window_before.mean()) if len(window_before) else 0.0)
        after.append(float(window_after.mean()) if len(window_after) else 0.0)
    df = events.assign(before=before, after=after)
    change = df["after"] - df["before"]
    big = (change.abs() >= 10) & (change.abs() >= 0.25 * df[["before", "after"]].abs().max(axis=1))
    df["lines_up"] = big & (((df["event"] == "out") & (change > 0)) | ((df["event"] == "back") & (change < 0)))
    return df


# --- ENERGYCORE MATCHING AND DESK NOTES ---

def tioscore_matches(engine, market: str, me_id: int | None, day: datetime.date, equipment_ids,
                     warn=_no_warn) -> dict[str, pd.DataFrame]:
    """What EnergyCore and the desk already linked to this monitored element and outages:

    - ``detections``: shadow-outage detection results for the element (trade dates from 3
      days before to 1 day after ``day``), best score first
    - ``alerts``: daily monitored element alert details (new outages that match past outages
      that coincided with shadow prices on this element)
    - ``annotations``: outage annotations linking outages to this element
    - ``flags``: the desk's transmission outage flags and notes on the given outages
    """
    out = {k: pd.DataFrame() for k in ("detections", "alerts", "annotations", "flags")}
    ids = sorted({int(i) for i in equipment_ids if pd.notna(i)})
    with engine.connect() as conn:
        if me_id is not None:
            try:
                out["detections"] = pd.read_sql(text(f"""
                    SELECT s.trade_dt, s.overall_score, s.temporal_lineup_score, s.outage_combination,
                           s.avg_rt_shadow, s.last_rt_shadow, s.planned_start, s.planned_end,
                           GROUP_CONCAT(DISTINCT a.match_strings SEPARATOR ' ') AS match_strings
                    FROM {market}_shadow_outage_detection_shadows s
                    LEFT JOIN {market}_shadow_outage_detection_annotations a ON a.shadow_outage_detection_shadow_id = s.id
                    WHERE s.shadow_collection_type LIKE '%OfficialMonitoredElement' AND s.shadow_collection_id = :m
                      AND s.trade_dt BETWEEN :d0 AND :d1
                    GROUP BY s.id ORDER BY s.overall_score DESC LIMIT 15
                """), conn, params={"m": me_id, "d0": day - datetime.timedelta(days=3),
                                    "d1": day + datetime.timedelta(days=1)})
            except Exception as e:
                warn(f"Could not load shadow-outage detections: {e}")
            try:
                out["alerts"] = pd.read_sql(text(f"""
                    SELECT for_date, outage_market_urn, new_outage_equipment_id, new_outage_element_string, match_type,
                           match_strength, new_outage_start_dtime, new_outage_end_dtime,
                           past_outage_start_dtime, past_outage_end_dtime, past_outage_period_total_rt_shadow
                    FROM {market}_monelem_alert_details
                    WHERE monitored_element_id = :m AND for_date BETWEEN :d0 AND :d1
                    ORDER BY past_outage_period_total_rt_shadow DESC LIMIT 15
                """), conn, params={"m": me_id, "d0": day - datetime.timedelta(days=1),
                                    "d1": day + datetime.timedelta(days=1)})
            except Exception as e:
                warn(f"Could not load monitored element alerts: {e}")
            try:
                out["annotations"] = pd.read_sql(text(f"""
                    SELECT a.id AS annotation_id, a.name, o.outage_id AS outage_equipment_id, o.start_dt, o.stop_dt,
                           a.updated_at, LEFT(a.notes, 300) AS notes
                    FROM {market}_outage_annotations a
                    JOIN {market}_outage_annotation_monelems am ON am.annotation_id = a.id
                    JOIN {market}_outage_annotation_outages o ON o.annotation_id = a.id
                    WHERE a.archived = 0 AND am.official_monitored_element_id = :m
                    ORDER BY a.updated_at DESC LIMIT 15
                """), conn, params={"m": me_id})
            except Exception as e:
                warn(f"Could not load outage annotations: {e}")
        if ids:
            placeholders = ", ".join(f":e{i}" for i in range(len(ids)))
            try:
                out["flags"] = pd.read_sql(text(f"""
                    SELECT outage_id AS outage_equipment_id, flag_type, updated_at, LEFT(notes, 300) AS notes
                    FROM {market}_transmission_outage_flags
                    WHERE outage_type LIKE '%OutageEquipment' AND outage_id IN ({placeholders})
                      AND notes IS NOT NULL AND notes <> ''
                    ORDER BY updated_at DESC
                """), conn, params={f"e{i}": v for i, v in enumerate(ids)})
                out["flags"] = out["flags"].drop_duplicates(["outage_equipment_id", "notes"])
            except Exception as e:
                warn(f"Could not load outage flags: {e}")
    for key in ("annotations", "flags"):
        if not out[key].empty and "notes" in out[key]:
            out[key]["notes"] = out[key]["notes"].fillna("").map(_flatten_note)
    return out


def _flatten_note(note: str) -> str:
    """One-line note text: markdown links reduced to their text, whitespace collapsed."""
    note = re.sub(r"\]\(https?://[^)]+\)", "]", note)
    return " ".join(note.split())
