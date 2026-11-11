"""Every time an outage was out of service, from both of the ISO's outage tables, for the MCP server.

- ``{market}_active_outages`` (act): the ISO's real-time list of what is out, one row per
  out-of-service spell with the actual start and end EnergyCore deduced from it.
- ``{market}_transmission_outages`` (sched): outage requests with planned and reported actual
  times; ``most_recent = 1`` is the current version of each request.

Rows from the two tables are never merged: each row lists the other table's rows whose times
overlap it. Both tables hold naive market-local times (MISO: EST all year). An active row's
end is the first snapshot (about every 15 minutes) the line was missing from, so it can be a
snapshot late; it is NULL while the line is still out. Like EnergyCore, only markets with
supports_active_outage_tools (MISO) use the active table.
Desk flags are matched per record: ``outage_source`` is the request ID for a
transmission-table row and the active row's ``outage_request_id`` hash for an active one.
"""

import datetime

import pandas as pd
from sqlalchemy import text

from lib import autoflow
from lib import flags as flags_lib
from lib import long_lodf
from lib import outage_groups
from lib import outage_search as osearch
from lib import upcoming_outages

TABLES = ("comb", "act", "sched")
OPEN_END = pd.Timestamp("2200-01-01")

# Columns of the active table that hold a name, per market (the others have only IDs).
ACTIVE_NAME_COLUMNS = {"miso": ["kv", "from_station", "to_station"], "pjm": ["b1", "b3", "kv"]}


def _no_warn(_message):
    pass


def _in(prefix: str, values) -> tuple[str, dict]:
    values = list(values)
    return ", ".join(f":{prefix}{i}" for i in range(len(values))), {f"{prefix}{i}": v for i, v in enumerate(values)}


def expand_ids(engine, market: str, outage_id: str, id_type: str, include_groups: bool) -> tuple[list[int], list[int]]:
    """(equipment IDs, group IDs) to search: a group's members and its associated groups' members,
    or an equipment ID plus, with include_groups, every member of its groups."""
    oid = int(osearch.clean_id(str(outage_id)) or 0)
    if not oid:
        raise ValueError(f"No ID found in {outage_id!r}.")
    if id_type == "group":
        groups = [oid]
    elif include_groups:
        groups = long_lodf.groups_for_equipment(engine, market, [oid]).get(oid, [])
    else:
        return [oid], []
    members = {oid} if id_type == "equipment" else set()
    for g in groups:
        members |= set(long_lodf.group_members_for_shadows(engine, market, g))
    return sorted(members), groups


def _window(start: datetime.date | None, end: datetime.date | None) -> tuple[list[str], list[str], dict]:
    """WHERE fragments for (active, transmission) rows out at some time in [start, end]: actual
    times, or planned times for a request that has not started."""
    act, sched, params = [], [], {}
    if start:
        params["w_start"] = start
        act.append("(a.actual_end IS NULL OR a.actual_end >= :w_start)")
        sched.append("COALESCE(t.actual_end, t.planned_end, '2200-01-01') >= :w_start")
    if end:
        params["w_end"] = end + datetime.timedelta(days=1)
        act.append("a.actual_start < :w_end")
        sched.append("COALESCE(t.actual_start, t.planned_start) < :w_end")
    return act, sched, params


def active_rows(engine, market: str, ids, start, end) -> pd.DataFrame:
    if not ids or not long_lodf.supports_active_outages(engine, market):
        return pd.DataFrame()
    name_cols = ACTIVE_NAME_COLUMNS.get(market, [])
    where, _, params = _window(start, end)
    ph, p = _in("e", ids)
    with engine.connect() as conn:
        df = pd.read_sql(text(f"""
            SELECT a.id AS record_id, a.outage_equipment_id AS equipment_id, a.outage_request_id AS flag_key,
                   a.actual_start, a.actual_end, a.outage_equipment_identifier
                   {"".join(f", a.`{c}`" for c in name_cols)}
            FROM {market}_active_outages a
            WHERE a.outage_equipment_id IN ({ph}) AND a.actual_start IS NOT NULL {"".join(f" AND {w}" for w in where)}
        """), conn, params={**params, **p})
    if df.empty:
        return df
    df["name"] = [osearch.format_outage_name(market, r) if name_cols else "" for r in df.to_dict("records")]
    df["name"] = df["name"].where(df["name"] != "", df["outage_equipment_identifier"].fillna(""))
    df["table"] = "act"
    df["record_id"] = df["record_id"].astype(str)
    return df.drop(columns=[c for c in name_cols if c != "kv"] + ["outage_equipment_identifier"])


