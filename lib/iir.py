"""Generator outages and derates from IIR, for the MCP server.

- ``{market}_iir_outages``: the latest version of every IIR outage, past and future (back to
  the 1980s). Dates are days: ``ta_start``..``ta_end`` inclusive, as EnergyCore's
  ``active_on_date`` reads them. ``cap_offlin`` is the MW offline, ``out_design`` the unit's
  capacity, ``outage_sta`` the status (Cancelled, Completed, In Progress, Scheduled, ...),
  ``outage_typ`` the kind (Planned, Forced, ...), ``prv_ta_st``/``prv_ta_end`` the dates before
  the last revision.
- ``{market}_iir_outage_snapshots``: one row per outage each time IIR changed it, keyed by
  ``for_date`` (when we loaded that version). That is the revision history.
"""

import datetime

import pandas as pd
from sqlalchemy import text

COLUMNS = ["outage_id", "unit_id", "unit_name", "plant_id", "plant_name", "owner_name", "unit_state", "prim_fuel",
           "utype_desc", "power_usag", "out_design", "cap_offlin", "ta_start", "ta_end", "outage_typ", "outage_sta",
           "out_cause", "prv_ta_st", "prv_ta_end", "comments"]


def _in(prefix: str, values) -> tuple[str, dict]:
    values = list(values)
    return ", ".join(f":{prefix}{i}" for i in range(len(values))), {f"{prefix}{i}": v for i, v in enumerate(values)}


def search(engine, market: str, start: datetime.date | None, end: datetime.date | None, name: str | None = None,
           plant_ids=(), unit_ids=(), outage_ids=(), fuels=(), outage_types=(), min_mw: float | None = None,
           include_cancelled: bool = False) -> pd.DataFrame:
    """Outages active at some time from ``start`` through ``end`` (either may be None for an open
    range) that match every given filter, largest MW offline first, with ``updated_at``."""
    where, params = [], {}
    if start is not None:
        where.append("o.ta_end >= :start")
        params["start"] = start
    if end is not None:
        where.append("o.ta_start <= :end")
        params["end"] = end
    if name:
        where.append("(o.plant_name LIKE :name OR o.unit_name LIKE :name OR o.owner_name LIKE :name)")
        params["name"] = f"%{name.strip()}%"
    for col, values, prefix in (("plant_id", plant_ids, "p"), ("unit_id", unit_ids, "u"), ("outage_id", outage_ids, "o")):
        values = [int(v) for v in values]
        if values:
            placeholders, p = _in(prefix, values)
            where.append(f"o.{col} IN ({placeholders})")
            params.update(p)
    if fuels:
        likes = []
        for i, f in enumerate(fuels):
            likes.append(f"o.prim_fuel LIKE :fuel{i}")
            params[f"fuel{i}"] = f"%{f.strip()}%"
        where.append("(" + " OR ".join(likes) + ")")
    if outage_types:
        placeholders, p = _in("t", [t.strip() for t in outage_types])
        where.append(f"o.outage_typ IN ({placeholders})")
        params.update(p)
    if min_mw is not None:
        where.append("o.cap_offlin >= :min_mw")
        params["min_mw"] = min_mw
    if not include_cancelled:
        where.append("(o.outage_sta IS NULL OR o.outage_sta <> 'Cancelled')")
    if not where:
        raise ValueError("Give a date range or at least one filter.")
    with engine.connect() as conn:
        return pd.read_sql(text(f"""
            SELECT {", ".join(f"o.`{c}`" for c in COLUMNS)}, o.updated_at
            FROM {market}_iir_outages o
            WHERE {" AND ".join(where)}
            ORDER BY o.cap_offlin DESC, o.ta_start
        """), conn, params=params)


def revisions(engine, market: str, outages: pd.DataFrame) -> pd.DataFrame:
    """Every loaded version of these outages (rows of ``search``), oldest first per outage.

    The snapshots are keyed by (for_date, outage_id), so this filters on the indexed unit_id
    and then on outage_id.
    """
    if outages.empty:
        return pd.DataFrame()
    units = sorted({int(u) for u in outages["unit_id"].dropna()})
    ids = sorted({int(o) for o in outages["outage_id"]})
    up, uparams = _in("u", units)
    op, oparams = _in("o", ids)
    unit_clause = f"s.unit_id IN ({up}) AND " if units else ""
    with engine.connect() as conn:
        return pd.read_sql(text(f"""
            SELECT s.for_date, s.outage_id, s.ta_start, s.ta_end, s.cap_offlin, s.outage_typ, s.outage_sta,
                   s.out_cause, s.comments
            FROM {market}_iir_outage_snapshots s
            WHERE {unit_clause}s.outage_id IN ({op})
            ORDER BY s.outage_id, s.for_date
        """), conn, params={**uparams, **oparams})


def changes(rev: pd.DataFrame) -> pd.DataFrame:
    """Only the versions that changed dates, MW, type, or status from the version before (the
    first version always counts). Snapshots also record comment-only edits."""
    if rev.empty:
        return rev
    keys = ["ta_start", "ta_end", "cap_offlin", "outage_typ", "outage_sta"]
    prev = rev.groupby("outage_id")[keys].shift()
    changed = prev.isna().all(axis=1) | (rev[keys].astype(str) != prev.astype(str)).any(axis=1)
    return rev[changed]
