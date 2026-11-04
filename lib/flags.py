"""Transmission outages the desk has flagged (alert / watch / safe), for the MCP server.

Flags are in ``{market}_transmission_outage_flags``, one row per (outage_type, equipment ID,
outage request) -- ``outage_id`` is the equipment ID and ``outage_source`` the request ID.
The table is the market of the page the flag was set on, and ``outage_type`` is the outage's
class (``MisoOutageEquipment``, ``PjmOutageEquipment``, ...), so a MISO desk can flag a PJM
outage; most flags are in the outage's own market.

Flag types (``AnyMarketTransmissionOutageFlagManager``): the desk's ``alert``, ``watch``,
``safe``; the automatic ``auto_alert``, ``auto_watch``, ``auto_safe``; and '' (a note with no
flag). EnergyCore colours alert and auto_alert red, watch orange, safe green. Its outage
search filter takes flag names plus "manual-only", "auto-only", or both (the default), the
same as ``flag_type_values`` here.
"""

import datetime

import pandas as pd
from sqlalchemy import text

from lib import outage_search as osearch
from lib import upcoming_outages

FLAG_NAMES = ["alert", "watch", "safe"]
FLAG_SOURCES = ["both", "manual", "auto"]


def _no_warn(_message):
    pass


def flag_type_values(flags, source: str = "both") -> list[str]:
    """flag_type values for flag names, as EnergyCore's outage search filter builds them."""
    names = [f.strip().lower() for f in flags]
    for n in names:
        if n not in FLAG_NAMES:
            raise ValueError(f"Unknown flag '{n}'. Use one of: {', '.join(FLAG_NAMES)}")
    if source not in FLAG_SOURCES:
        raise ValueError(f"Unknown flag source '{source}'. Use one of: {', '.join(FLAG_SOURCES)}")
    return [v for n in names for v in ([n] if source == "manual" else [f"auto_{n}"] if source == "auto"
                                       else [n, f"auto_{n}"])]


def outage_type(market: str) -> str:
    return f"{market.capitalize()}OutageEquipment"


def flag_tables(engine) -> list[str]:
    """Markets that have a transmission outage flag table."""
    with engine.connect() as conn:
        names = {r[0] for r in conn.execute(text("SHOW TABLES LIKE '%\\_transmission\\_outage\\_flags'"))}
    return [m for m in osearch.MARKETS if f"{m}_transmission_outage_flags" in names]


def flagged_outages(engine, market: str, flag_types: list[str], start: datetime.date, end: datetime.date | None,
                    apply_ignore_rules: bool = True, warn=_no_warn) -> pd.DataFrame:
    """Flagged transmission outages of ``market`` on the current report (most_recent = 1) that
    are out at some time from ``start`` through ``end`` (no end: still out on ``start`` or
    later), from every market's flag table. One row per flagged (equipment, request).

    Columns: equipment_id, request_id, request_status, planned_start, planned_end, actual_start,
    actual_end, equipment_type, kv, name, flag_type, notes, flag_updated_at, note_updated_at,
    user_id, flagged_in.
    """
    table = f"{market}_transmission_outages"
    name_cols = [c for c in osearch.OUTAGE_NAME_COLUMNS[market] if c != "kv"]
    cols = ["outage_equipment_id", "outage_request_id", "request_status", "planned_start", "planned_end",
            "actual_start", "actual_end", "equipment_type", "kv"] + name_cols
    type_placeholders = ", ".join(f":ft{i}" for i in range(len(flag_types)))
    params = {"start": start, "otype": outage_type(market), **{f"ft{i}": v for i, v in enumerate(flag_types)}}
    # The OR on the two end columns lets MySQL merge their indexes; the COALESCE is the real test.
    where = ["t.most_recent = 1", "(t.planned_end >= :start OR t.actual_end >= :start)",
             "COALESCE(t.actual_end, t.planned_end) >= :start", f"f.flag_type IN ({type_placeholders})"]
    if end is not None:
        where.append("COALESCE(t.actual_start, t.planned_start) < :end_excl")
        params["end_excl"] = end + datetime.timedelta(days=1)

    with engine.connect() as conn:
        if apply_ignore_rules:
            clauses, rule_params = upcoming_outages._exclusion_sql(
                upcoming_outages.ignore_rules(engine, market), upcoming_outages._table_columns(conn, table), "t", warn)
            where += clauses
            params.update(rule_params)
            if market in upcoming_outages.MARKETS_WITH_IGNORED_OUTAGE_TABLE:
                where.append(f"""NOT EXISTS (
                    SELECT 1 FROM {market}_regression_ignore_outages ri
                    WHERE ri.outage_equipment_id = t.outage_equipment_id
                      AND ri.outage_request_id = t.outage_request_id)""")

    frames = []
    for flag_market in flag_tables(engine):
        try:
            with engine.connect() as conn:
                df = pd.read_sql(text(f"""
                    SELECT {", ".join(f"t.`{c}`" for c in cols)}, f.flag_type, f.notes,
                           f.updated_at AS flag_updated_at, f.note_last_updated_at AS note_updated_at, f.user_id
                    FROM {table} t
                    STRAIGHT_JOIN {flag_market}_transmission_outage_flags f
                      ON f.outage_type = :otype AND f.outage_id = t.outage_equipment_id
                     AND f.outage_source = t.outage_request_id
                    WHERE {" AND ".join(where)}
                """), conn, params=params)
        except Exception as e:
            warn(f"Could not read {flag_market.upper()} outage flags: {e}")
            continue
        if not df.empty:
            frames.append(df.assign(flagged_in=flag_market))
    if not frames:
        return pd.DataFrame(columns=["equipment_id", "request_id", "request_status", "planned_start", "planned_end",
                                     "actual_start", "actual_end", "equipment_type", "kv", "name", "flag_type", "notes",
                                     "flag_updated_at", "note_updated_at", "user_id", "flagged_in"])
    columns = frames[0].columns
    df = (pd.concat([f.dropna(axis=1, how="all") for f in frames], ignore_index=True).reindex(columns=columns)
          if len(frames) > 1 else frames[0])
    df["name"] = [osearch.format_outage_name(market, r) for r in df.to_dict("records")]
    df = df.rename(columns={"outage_equipment_id": "equipment_id", "outage_request_id": "request_id"})
    df["equipment_id"] = df["equipment_id"].astype(str)
    df["request_id"] = df["request_id"].astype(str)
    return df.drop(columns=name_cols)


def user_names(engine, user_ids) -> dict[int, str]:
    ids = sorted({int(u) for u in user_ids if u is not None and not pd.isna(u)})
    if not ids:
        return {}
    placeholders = ", ".join(f":u{i}" for i in range(len(ids)))
    with engine.connect() as conn:
        rows = conn.execute(text(f"SELECT id, first_name, last_name, email FROM users WHERE id IN ({placeholders})"),
                            {f"u{i}": u for i, u in enumerate(ids)}).fetchall()
    return {r[0]: (" ".join(p for p in (r[1], r[2]) if p) or (r[3] or "").split("@")[0]) for r in rows}