def transmission_rows(engine, market: str, ids, start, end, warn=_no_warn) -> pd.DataFrame:
    """Current versions (most_recent = 1) of the equipment's requests, with ``ignored`` set
    where the market's regression ignore rules drop the request."""
    if not ids:
        return pd.DataFrame()
    table = f"{market}_transmission_outages"
    name_cols = osearch.OUTAGE_NAME_COLUMNS[market]
    _, where, params = _window(start, end)
    ph, p = _in("e", ids)
    with engine.connect() as conn:
        clauses, rule_params = upcoming_outages._exclusion_sql(
            upcoming_outages.ignore_rules(engine, market), upcoming_outages._table_columns(conn, table), "t", warn)
        if market in upcoming_outages.MARKETS_WITH_IGNORED_OUTAGE_TABLE:
            clauses.append(f"""NOT EXISTS (SELECT 1 FROM {market}_regression_ignore_outages ri
                               WHERE ri.outage_equipment_id = t.outage_equipment_id
                                 AND ri.outage_request_id = t.outage_request_id)""")
        ignored = f"CASE WHEN {' AND '.join(f'({c})' for c in clauses)} THEN 0 ELSE 1 END" if clauses else "0"
        df = pd.read_sql(text(f"""
            SELECT t.outage_request_id AS record_id, t.outage_equipment_id AS equipment_id, t.request_status,
                   t.planned_start, t.planned_end, t.actual_start, t.actual_end, t.equipment_type,
                   t.last_update_for_date, {ignored} AS ignored
                   {"".join(f", t.`{c}`" for c in name_cols if c != "kv")}, t.kv
            FROM {table} t
            WHERE t.outage_equipment_id IN ({ph}) AND t.most_recent = 1 {"".join(f" AND {w}" for w in where)}
        """), conn, params={**params, **rule_params, **p})
    if df.empty:
        return df
    df["name"] = [osearch.format_outage_name(market, r) for r in df.to_dict("records")]
    df["table"] = "sched"
    df["record_id"] = df["record_id"].astype(str)
    df["flag_key"] = df["record_id"]
    return df.drop(columns=[c for c in name_cols if c != "kv"])


def record_flags(engine, market: str, ids, warn=_no_warn) -> pd.DataFrame:
    """Flags on the equipment's records from every market's flag table, newest per record."""
    frames = []
    ph, p = _in("e", ids)
    for flag_market in flags_lib.flag_tables(engine):
        try:
            with engine.connect() as conn:
                df = pd.read_sql(text(f"""
                    SELECT outage_id AS equipment_id, outage_source AS flag_key, flag_type, notes, user_id,
                           updated_at AS flag_updated
                    FROM {flag_market}_transmission_outage_flags
                    WHERE outage_type = :otype AND outage_id IN ({ph})
                """), conn, params={"otype": flags_lib.outage_type(market), **p})
        except Exception as e:
            warn(f"Could not read {flag_market.upper()} outage flags: {e}")
            continue
        if not df.empty:
            frames.append(df.assign(flagged_in=flag_market))
    if not frames:
        return pd.DataFrame(columns=["equipment_id", "flag_key", "flag_type", "notes", "user_id", "flag_updated",
                                     "flagged_in"])
    df = pd.concat(frames, ignore_index=True).sort_values("flag_updated", ascending=False)
    df["equipment_id"] = df["equipment_id"].astype(int)
    return df.drop_duplicates(["equipment_id", "flag_key"])


