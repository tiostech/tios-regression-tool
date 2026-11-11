"""Nodal LMPs: actual DA and RT with components, and our forecast LMP, for the MCP server.

Actuals come from ``{market}_lmp_combined`` (hourly, both DA and RT; an hour appears only
once both exist), and for today onward from ``{market}_da_lmp`` (has tomorrow) and
``{market}_rt_lmp`` (preliminary until settled, kept about 10 days). Hours are hour ending
1-24 in market time (MISO fixed EST). Energy = LMP - congestion - loss.

The forecast LMP is priced the way the most recent Lion proposal prices bids (Lion is the live
nodal bidder; BidPriceFormula::CongestionNodalBidPriceFormula without the adder): the energy
forecast in the current version of its template's adder config (tios_configs, energy_forecasts),
plus the template's congestion forecast column, plus its loss factor column x energy.
"""

import datetime
import json

import pandas as pd
from sqlalchemy import bindparam, text

from lib import forecast_models
from lib import market_conditions

def _no_warn(_message):
    pass


MARKETS = ("miso", "spp", "pjm", "ercot", "caiso")
MAX_DAYS = 366


def _check_market(market: str) -> None:
    if market not in MARKETS:
        raise ValueError(f"Unknown market {market!r}.")


def _expanding(sql: str, *names: str):
    query = text(sql)
    for n in names:
        query = query.bindparams(bindparam(n, expanding=True))
    return query


def resolve_nodes(engine, market: str, nodes: list[str]) -> tuple[pd.DataFrame, list[str]]:
    """Pnodes for names, market identifiers, or official pnode IDs; a name fragment matches when
    it picks out a single node. Returns (nodes, messages about inputs that did not resolve)."""
    _check_market(market)
    wanted = [str(n).strip() for n in nodes if str(n).strip()]
    node_type = "NULL" if market == "caiso" else "node_type"  # caiso_official_pnodes has no node_type
    cols = (f"SELECT id, name, market_pnode_identifier, {node_type} AS node_type, deleted_at "
            f"FROM {market}_official_pnodes")
    ids = [int(n) for n in wanted if n.isdigit()] or [-1]
    with engine.connect() as conn:
        found = pd.read_sql(_expanding(f"{cols} WHERE name IN :n OR market_pnode_identifier IN :n OR id IN :ids",
                                       "n", "ids"), conn, params={"n": wanted, "ids": ids})
        keys = {str(v).upper() for c in ("id", "name", "market_pnode_identifier") for v in found[c]}
        messages, extra = [], []
        for n in wanted:
            if n.upper() in keys:
                continue
            like = pd.read_sql(text(f"{cols} WHERE name LIKE :p ORDER BY deleted_at IS NOT NULL, name LIMIT 11"),
                               conn, params={"p": f"%{n.replace('_', '%').replace('.', '%')}%"})
            if len(like) == 1:
                extra.append(like)
            elif like.empty:
                messages.append(f"No {market.upper()} pnode matches {n!r}.")
            else:
                names = ", ".join(like["name"].head(10))
                messages.append(f"{n!r} matches several pnodes; pass one of: {names}"
                                + (" ..." if len(like) > 10 else ""))
    out = pd.concat([found] + extra, ignore_index=True).drop_duplicates("id")
    return out, messages


def actuals(engine, market: str, pnode_ids: list[int], start: datetime.date, end: datetime.date,
            today: datetime.date) -> pd.DataFrame:
    """Hourly DA and RT LMP with congestion and loss: lmp_combined before today, da_lmp and
    rt_lmp from today on (rt_preliminary says whether the RT hour is still preliminary).

    da_lmp and rt_lmp have no clustered key, so their (dt, hr, pnode) index is forced: through
    the pnode index MySQL reads every hour the node ever had (200k+ rows)."""
    _check_market(market)
    frames = []
    with engine.connect() as conn:
        for pid in pnode_ids:
            if start < today:
                df = pd.read_sql(text(f"""
                    SELECT dt, hr, official_pnode_id, da_lmp, da_congestion, da_loss, rt_lmp, rt_congestion, rt_loss
                    FROM {market}_lmp_combined
                    WHERE official_pnode_id = :p AND dt BETWEEN :s AND :e
                """), conn, params={"p": pid, "s": start, "e": min(end, today - datetime.timedelta(days=1))})
                df["rt_preliminary"] = 0
                frames.append(df)
            if end >= today:
                s = max(start, today)
                da = pd.read_sql(text(f"""
                    SELECT dt, hr, official_pnode_id, lmp AS da_lmp, congestion AS da_congestion, loss AS da_loss
                    FROM {market}_da_lmp FORCE INDEX (index_{market}_da_lmp_on_dt_and_hr_and_official_pnode_id)
                    WHERE official_pnode_id = :p AND dt BETWEEN :s AND :e
                """), conn, params={"p": pid, "s": s, "e": end})
                rt = pd.read_sql(text(f"""
                    SELECT dt, hr, official_pnode_id, lmp AS rt_lmp, congestion AS rt_congestion, loss AS rt_loss,
                           preliminary AS rt_preliminary
                    FROM {market}_rt_lmp FORCE INDEX (index_{market}_rt_lmp_on_dt_and_hr_and_official_pnode_id)
                    WHERE official_pnode_id = :p AND dt BETWEEN :s AND :e
                """), conn, params={"p": pid, "s": s, "e": end})
                frames.append(da.merge(rt, on=["dt", "hr", "official_pnode_id"], how="outer"))
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame(columns=["dt", "hr", "official_pnode_id"])
    df = pd.concat(frames, ignore_index=True)
    df["dt"] = pd.to_datetime(df["dt"]).dt.date
    for k in ("da", "rt"):
        df[f"{k}_energy"] = df[f"{k}_lmp"] - df[f"{k}_congestion"] - df[f"{k}_loss"]
    return df


