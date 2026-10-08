"""Transmission outages planned to start on a given day, for the MCP server.

Which outages count is market specific. Rather than keep a second copy of those
rules here, this module reads the same ``regression_ignore_rules`` market
parameter that EnergyCore's ``exclude_ignored_outages`` scope uses (see
``AnyMarketTransmissionOutageManager#regression_exclusion_arel`` in tios-core),
so the list changes when the desk changes the rules there.
"""

import datetime

import pandas as pd
import yaml
from sqlalchemy import text

from lib import outage_search as osearch

IGNORE_RULES_PARAM = "regression_ignore_rules"

# Markets whose transmission outage class defines regression_ignore_outages in
# EnergyCore: individual (equipment, request) pairs the desk has told the
# regressions to skip. The other markets return nil there.
MARKETS_WITH_IGNORED_OUTAGE_TABLE = {"caiso"}


def _no_warn(_message):
    pass


def ignore_rules(engine, market: str) -> dict:
    """The market's regression_ignore_rules as {column: value}, or {} when unset.

    EnergyCore stores the rules as Ruby YAML with symbol keys (``:request_status:``),
    which Python reads as ``":request_status"``, so the leading colon is dropped.
    """
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT p.value
            FROM market_parameters p
            JOIN markets m ON m.id = p.market_id
            WHERE m.urn = :market AND p.name = :name
            ORDER BY p.id
            LIMIT 1
        """), {"market": market, "name": IGNORE_RULES_PARAM}).fetchone()

    if row is None or row[0] is None:
        return {}
    rules = yaml.safe_load(row[0]) or {}
    return {str(k).lstrip(":"): v for k, v in rules.items()}


def _exclusion_sql(rules: dict, columns: set, table_alias: str, warn=_no_warn) -> tuple[list[str], dict]:
    """WHERE fragments equivalent to EnergyCore's regression_exclusion_arel.

    Per column: a list with any '%' value becomes NOT LIKE / != for each value; a
    plain list becomes NOT IN; a single value becomes NOT LIKE (with '%') or !=.
    Like the Rails version, these comparisons also drop rows where the column is NULL.
    """
    clauses = []
    params = {}
    for n, (attr, val) in enumerate(rules.items()):
        if attr not in columns:
            warn(f"Ignore rule on unknown column '{attr}' was skipped.")
            continue
        col = f"{table_alias}.`{attr}`"

        def bind(v, i, _n=n):
            key = f"r{_n}_{i}"
            params[key] = v
            return f":{key}"

        if isinstance(val, list):
            if any(isinstance(v, str) and "%" in v for v in val):
                for i, v in enumerate(val):
                    op = "NOT LIKE" if isinstance(v, str) and "%" in v else "<>"
                    clauses.append(f"{col} {op} {bind(v, i)}")
            else:
                clauses.append(f"{col} NOT IN ({', '.join(bind(v, i) for i, v in enumerate(val))})")
        elif isinstance(val, str) and "%" in val:
            clauses.append(f"{col} NOT LIKE {bind(val, 0)}")
        else:
            clauses.append(f"{col} <> {bind(val, 0)}")
    return clauses, params


def _table_columns(conn, table: str) -> set:
    return {r[0] for r in conn.execute(text(f"SHOW COLUMNS FROM {table}")).fetchall()}


def list_upcoming_outages(engine, market: str, target_date: datetime.date, apply_ignore_rules: bool = True,
                          warn=_no_warn) -> list[dict]:
    """Transmission outages on the current report (most_recent = 1) with planned_start on
    ``target_date``, one entry per equipment ID, ordered by planned start.

    ``target_date`` is compared with planned_start as stored, which is market local time.
    """
    if market not in osearch.MARKETS:
        raise ValueError(f"Unknown market '{market}'. Use one of: {', '.join(osearch.MARKETS)}")

    table = f"{market}_transmission_outages"
    name_cols = [c for c in osearch.OUTAGE_NAME_COLUMNS[market] if c not in ("kv",)]
    select_cols = ["outage_equipment_id", "outage_request_id", "request_status", "planned_start", "planned_end",
                   "equipment_type", "kv"] + name_cols

    where = ["t.most_recent = 1", "t.planned_start >= :day_start", "t.planned_start < :day_end"]
    params = {"day_start": target_date, "day_end": target_date + datetime.timedelta(days=1)}

    with engine.connect() as conn:
        if apply_ignore_rules:
            columns = _table_columns(conn, table)
            clauses, rule_params = _exclusion_sql(ignore_rules(engine, market), columns, "t", warn)
            where += clauses
            params.update(rule_params)

            if market in MARKETS_WITH_IGNORED_OUTAGE_TABLE:
                where.append(f"""NOT EXISTS (
                    SELECT 1 FROM {market}_regression_ignore_outages ri
                    WHERE ri.outage_equipment_id = t.outage_equipment_id
                      AND ri.outage_request_id = t.outage_request_id)""")

        rows = conn.execute(text(f"""
            SELECT {", ".join(f"t.`{c}`" for c in select_cols)}
            FROM {table} t
            WHERE {" AND ".join(where)}
            ORDER BY t.planned_start, t.outage_equipment_id, t.outage_request_id
        """), params).mappings().fetchall()

    outages = {}
    for row in rows:
        eq_id = str(row["outage_equipment_id"])
        entry = outages.get(eq_id)
        if entry is None:
            entry = outages[eq_id] = {
                "outage_equipment_id": eq_id,
                "name": osearch.format_outage_name(market, row),
                "equipment_type": row["equipment_type"] or "",
                "kv": row["kv"],
                "planned_start": row["planned_start"],
                "planned_end": row["planned_end"],
                "request_ids": [],
                "request_statuses": [],
            }
        else:
            # Rows are ordered by planned_start, so the first one already has the earliest start.
            ends = [e for e in (entry["planned_end"], row["planned_end"]) if e is not None]
            entry["planned_end"] = max(ends) if ends else None
        if row["outage_request_id"] not in entry["request_ids"]:
            entry["request_ids"].append(row["outage_request_id"])
        if row["request_status"] not in entry["request_statuses"]:
            entry["request_statuses"].append(row["request_status"])

    return list(outages.values())


def add_group_ids(engine, market: str, outages: list[dict], warn=_no_warn) -> None:
    """Adds 'group_ids': the outage equipment groups each outage belongs to."""
    for o in outages:
        o["group_ids"] = []
    if not outages:
        return
    by_id = {o["outage_equipment_id"]: o for o in outages}
    ids = list(by_id)
    placeholders = ", ".join(f":e{i}" for i in range(len(ids)))
    try:
        with engine.connect() as conn:
            res = conn.execute(text(f"""
                SELECT DISTINCT outage_equipment_id, outage_equipment_group_id
                FROM {market}_outage_equipment_group_members
                WHERE outage_equipment_id IN ({placeholders})
                ORDER BY outage_equipment_group_id
            """), {f"e{i}": eq for i, eq in enumerate(ids)}).fetchall()
        for eq_id, group_id in res:
            by_id[str(eq_id)]["group_ids"].append(str(group_id))
    except Exception as e:
        warn(f"Could not fetch outage equipment groups for {market.upper()}: {e}")


def add_history(engine, market: str, outages: list[dict], notes_df: pd.DataFrame, start_date_str,
                warn=_no_warn) -> None:
    """Adds what the outage documentation would find for each outage, so empty ones can be skipped:

    - 'note_matches': quick notes found by the same equipment-ID search as the outage search page
    - 'annotations': non-archived outage annotations
    - 'flag_type': the latest transmission outage flag type ('' if none; PJM has no flags)
    """
    if not outages:
        return
    ids = [o["outage_equipment_id"] for o in outages]
    placeholders = ", ".join(f":e{i}" for i in range(len(ids)))
    params = {f"e{i}": eq for i, eq in enumerate(ids)}

    annotation_counts = {}
    try:
        with engine.connect() as conn:
            res = conn.execute(text(f"""
                SELECT o.outage_id, COUNT(DISTINCT a.id)
                FROM {market}_outage_annotations a
                JOIN {market}_outage_annotation_outages o ON a.id = o.annotation_id
                WHERE a.archived = 0
                  AND o.outage_id IN ({placeholders})
                  AND (o.outage_type = :outage_type_exact OR LOWER(o.outage_type) LIKE :outage_type_like)
                  AND EXISTS (SELECT 1 FROM {market}_outage_annotation_monelems m WHERE m.annotation_id = a.id)
                GROUP BY o.outage_id
            """), {**params, "outage_type_exact": f"{market.capitalize()}OutageEquipment",
                   "outage_type_like": f"%{market}%"}).fetchall()
        annotation_counts = {str(r[0]): r[1] for r in res}
    except Exception as e:
        warn(f"Could not count outage annotations for {market.upper()}: {e}")

    flags = {f["outage_id"]: f for f in osearch.fetch_transmission_outage_flags(engine, market, ids, warn=warn)}

    for o in outages:
        eq_id = o["outage_equipment_id"]
        search_fields, _, _ = osearch.build_search_fields(engine, "Equipment ID", eq_id, market, market, warn=warn)
        o["note_matches"] = len(osearch.filter_notes(notes_df, search_fields, "All", start_date_str)) \
            if search_fields else 0
        o["annotations"] = annotation_counts.get(eq_id, 0)
        o["flag_type"] = flags[eq_id]["flag_type"] or "flag" if eq_id in flags else ""
