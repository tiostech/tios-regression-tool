"""The desk's RT shadow price forecasts, next to what actually happened.

Used by ``mcp_server.py``. Like ``lib/constraint_drivers.py``, nothing here imports
Streamlit.

Forecasts live in ``{market}_rt_constraint_shadow_price_forecasts``, one row per (day,
hour ending, official constraint ID). Each row has up to three forecasts, and ``shadow``
is the one that counts: the manual forecast if a trader set one, else the custom
forecast, else the default model forecast (checked for MISO in October 2026: no row
disagreed). A manual 0 is a trader zeroing out the model.

Actual RT shadow prices are one row per hour in ``{market}_rt_constraint_shadow_prices``,
in the same $/MWh units, and both are positive in every market. The forecasts are
expected values, so they spread small amounts over many hours rather than predicting
the binding hours exactly; compare totals as well as hours.

Constraint IDs churn: a monitored element often binds under a new constraint ID that
was never forecast, while the forecast sat on an older ID for the same element. So
accuracy can be rolled up by monitored element (``by="element"``), falling back to the
constraint ID where no element is mapped.
"""

import datetime

import numpy as np
import pandas as pd
from sqlalchemy import text

from lib import outage_search as osearch

FORECAST_COLUMNS = ["default_fc", "custom_fc", "custom_type", "manual_fc", "forecast"]
GROUPINGS = ("element", "constraint")

# Forecast or actual at least this much ($/MWh) counts as "binding" for hit/miss hours.
BINDING_THRESHOLD = 1.0


def _check_market(market: str) -> None:
    if market not in osearch.MARKETS:
        raise ValueError(f"Unknown market '{market}'.")


def hourly(engine, market: str, start: datetime.date, end: datetime.date, constraint_ids=None) -> pd.DataFrame:
    """Forecasts and actual RT shadow prices by (dt, hr, constraint), over ``start``..``end``.

    Rows come from either side, so constraints that bound without a forecast and
    forecasts that never bound are both included. ``forecast`` and ``actual`` are 0 where
    that side has no row; ``has_forecast`` says whether a forecast row existed at all.
    """
    _check_market(market)
    params = {"s": start, "e": end}
    where = ""
    if constraint_ids:
        ids = [int(c) for c in constraint_ids]
        where = f" AND official_constraint_id IN ({', '.join(str(c) for c in ids)})"
    with engine.connect() as conn:
        fc = pd.read_sql(text(f"""
            SELECT dt, hr, official_constraint_id AS constraint_id, official_monitored_element_id AS fc_me_id,
                   shadow_price_default_forecast AS default_fc, shadow_price_custom_forecast AS custom_fc,
                   shadow_price_custom_forecast_type AS custom_type, shadow_price_manual_forecast AS manual_fc,
                   shadow AS forecast
            FROM {market}_rt_constraint_shadow_price_forecasts
            WHERE dt BETWEEN :s AND :e{where}
        """), conn, params=params)
        act = pd.read_sql(text(f"""
            SELECT dt, hr, official_constraint_id AS constraint_id, MAX(official_monitored_element_id) AS act_me_id,
                   MAX(constraint_name) AS act_name, SUM(shadow_price) AS actual, MAX(preliminary) AS preliminary
            FROM {market}_rt_constraint_shadow_prices
            WHERE dt BETWEEN :s AND :e{where}
            GROUP BY dt, hr, official_constraint_id
        """), conn, params=params)

    fc["has_forecast"] = True
    df = fc.merge(act, on=["dt", "hr", "constraint_id"], how="outer")
    df["has_forecast"] = df["has_forecast"].eq(True)
    df["forecast"] = df["forecast"].fillna(0.0)
    df["actual"] = df["actual"].fillna(0.0)
    df["preliminary"] = df["preliminary"].fillna(0).astype(int)
    df["dt"] = pd.to_datetime(df["dt"]).dt.date
    return _add_names(engine, market, df)


