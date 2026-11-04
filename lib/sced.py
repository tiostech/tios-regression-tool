"""ERCOT SCED-run constraint shadow prices, for the MCP server.

``ercot_rt_sced_constraint_shadow_prices`` has one row per SCED run (about every 5 minutes,
``sced_run`` in market local time) and binding constraint: shadow_price, line_flow,
line_limit, the monitored element, and the constraint name ERCOT published. Constraints
appear when SCED binds or nearly binds them, so some rows have a $0 shadow price. The primary key is (sced_run, official_constraint_id), so every query
here is bounded by a sced_run range first.

EnergyCore's "SCED Run Shadow Prices" chart (MarketDataAnalysis ScedRunShadows) reads the
same rows.
"""

import datetime

import pandas as pd
from sqlalchemy import text

TABLE = "ercot_rt_sced_constraint_shadow_prices"
GRANULARITIES = ["run", "hour", "day"]


def _in(prefix: str, values) -> tuple[str, dict]:
    values = list(values)
    return ", ".join(f":{prefix}{i}" for i in range(len(values))), {f"{prefix}{i}": v for i, v in enumerate(values)}


def _filters(start: datetime.date, end: datetime.date, constraint_ids=(), me_ids=(), name: str | None = None):
    where = ["sced_run >= :start", "sced_run < :end_excl"]
    params = {"start": start, "end_excl": end + datetime.timedelta(days=1)}
    if constraint_ids:
        placeholders, p = _in("c", [int(c) for c in constraint_ids])
        where.append(f"official_constraint_id IN ({placeholders})")
        params.update(p)
    if me_ids:
        placeholders, p = _in("m", [int(m) for m in me_ids])
        where.append(f"official_monitored_element_id IN ({placeholders})")
        params.update(p)
    if name:
        where.append("constraint_name LIKE :name")
        params["name"] = f"%{name.strip()}%"
    return where, params


def summary(engine, start: datetime.date, end: datetime.date, constraint_ids=(), me_ids=(),
            name: str | None = None) -> pd.DataFrame:
    """One row per constraint listed in the range: runs, binding runs (shadow > 0), hours with a binding run, total,
    average, and max shadow price, the highest flow as % of limit, first and last binding run.
    Largest total shadow first."""
    where, params = _filters(start, end, constraint_ids, me_ids, name)
    with engine.connect() as conn:
        return pd.read_sql(text(f"""
            SELECT official_constraint_id AS constraint_id,
                   MAX(official_monitored_element_id) AS me_id,
                   MAX(constraint_name) AS constraint_name,
                   COUNT(*) AS runs,
                   SUM(shadow_price > 0) AS binding_runs,
                   COUNT(DISTINCT CASE WHEN shadow_price > 0 THEN DATE_FORMAT(sced_run, '%Y-%m-%d %H') END) AS binding_hours,
                   SUM(shadow_price) AS total_shadow,
                   AVG(shadow_price) AS avg_shadow,
                   MAX(shadow_price) AS max_shadow,
                   MAX(CASE WHEN line_limit > 0 THEN 100 * line_flow / line_limit END) AS max_flow_pct,
                   MIN(sced_run) AS first_run,
                   MAX(sced_run) AS last_run
            FROM {TABLE}
            WHERE {" AND ".join(where)}
            GROUP BY official_constraint_id
            ORDER BY total_shadow DESC
        """), conn, params=params)


def runs(engine, start: datetime.date, end: datetime.date, constraint_ids=(), me_ids=(),
         name: str | None = None) -> pd.DataFrame:
    """Every SCED run row in the range, oldest first."""
    where, params = _filters(start, end, constraint_ids, me_ids, name)
    with engine.connect() as conn:
        return pd.read_sql(text(f"""
            SELECT sced_run, official_constraint_id AS constraint_id, official_monitored_element_id AS me_id,
                   constraint_name, shadow_price, line_flow, line_limit, preliminary, sourcetype
            FROM {TABLE}
            WHERE {" AND ".join(where)}
            ORDER BY sced_run, official_constraint_id
        """), conn, params=params)


def aggregate(df: pd.DataFrame, granularity: str) -> pd.DataFrame:
    """Runs rolled up per constraint and hour ending (HE1-24, a run at 10:05 is in HE11) or day:
    runs, binding runs (shadow > 0), average and max shadow price, average flow and limit, and the highest flow as
    % of limit."""
    if df.empty or granularity == "run":
        return df
    df = df.copy()
    df["date"] = df["sced_run"].dt.date
    keys = ["date"]
    if granularity == "hour":
        df["he"] = df["sced_run"].dt.hour + 1
        keys.append("he")
    df["flow_pct"] = 100 * df["line_flow"] / df["line_limit"].where(df["line_limit"] > 0)
    return (df.groupby(keys + ["constraint_id"], sort=True)
            .agg(runs=("shadow_price", "size"), binding_runs=("shadow_price", lambda x: int((x > 0).sum())), avg_shadow=("shadow_price", "mean"), max_shadow=("shadow_price", "max"),
                 avg_flow=("line_flow", "mean"), avg_limit=("line_limit", "mean"), max_flow_pct=("flow_pct", "max"))
            .reset_index())
