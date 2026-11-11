"""Our trading returns, as EnergyCore stores them, for the MCP server.

EnergyCore writes returns onto every submitted bid once the ISO's awards load
(``AnyMarketBidSlateManager#update_returns!``): ``{market}_submitted_bids`` has one row per
trade day, hour (ending, market time), node and bid type (ERCOT: path, bid type PTP) with
cleared MW and the return split into energy, congestion and loss:

- INC: cleared MW x (DA - RT) of each component; DEC: cleared MW x (RT - DA).
- ERCOT PTP: cleared MW x ((DA source - DA sink) - (RT source - RT sink)), pnode = source,
  pnode2 = sink. ERCOT loss is 0 and its congestion is derived, so returns are congestion.
- Prices are ``{market}_lmp_combined``; energy = LMP - congestion - loss, so energy +
  congestion + loss = lmp_return. total_return = lmp_return - bid fee - clear fee - (MISO)
  DA opres charges on decs.

``{market}_summarized_returns`` is EnergyCore's daily total; it also subtracts day-level opres
charges (or the desk's override) and misc fees, so its total can differ from the bids' sum.
"""

import datetime

import pandas as pd
from sqlalchemy import bindparam, text

GROUPS = ("day", "hour", "node")
SUMS = ["bids", "cleared_bids", "bid_mw", "cleared_mw", "inc_mw", "dec_mw", "total_return", "lmp_return",
        "energy_return", "congestion_return", "loss_return", "fees", "preliminary_rows"]


def _check_market(market: str) -> None:
    if market not in ("miso", "spp", "pjm", "ercot", "caiso"):
        raise ValueError(f"Unknown market {market!r}.")


def node_key_sql(market: str) -> str:
    if market == "ercot":
        return "CONCAT(b.cached_market_pnode_identifier, ' -> ', b.cached_market_pnode2_identifier)"
    return "b.cached_market_pnode_identifier"


def bid_returns(engine, market: str, start: datetime.date, end: datetime.date, group: str,
                nodes: list[str] | None = None, bid_types: list[str] | None = None) -> pd.DataFrame:
    """Summed returns from ``{market}_submitted_bids``, grouped by day, hour, or node/path."""
    _check_market(market)
    key = {"day": "b.trade_dt", "hour": "b.hr", "node": node_key_sql(market)}[group]
    where = ["b.trade_dt BETWEEN :s AND :e"]
    params = {"s": start, "e": end}
    if nodes:
        where.append("(b.cached_market_pnode_identifier IN :nodes OR b.cached_market_pnode2_identifier IN :nodes"
                     " OR b.cached_market_pnode_name IN :nodes)")
        params["nodes"] = list(nodes)
    if bid_types:
        where.append("b.bid_type IN :types")
        params["types"] = [t.upper() for t in bid_types]
    query = text(f"""
        SELECT {key} AS grp,
               COUNT(*) AS bids, SUM(b.cleared_volume > 0) AS cleared_bids,
               SUM(b.bid_volume) AS bid_mw, SUM(b.cleared_volume) AS cleared_mw,
               SUM(CASE WHEN b.bid_type = 'INC' THEN b.cleared_volume ELSE 0 END) AS inc_mw,
               SUM(CASE WHEN b.bid_type = 'DEC' THEN b.cleared_volume ELSE 0 END) AS dec_mw,
               SUM(b.total_return) AS total_return, SUM(b.lmp_return) AS lmp_return,
               SUM(b.energy_return) AS energy_return, SUM(b.congestion_return) AS congestion_return,
               SUM(b.loss_return) AS loss_return,
               SUM(COALESCE(b.bid_fee, 0) + COALESCE(b.clear_fee, 0) + COALESCE(b.bid_opres_charges, 0)) AS fees,
               SUM(COALESCE(b.returns_are_preliminary, 0)) AS preliminary_rows,
               {"MAX(b.cached_market_pnode_name)" if group == "node" and market != "ercot" else "NULL"} AS node_name
        FROM {market}_submitted_bids b
        WHERE {" AND ".join(where)}
        GROUP BY grp
    """)
    for name in ("nodes", "types"):
        if name in params:
            query = query.bindparams(bindparam(name, expanding=True))
    with engine.connect() as conn:
        df = pd.read_sql(query, conn, params=params)
    for c in SUMS:
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    return df


def daily_summaries(engine, market: str, start: datetime.date, end: datetime.date) -> pd.DataFrame:
    """EnergyCore's daily totals, with the day-level adjustments the bids don't carry."""
    _check_market(market)
    with engine.connect() as conn:
        df = pd.read_sql(text(f"""
            SELECT trade_dt AS grp, total_return AS day_total_return,
                   COALESCE(day_opres_charges_override, day_opres_charges, 0) AS day_opres_charges,
                   COALESCE(misc_fees, 0) AS misc_fees, (day_opres_charges_override IS NOT NULL) AS opres_overridden
            FROM {market}_summarized_returns WHERE trade_dt BETWEEN :s AND :e
        """), conn, params={"s": start, "e": end})
    return df


def trade_days(engine, market: str, start: datetime.date, end: datetime.date) -> set[datetime.date]:
    """Trade days in the range that have submitted bids."""
    _check_market(market)
    with engine.connect() as conn:
        rows = conn.execute(text(f"SELECT DISTINCT trade_dt FROM {market}_submitted_bids WHERE trade_dt BETWEEN :s AND :e"),
                            {"s": start, "e": end}).fetchall()
    return {pd.Timestamp(r[0]).date() for r in rows}