def _add_names(engine, market: str, df: pd.DataFrame) -> pd.DataFrame:
    """Adds me_id, constraint name, and element name from the official constraint tables."""
    ids = df["constraint_id"].dropna().astype(int).unique().tolist()
    names = pd.DataFrame(columns=["constraint_id", "oc_me_id", "oc_name", "element"])
    if ids:
        with engine.connect() as conn:
            names = pd.read_sql(text(f"""
                SELECT oc.id AS constraint_id, oc.official_monitored_element_id AS oc_me_id,
                       COALESCE(oc.rt_descriptive_name, oc.monitored_element_name) AS oc_name,
                       ome.monitored_element_name AS element
                FROM {market}_official_constraints oc
                LEFT JOIN {market}_official_monitored_elements ome ON ome.id = oc.official_monitored_element_id
                WHERE oc.id IN ({', '.join(str(i) for i in ids)})
            """), conn)
    df = df.merge(names, on="constraint_id", how="left")
    df["me_id"] = df["act_me_id"].combine_first(df["fc_me_id"]).combine_first(df["oc_me_id"])
    df["name"] = df["act_name"].combine_first(df["oc_name"]).fillna("").str.strip()
    df["element"] = df["element"].fillna("").str.strip()
    return df.drop(columns=["act_me_id", "fc_me_id", "oc_me_id", "act_name", "oc_name"])


def _summarize(rows: pd.DataFrame, hours: pd.DataFrame) -> pd.Series:
    """Accuracy for one group: ``rows`` are its constraint-level rows, ``hours`` the same summed per hour."""
    fc, act = hours["forecast"].to_numpy(), hours["actual"].to_numpy()
    act_on = act >= BINDING_THRESHOLD
    corr = float(np.corrcoef(fc, act)[0, 1]) if len(hours) > 2 and fc.std() > 0 and act.std() > 0 else np.nan
    # Name the group after the constraint that carried the most money, so an element shows its main contingency.
    by_cid = rows.groupby("constraint_id")[["actual", "forecast"]].sum().sum(axis=1)
    active = sorted(int(c) for c in by_cid.index[by_cid > 0])
    names = rows.loc[rows["constraint_id"] == by_cid.idxmax(), "name"]
    unforecast = rows[~rows["has_forecast"] & (rows["actual"] >= BINDING_THRESHOLD)]
    return pd.Series({
        "me_id": rows["me_id"].dropna().iloc[0] if rows["me_id"].notna().any() else None,
        "name": next((n for n in names if n), "") or next((n for n in rows["element"] if n), ""),
        "constraint_ids": ",".join(str(c) for c in active),
        "actual_total": act.sum(),
        "forecast_total": fc.sum(),
        "bias": fc.sum() - act.sum(),
        "abs_error": np.abs(fc - act).sum(),
        "hours_bound": int(act_on.sum()),
        "fc_when_bound": fc[act_on].sum(),
        "fc_when_not": fc[~act_on].sum(),
        "bound_unforecast": unforecast["actual"].sum(),
        "corr": corr,
        "preliminary": int(rows["preliminary"].max()),
    })


def accuracy(df: pd.DataFrame, by: str = "element") -> pd.DataFrame:
    """One row per monitored element (or constraint) from ``hourly`` output: totals and error,
    largest abs_error first.

    Constraints in a group are summed hour by hour before comparing, so an element's forecast
    and actual line up even when they sit on different constraint IDs. constraint_ids lists
    the IDs in the group that carried any forecast or actual.

    - abs_error: sum over hours of |forecast - actual|. The dollars missed, either way.
    - norm_error: abs_error / max(actual_total, forecast_total). 0 is perfect; 1 means the
      forecast and actual never overlapped; up to 2 when they are similar in size but in
      different hours. The forecasts are expected values spread over many hours, so even a
      good one rarely gets far below 0.7.
    - hours_bound: hours with an actual of at least BINDING_THRESHOLD.
    - fc_when_bound / fc_when_not: forecast $ in hours that did / did not bind. Hit-or-miss
      by hour says little when nearly every hour carries some forecast; this shows where
      the forecast money went.
    - bound_unforecast: actual $ in hours whose constraint had no forecast row at all.
    - corr: hourly correlation of forecast with actual.
    """
    if by not in GROUPINGS:
        raise ValueError(f"Unknown grouping '{by}'. Use one of: {', '.join(GROUPINGS)}")
    if df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["group"] = "c" + df["constraint_id"].astype(int).astype(str)
    if by == "element":
        has_me = df["me_id"].notna()
        df.loc[has_me, "group"] = "me" + df.loc[has_me, "me_id"].astype(int).astype(str)

    per_hour = df.groupby(["group", "dt", "hr"])[["forecast", "actual"]].sum().reset_index()
    rows_by_group = dict(tuple(df.groupby("group")))
    res = pd.DataFrame([_summarize(rows_by_group[g], hours) for g, hours in per_hour.groupby("group")])
    res["norm_error"] = res["abs_error"] / res[["actual_total", "forecast_total"]].max(axis=1).replace(0, np.nan)
    return res.sort_values("abs_error", ascending=False).reset_index(drop=True)
