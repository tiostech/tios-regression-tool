"""Outage equipment groups, quicklinks between monitored elements and outages, and the quick
notes on monitored elements.

Used by ``mcp_server.py``. Nothing here imports Streamlit.

- Groups: ``{market}_outage_equipment_groups`` (the desk's "GID n"), their members in
  ``{market}_outage_equipment_group_members`` (``removed = 1`` rows are soft-removed and
  EnergyCore ignores them), and linked groups in ``{market}_outage_group_associations``.
- Quicklinks: ``{me_market}_outage_quicklinks`` is in the monitored element's market and is
  polymorphic: ``outage_type`` is an EnergyCore class name whose prefix is the outage's market
  (``MisoOutageEquipment``, ``MisoOutageEquipmentGroup``, ``PjmOutageEquipment``, ...), so a
  MISO outage can be quicklinked from SPP or PJM elements. For equipment, ``outage_source`` is
  the outage request ID the link was made for; groups have none.
- Notes: ``{market}_monelem_quick_notes``; ``deleted = 1`` rows are soft-deleted.
"""

import pandas as pd
from sqlalchemy import text

from lib import outage_search as osearch


def _no_warn(_message):
    pass


def _in(prefix: str, values) -> tuple[str, dict]:
    values = list(values)
    return ", ".join(f":{prefix}{i}" for i in range(len(values))), {f"{prefix}{i}": v for i, v in enumerate(values)}


def _class_prefix(market: str) -> str:
    return market.capitalize()


def equipment_names(engine, market: str, equipment_ids, warn=_no_warn) -> dict[str, str]:
    """{equipment ID: name} from {market}_outage_equipments, named the way the outage search names them."""
    ids = sorted({str(e) for e in equipment_ids if e is not None and str(e).strip()})
    cols = osearch.OUTAGE_NAME_COLUMNS.get(market)
    if not ids or not cols:
        return {}
    placeholders, params = _in("e", ids)
    try:
        with engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT id, {", ".join(f"`{c}`" for c in cols)} FROM {market}_outage_equipments
                WHERE id IN ({placeholders})
            """), params).mappings().fetchall()
        return {str(r["id"]): osearch.format_outage_name(market, r) for r in rows}
    except Exception as e:
        warn(f"Could not fetch {market.upper()} outage equipment names: {e}")
        return {}


def groups_for_equipment(engine, market: str, equipment_id: str) -> list[str]:
    """Groups the equipment is a (not removed) member of."""
    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT DISTINCT outage_equipment_group_id FROM {market}_outage_equipment_group_members
            WHERE outage_equipment_id = :e AND (removed = 0 OR removed IS NULL)
            ORDER BY outage_equipment_group_id
        """), {"e": equipment_id}).fetchall()
    return [str(r[0]) for r in rows]


def associations(engine, market: str, group_ids) -> pd.DataFrame:
    """Association rows touching these groups: group_id -> associated_group_id. The outage
    search follows them from group_id to associated_group_id."""
    ids = sorted({str(g) for g in group_ids})
    if not ids:
        return pd.DataFrame(columns=["group_id", "associated_group_id"])
    placeholders, params = _in("g", ids)
    with engine.connect() as conn:
        return pd.read_sql(text(f"""
            SELECT DISTINCT group_id, associated_group_id FROM {market}_outage_group_associations
            WHERE group_id IN ({placeholders}) OR associated_group_id IN ({placeholders})
        """), conn, params=params).astype(str)


def groups(engine, market: str, group_ids) -> pd.DataFrame:
    ids = sorted({str(g) for g in group_ids})
    if not ids:
        return pd.DataFrame()
    placeholders, params = _in("g", ids)
    with engine.connect() as conn:
        df = pd.read_sql(text(f"""
            SELECT id AS group_id, equipment_name, likely_equipment_type, likely_from_station, likely_to_station,
                   COALESCE(likely_from_kv, likely_to_kv) AS kv, basis_outage_id, `manual`
            FROM {market}_outage_equipment_groups WHERE id IN ({placeholders})
        """), conn, params=params)
    df["group_id"] = df["group_id"].astype(str)
    return df