def equipment_info(engine, market: str, ids) -> dict[int, dict]:
    """Latest transmission-table name, type and kV per equipment ID (active rows lack them)."""
    if not ids:
        return {}
    name_cols = osearch.OUTAGE_NAME_COLUMNS[market]
    ph, p = _in("e", ids)
    with engine.connect() as conn:
        df = pd.read_sql(text(f"""
            SELECT t.outage_equipment_id AS equipment_id, t.equipment_type
                   {"".join(f", t.`{c}`" for c in name_cols)}
            FROM {market}_transmission_outages t
            JOIN (SELECT outage_equipment_id, MAX(id) AS id FROM {market}_transmission_outages
                  WHERE outage_equipment_id IN ({ph}) GROUP BY outage_equipment_id) latest ON latest.id = t.id
        """), conn, params=p)
    return {int(r["equipment_id"]): {"name": osearch.format_outage_name(market, r), "equipment_type": r["equipment_type"],
                                     "kv": r.get("kv")} for r in df.to_dict("records")}


def _interval(row) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    start, end = row["actual_start"], row["actual_end"]
    if pd.isna(start):
        start, end = row.get("planned_start"), row.get("planned_end")
    if start is None or pd.isna(start):
        return None
    return pd.Timestamp(start), (OPEN_END if end is None or pd.isna(end) else pd.Timestamp(end))


def _minutes(a, b) -> str:
    if a == OPEN_END or b == OPEN_END:
        return "open"
    m = round((b - a).total_seconds() / 60)
    return f"{m:+d}m"


def add_overlaps(df: pd.DataFrame) -> pd.DataFrame:
    """``overlaps``: the other table's records whose times overlap the row, with the start and
    end difference in minutes (theirs minus this row's); "none" when there are none."""
    spans = [_interval(r) for r in df.to_dict("records")]
    out = []
    for i, (tbl, span) in enumerate(zip(df["table"], spans)):
        if span is None:
            out.append("none")
            continue
        hits = []
        for j, (other_tbl, other) in enumerate(zip(df["table"], spans)):
            if other_tbl == tbl or other is None or not (span[0] < other[1] and other[0] < span[1]):
                continue
            hits.append(f"{other_tbl} {df['record_id'].iat[j]} (start {_minutes(span[0], other[0])}, "
                        f"end {_minutes(span[1], other[1])})")
        out.append("; ".join(hits) or "none")
    return df.assign(overlaps=out)


def add_state(df: pd.DataFrame, now: datetime.datetime) -> pd.DataFrame:
    """completed, in progress, planned, not started (planned dates passed), or ignored; and
    duration_hr from actual times (planned for a request that has not started)."""
    states, hours = [], []
    for r in df.to_dict("records"):
        started, ended = pd.notna(r["actual_start"]), pd.notna(r["actual_end"])
        if r.get("ignored") == 1:
            state = "ignored"
        elif started:
            state = "completed" if ended and pd.Timestamp(r["actual_end"]) <= pd.Timestamp(now) else "in progress"
        elif pd.notna(r.get("planned_end")) and pd.Timestamp(r["planned_end"]) < pd.Timestamp(now):
            state = "not started (planned dates passed)"
        else:
            state = "planned"
        states.append(state)
        s, e = (r["actual_start"], r["actual_end"]) if started else (r.get("planned_start"), r.get("planned_end"))
        if s is None or pd.isna(s):
            hours.append("")
        elif e is None or pd.isna(e):
            hours.append(f"{(pd.Timestamp(now) - pd.Timestamp(s)).total_seconds() / 3600:.1f} so far" if started else "")
        else:
            h = f"{(pd.Timestamp(e) - pd.Timestamp(s)).total_seconds() / 3600:.1f}"
            hours.append(h if started else f"{h} planned")
    return df.assign(state=states, duration_hr=hours)


def time_zone_label(engine, market: str) -> str:
    """The zone the outage tables' times are in, for the header."""
    with engine.connect() as conn:
        row = conn.execute(text("SELECT time_zone FROM markets WHERE urn = :u"), {"u": market}).fetchone()
    name = row[0] if row else None
    if name == "Etc/GMT+5":
        return ("MISO time: EST, UTC-5 all year (no daylight saving). Report them as given, labeled "
                "\"EST (MISO time)\", not converted to Central.")
    zone = autoflow.RAILS_TIME_ZONES.get(name, name) if name else "unknown"
    return f"{market.upper()} market time ({zone}, with daylight saving); report as given, labeled with the zone."


