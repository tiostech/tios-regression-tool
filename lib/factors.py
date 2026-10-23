"""A constraint's promoted shift factors: the constraint -> pnode factors EnergyCore actually uses.

Used by ``mcp_server.py``. Nothing here imports Streamlit.

EnergyCore's FactorAggregator (tios-core ``app/modules/constraint_analysis/factor_aggregator.rb``)
picks one upstream source per constraint by a per-market priority order (trader-frozen,
dependency-recent, rt-matrix-recent, dependency, rt-matrix, trader-csv, trader-copy, auto-copy,
da-matrix, tlr in PJM) and copies it into two tables:

- ``{market}_constraint_congestion_factor_masters``: one row per constraint. ``source_type``
  is the source that won; for ``auto-copy`` and ``trader-copy`` rows ``aggregate_type`` is the
  original source and ``source_official_constraint_id`` the constraint the factors came from.
- ``{market}_constraint_congestion_factor_master_details``: (constraint, pnode) -> factor.
  Tens of millions of rows, so always filter on the constraint (the key starts with it).

The shadow-based nodal congestion forecasts use these as congestion = factor x shadow price.
"""

import pandas as pd
from sqlalchemy import text

MASTER_COLUMNS = ["official_constraint_id", "source_type", "aggregate_type", "source_official_constraint_id",
                  "reading_time", "solve_start_dt", "solve_stop_dt", "constraints_at_reading",
                  "shadow_price_at_reading", "max_official_pnode_id", "max_factor", "min_official_pnode_id",
                  "min_factor", "temporary", "manual", "updated_at", "aggregated_at", "expires_at", "frozen_until"]


def _columns(conn, table: str) -> set:
    return {r[0] for r in conn.execute(text(f"SHOW COLUMNS FROM {table}"))}


def constraint_name(engine, market: str, constraint_id: int) -> tuple[str, int | None, str]:
    """(constraint name, monitored element ID, monitored element name); blanks when unknown."""
    with engine.connect() as conn:
        row = conn.execute(text(f"""
            SELECT COALESCE(rt_descriptive_name, da_descriptive_name), official_monitored_element_id,
                   monitored_element_name
            FROM {market}_official_constraints WHERE id = :c
        """), {"c": constraint_id}).fetchone()
    if not row:
        return "", None, ""
    return (row[0] or "").strip(), row[1], (row[2] or "").strip()


def master(engine, market: str, constraint_id: int) -> dict | None:
    """The constraint's promoted master row, or None when it has no factors."""
    with engine.connect() as conn:
        present = _columns(conn, f"{market}_constraint_congestion_factor_masters")
        cols = ", ".join(f"`{c}`" if c in present else f"NULL AS `{c}`" for c in MASTER_COLUMNS)
        row = conn.execute(text(f"""
            SELECT {cols} FROM {market}_constraint_congestion_factor_masters WHERE official_constraint_id = :c
        """), {"c": constraint_id}).mappings().fetchone()
    return dict(row) if row else None


def details(engine, market: str, constraint_id: int) -> pd.DataFrame:
    """Every promoted factor for the constraint with its pnode's name, node type, and zone
    (MISO and some other markets have no zone column, so zone is blank there)."""
    with engine.connect() as conn:
        present = _columns(conn, f"{market}_official_pnodes")
        zone = "p.zone" if "zone" in present else "NULL"
        node_type = "p.node_type" if "node_type" in present else "NULL"
        df = pd.read_sql(text(f"""
            SELECT d.official_pnode_id AS pnode_id, p.name AS pnode, {node_type} AS node_type, {zone} AS zone, d.factor
            FROM {market}_constraint_congestion_factor_master_details d
            LEFT JOIN {market}_official_pnodes p ON p.id = d.official_pnode_id
            WHERE d.official_constraint_id = :c AND d.factor IS NOT NULL
        """), conn, params={"c": constraint_id})
    for col in ("pnode", "node_type", "zone"):
        df[col] = df[col].fillna("").astype(str).str.strip()
    return df


def filter_details(df: pd.DataFrame, node_types=None, zones=None) -> pd.DataFrame:
    """Only pnodes of these node types and zones (case-insensitive)."""
    if node_types:
        wanted = {t.strip().lower() for t in node_types}
        df = df[df["node_type"].str.lower().isin(wanted)]
    if zones:
        wanted = {z.strip().lower() for z in zones}
        df = df[df["zone"].str.lower().isin(wanted)]
    return df


def extremes(df: pd.DataFrame, n: int, collapse_ties: bool = False,
             examples: int = 3) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(most positive, most negative) factors, n of each, largest magnitude first. Zero factors
    are in neither list.

    Electrically equivalent pnodes (the load and generator buses at one station) share the same
    factor, so a plain list can be one station over and over. With ``collapse_ties`` each row is
    one factor value instead, with how many pnodes have it and a few of their names."""
    pos = df[df["factor"] > 0].sort_values(["factor", "pnode"], ascending=[False, True])
    neg = df[df["factor"] < 0].sort_values(["factor", "pnode"], ascending=[True, True])
    if not collapse_ties:
        return pos.head(n), neg.head(n)

    def collapse(side: pd.DataFrame, ascending: bool) -> pd.DataFrame:
        if side.empty:
            return pd.DataFrame(columns=["factor", "pnodes", "examples", "node_types", "zones"])
        side = side.assign(level=side["factor"].round(6))
        out = side.groupby("level", sort=False).agg(
            pnodes=("pnode_id", "size"),
            examples=("pnode", lambda s: ", ".join(list(s)[:examples]) + (f", ... (+{len(s) - examples})" if len(s) > examples else "")),
            node_types=("node_type", lambda s: ", ".join(sorted({x for x in s if x}))),
            zones=("zone", lambda s: ", ".join(sorted({x for x in s if x}))),
        ).reset_index().rename(columns={"level": "factor"})
        return out.sort_values("factor", ascending=ascending).head(n)

    return collapse(pos, False), collapse(neg, True)