CONGESTION_COLUMNS = {"rt_congestion_forecast", "rt_congestion_forecast_load_adj", "rt_congestion_forecast_shadow_based",
                      "rt_congestion_forecast_pure_shadow_based"}
LOSS_COLUMNS = {"da_lf_forecast", "rt_lf_forecast"}


def lion_setup(engine, market: str, warn=_no_warn) -> dict | None:
    """What the most recent Lion proposal prices with: its template's congestion and loss
    columns and the energy forecasts in the current version of the template's adder config
    (tios_configs). None when the market has no Lion proposals."""
    try:
        with engine.connect() as conn:
            prop = conn.execute(text(f"""
                SELECT id, trade_dt, proposal_template_urn FROM {market}_lion_proposals ORDER BY id DESC LIMIT 1
            """)).mappings().first()
            if prop is None:
                return None
            raw = conn.execute(text(f"SELECT params FROM {market}_lion_proposal_templates WHERE urn = :u"),
                               {"u": prop["proposal_template_urn"]}).scalar()
            params = {str(k).lstrip(":"): v for k, v in (forecast_models.load_yaml(raw) or {}).items()}
            cfg_name = params.get("adder_config_name")
            cfg = conn.execute(text("""
                SELECT id, config FROM tios_configs WHERE name = :n AND current = 1 ORDER BY id DESC LIMIT 1
            """), {"n": cfg_name}).mappings().first() if cfg_name else None
    except Exception as e:
        warn(f"Could not read the {market.upper()} Lion proposal setup: {e}")
        return None
    energy = (json.loads(cfg["config"]) or {}).get("energy_forecasts", {}) if cfg else {}
    if not energy:
        warn(f"The {market.upper()} Lion adder config {cfg_name or '(none named)'} has no energy_forecasts.")
    cong = params.get("congestion_forecast_column", "rt_congestion_forecast_pure_shadow_based")
    loss = params.get("lossfactor_forecast_column", "rt_lf_forecast")
    if cong not in CONGESTION_COLUMNS or loss not in LOSS_COLUMNS:
        raise ValueError(f"Unexpected Lion template columns: {cong}, {loss}")
    return {"proposal_id": prop["id"], "trade_dt": prop["trade_dt"], "template": prop["proposal_template_urn"],
            "adder_config": cfg_name, "tios_config_id": cfg["id"] if cfg else None,
            "energy_inc": energy.get("inc"), "energy_dec": energy.get("dec"),
            "congestion_column": cong, "loss_column": loss}


def forecasts(engine, market: str, pnode_ids: list[int], start: datetime.date, end: datetime.date,
              warn=_no_warn) -> tuple[pd.DataFrame, dict | None]:
    """Hourly forecast LMP and its parts per node and trade day, priced as the most recent
    Lion proposal's template and current adder config price bids (without the adder). Energy is
    read as it stood at each day's day-ahead deadline. Returns (rows, lion_setup)."""
    _check_market(market)
    setup = lion_setup(engine, market, warn)
    cong_col = setup["congestion_column"] if setup else "rt_congestion_forecast_pure_shadow_based"
    loss_col = setup["loss_column"] if setup else "rt_lf_forecast"
    params = {"ids": [int(i) for i in pnode_ids], "s": start, "e": end}
    with engine.connect() as conn:
        cong = pd.read_sql(_expanding(f"""
            SELECT trade_dt AS dt, hr, official_pnode_id, {cong_col} AS fc_congestion
            FROM {market}_hourly_congestion_forecasts
            WHERE official_pnode_id IN :ids AND trade_dt BETWEEN :s AND :e
        """, "ids"), conn, params=params)
        loss = pd.read_sql(_expanding(f"""
            SELECT trade_dt AS dt, official_pnode_id, {loss_col} AS fc_loss_factor
            FROM {market}_daily_loss_forecasts
            WHERE official_pnode_id IN :ids AND trade_dt BETWEEN :s AND :e
        """, "ids"), conn, params=params)
    if cong.empty:
        return cong, setup
    for df in (cong, loss):
        df["dt"] = pd.to_datetime(df["dt"]).dt.date
    energy = []
    if setup and setup["energy_inc"]:
        for day in sorted(set(cong["dt"])):
            _, df = market_conditions.energy_forecasts(engine, market, day, names={"fc_energy": setup["energy_inc"]},
                                                       warn=warn)
            if "fc_energy" in df:
                energy.append(df.assign(dt=day)[["dt", "hr", "fc_energy"]])
    df = cong.merge(loss, on=["dt", "official_pnode_id"], how="left")
    if energy:
        df = df.merge(pd.concat(energy, ignore_index=True), on=["dt", "hr"], how="left")
    else:
        df["fc_energy"] = float("nan")
    df["fc_loss"] = df["fc_loss_factor"] * df["fc_energy"]
    df["fc_lmp"] = df["fc_energy"] + df["fc_congestion"] + df["fc_loss"]
    return df, setup