def members(engine, market: str, group_ids, include_removed: bool = False) -> pd.DataFrame:
    ids = sorted({str(g) for g in group_ids})
    if not ids:
        return pd.DataFrame(columns=["group_id", "equipment_id", "score", "manual", "removed"])
    placeholders, params = _in("g", ids)
    removed = "" if include_removed else "AND (removed = 0 OR removed IS NULL)"
    with engine.connect() as conn:
        df = pd.read_sql(text(f"""
            SELECT outage_equipment_group_id AS group_id, outage_equipment_id AS equipment_id, score, `manual`, removed
            FROM {market}_outage_equipment_group_members
            WHERE outage_equipment_group_id IN ({placeholders}) {removed}
            ORDER BY outage_equipment_group_id, outage_equipment_id
        """), conn, params=params)
    df["group_id"] = df["group_id"].astype(str)
    df["equipment_id"] = df["equipment_id"].astype(str)
    return df


def _quicklink_markets(engine) -> list[str]:
    with engine.connect() as conn:
        names = {r[0] for r in conn.execute(text("SHOW TABLES LIKE '%\\_outage\\_quicklinks'"))}
    return [m for m in osearch.MARKETS if f"{m}_outage_quicklinks" in names]


def quicklinks(engine, outage_market: str, equipment_ids=(), group_ids=(), warn=_no_warn) -> pd.DataFrame:
    """Every quicklink, in any market's elements, to these outage equipment IDs and groups of
    ``outage_market``: me_market, me_id, outage_kind (equipment/group), outage_id,
    request_id, created_at, updated_at."""
    prefix = _class_prefix(outage_market)
    wanted = [(f"{prefix}OutageEquipment", "equipment", sorted({str(e) for e in equipment_ids})),
              (f"{prefix}OutageEquipmentGroup", "group", sorted({str(g) for g in group_ids}))]
    frames = []
    for me_market in _quicklink_markets(engine):
        for outage_type, kind, ids in wanted:
            if not ids:
                continue
            placeholders, params = _in("o", ids)
            try:
                with engine.connect() as conn:
                    df = pd.read_sql(text(f"""
                        SELECT official_monitored_element_id AS me_id, outage_id, outage_source AS request_id,
                               created_at, updated_at
                        FROM {me_market}_outage_quicklinks
                        WHERE outage_type = :t AND outage_id IN ({placeholders})
                    """), conn, params={"t": outage_type, **params})
            except Exception as e:
                warn(f"Could not read {me_market.upper()} quicklinks: {e}")
                continue
            if not df.empty:
                frames.append(df.assign(me_market=me_market, outage_kind=kind))
    if not frames:
        return pd.DataFrame(columns=["me_market", "me_id", "outage_kind", "outage_id", "request_id",
                                     "created_at", "updated_at"])
    out = pd.concat(frames, ignore_index=True)
    out["outage_id"] = out["outage_id"].astype(str)
    out["request_id"] = out["request_id"].fillna("").astype(str)
    return out.sort_values(["created_at"], ascending=False)


def element_notes(engine, market: str, me_ids, start_date: str | None, end_date: str | None,
                  context: str = "All") -> pd.DataFrame:
    """Quick notes on these monitored elements, newest first, without deleted ones."""
    ids = sorted({int(i) for i in me_ids})
    if not ids:
        return pd.DataFrame(columns=["me_id", "dt", "context", "body"])
    placeholders, params = _in("m", ids)
    where = [f"official_monitored_element_id IN ({placeholders})", "(deleted = 0 OR deleted IS NULL)"]
    if start_date:
        where.append("dt >= :start")
        params["start"] = start_date
    if end_date:
        where.append("dt < DATE_ADD(:end, INTERVAL 1 DAY)")
        params["end"] = end_date
    if context != "All":
        where.append("context = :ctx")
        params["ctx"] = context
    with engine.connect() as conn:
        return pd.read_sql(text(f"""
            SELECT official_monitored_element_id AS me_id, dt, context, body
            FROM {market}_monelem_quick_notes
            WHERE {" AND ".join(where)}
            ORDER BY dt DESC, id DESC
        """), conn, params=params)