def record_annotations(engine, market: str, ids, warn=_no_warn) -> pd.DataFrame:
    """Non-archived annotation links to the equipment, each with its own window."""
    ph, p = _in("e", ids)
    try:
        with engine.connect() as conn:
            return pd.read_sql(text(f"""
                SELECT o.outage_id AS equipment_id, o.start_dt, o.stop_dt, a.name,
                       m.official_monitored_element_id AS me_id
                FROM {market}_outage_annotation_outages o
                JOIN {market}_outage_annotations a ON a.id = o.annotation_id AND a.archived = 0
                JOIN {market}_outage_annotation_monelems m ON m.annotation_id = a.id
                WHERE o.outage_type = :otype AND o.outage_id IN ({ph})
            """), conn, params={"otype": flags_lib.outage_type(market), **p})
    except Exception as e:
        warn(f"Could not read {market.upper()} outage annotations: {e}")
        return pd.DataFrame()


def add_annotations(df: pd.DataFrame, ann: pd.DataFrame) -> pd.DataFrame:
    """``annotations``: monitored elements annotated on any searched equipment with a window
    overlapping the row's (the group's members are the same line, often under other IDs)."""
    out = []
    for r in df.to_dict("records"):
        span = _interval(r)
        hits = []
        if span is not None and not ann.empty:
            for a in ann.to_dict("records"):
                a_start = pd.Timestamp(a["start_dt"])
                a_end = OPEN_END if pd.isna(a["stop_dt"]) else pd.Timestamp(a["stop_dt"])
                if span[0] < a_end and a_start < span[1]:
                    hits.append(f"ME {a['me_id']} ({' '.join(str(a['name'] or '').split())})")
        out.append(", ".join(dict.fromkeys(hits)))
    return df.assign(annotations=out)


def history(engine, market: str, ids: list[int], table: str, start, end, apply_ignore_rules: bool,
            include_planned: bool, now: datetime.datetime, warn=_no_warn) -> tuple[pd.DataFrame, dict]:
    """All rows, newest first, and counts for the header."""
    frames = []
    if table in ("comb", "act"):
        frames.append(active_rows(engine, market, ids, start, end))
    if table in ("comb", "sched"):
        frames.append(transmission_rows(engine, market, ids, start, end, warn))
    frames = [f for f in frames if not f.empty]
    counts = {"ignored_dropped": 0, "planned_dropped": 0}
    if not frames:
        return pd.DataFrame(), counts
    df = pd.concat(frames, ignore_index=True)
    for col in ("request_status", "planned_start", "planned_end", "equipment_type", "kv", "ignored"):
        if col not in df.columns:
            df[col] = None
    df["equipment_id"] = df["equipment_id"].astype(int)

    info = equipment_info(engine, market, sorted(set(df["equipment_id"])))
    for col in ("equipment_type", "kv"):
        df[col] = [v if v is not None and not (isinstance(v, float) and pd.isna(v))
                   else info.get(e, {}).get(col) for e, v in zip(df["equipment_id"], df[col])]
    names = outage_groups.equipment_names(engine, market, sorted(set(df["equipment_id"])), warn=warn)
    df["name"] = [names.get(str(e)) or n or info.get(e, {}).get("name", "")
                  for e, n in zip(df["equipment_id"], df["name"])]

    df = add_state(df, now)
    if apply_ignore_rules:
        counts["ignored_dropped"] = int((df["state"] == "ignored").sum())
        df = df[df["state"] != "ignored"]
    if not include_planned:
        counts["planned_dropped"] = int((df["state"] == "planned").sum())
        df = df[df["state"] != "planned"]
    df = df.reset_index(drop=True)
    df = add_overlaps(df) if table == "comb" else df.assign(overlaps="(other table not searched)")

    df = add_annotations(df, record_annotations(engine, market, ids, warn))
    flags = record_flags(engine, market, sorted(set(df["equipment_id"])), warn)
    df = df.merge(flags, on=["equipment_id", "flag_key"], how="left")
    df["sort_time"] = [s if pd.notna(s) else p for s, p in zip(df["actual_start"], df["planned_start"])]
    return df.sort_values("sort_time", ascending=False).reset_index(drop=True), counts
