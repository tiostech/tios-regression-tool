"""Autoflow: the flow that zonal wind, solar, and load put on a constraint.

This reproduces tios-core's Autoflow market data analysis
(``app/modules/market_data_analysis_extensions/autoflow_common.rb``) so the MCP server
can use it without EnergyCore:

    autoflow(hour) = sum over zones of  coefficient(zone) * MW(zone, hour)

**Coefficients** come from ``{market}_zonal_coefficients``. EnergyCore's
``AnymarketAutoflowRunner`` fills it daily, from Panorama bus exposures, but only for
the constraints listed in the market's ``autoflow_constraints`` parameter. A constraint
ID first seen today usually has none; ``coefficient_constraint`` then falls back to
another ID for the same line.

Sign: a positive coefficient * MW LOADS the constraint. tios-core computes wind and
solar coefficients as minus the capacity-weighted bus exposure and load coefficients as
the mean exposure, so all three read the same way. "High side" zones are those with a
positive load coefficient or a negative wind/solar coefficient. (Raw pnode shift factors
in ``constraint_drivers`` use the opposite sign: there a negative factor loads.)

**Zonal MW** comes from one of two sources, named as in tios-core:

- ``forecast``: the MySQL forecast tables (``{iso}_meteologica_{type}_forecasts``,
  ``{iso}_prt_loadtemp_forecasts``). These keep one forecast per hour, the one made
  before the day-ahead market, so they describe what the market expected.
- ``pseudo_actuals``: for each hour, the latest forecast made for it, which is close to
  what actually happened. tios-core reads these from Snowflake snapshot tables, through a
  parquet cache on S3 (``AnymarketPseudoActualsCacheBuilder``, refreshed every few
  minutes). This module reads that S3 cache with pyarrow and the normal AWS credential
  chain (``~/.aws`` or environment variables). It does not query Snowflake directly: the
  Snowflake connection needs an account and key-pair setup this repo does not have yet,
  so months missing from the cache are reported as warnings rather than filled in.

Zones whose ISO has no data table (for example MHEB or SPC) are skipped, and so are zones
with data for fewer than 90% of the hours, as in tios-core. Both are reported through
``warn``.
"""

import datetime
import os
import threading
import time
import zoneinfo

import pandas as pd
from sqlalchemy import text

from lib import outage_search as osearch

VENDORS = ("meteologica", "prt")
COEFFICIENT_TYPES = ("solar", "wind", "load")
SOURCES = ("pseudo_actuals", "forecast")
LEVERAGE_SIDES = ("all", "high", "low")

# Same as tios-core's REQUIRED_ZONE_READINGS_PERCENTAGE.
REQUIRED_ZONE_READINGS = 0.9

PSEUDO_ACTUALS_BUCKET = os.environ.get("TIOS_PSEUDO_ACTUALS_BUCKET", "tiosdata-production")
PSEUDO_ACTUALS_PREFIX = "pseudo-actuals/cache"
AWS_REGION = os.environ.get("TIOS_AWS_REGION", "us-east-1")
# The cache is rebuilt every few minutes, so files read from S3 are reused for this long.
S3_CACHE_SECONDS = 10 * 60

# Rails time zone names used in the markets table, mapped to IANA names. Names not
# listed here (e.g. "Etc/GMT+5" for MISO) are already IANA names.
RAILS_TIME_ZONES = {
    "Eastern Time (US & Canada)": "America/New_York",
    "Central Time (US & Canada)": "America/Chicago",
    "Mountain Time (US & Canada)": "America/Denver",
    "Pacific Time (US & Canada)": "America/Los_Angeles",
    "Arizona": "America/Phoenix",
    "Saskatchewan": "America/Regina",
}


def _no_warn(_message):
    pass


def _check(name: str, value, choices) -> None:
    if value not in choices:
        raise ValueError(f"Unknown {name} '{value}'. Use one of: {', '.join(choices)}")


# --- TIME ZONES ---

_tz_cache = {}


def market_time_zone(engine, urn: str):
    """The ZoneInfo for a market or ISO urn from the markets table, or None if it is not there."""
    if urn not in _tz_cache:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT time_zone FROM markets WHERE urn = :u"), {"u": urn}).fetchone()
        name = RAILS_TIME_ZONES.get(row[0], row[0]) if row and row[0] else None
        try:
            _tz_cache[urn] = zoneinfo.ZoneInfo(name) if name else None
        except zoneinfo.ZoneInfoNotFoundError:
            _tz_cache[urn] = None
    return _tz_cache[urn]


def _local_he_to_utc(df: pd.DataFrame, tz) -> pd.Series:
    """Naive UTC hour-beginning timestamps for market-local (dt, hr) hour-ending rows."""
    local = pd.to_datetime(df["dt"]) + pd.to_timedelta(df["hr"].astype(int) - 1, unit="h")
    return (local.dt.tz_localize(tz, ambiguous="NaT", nonexistent="shift_forward")
            .dt.tz_convert("UTC").dt.tz_localize(None))


def _utc_to_local_he(time_utc: pd.Series, tz) -> pd.DataFrame:
    """(dt, hr) hour-ending columns in ``tz`` for naive UTC hour-beginning timestamps."""
    local = pd.to_datetime(time_utc).dt.tz_localize("UTC").dt.tz_convert(tz).dt.tz_localize(None)
    return pd.DataFrame({"dt": local.dt.date, "hr": local.dt.hour + 1}, index=time_utc.index)


# --- COEFFICIENTS ---

def coefficient_constraint(engine, market: str, constraint_id: int, warn=_no_warn) -> tuple[int | None, object]:
    """(constraint ID whose coefficients to use, their computed_at).

    Uses ``constraint_id`` when it has coefficients. Otherwise looks for other constraint
    IDs with the same name in the RT and DA shadow price tables, and uses the one with the
    most recent coefficients. Returns (None, None) when nothing is found.
    """
    _check("market", market, osearch.MARKETS)
    with engine.connect() as conn:
        latest = conn.execute(text(f"""
            SELECT MAX(computed_at) FROM {market}_zonal_coefficients WHERE official_constraint_id = :c
        """), {"c": constraint_id}).scalar()
        if latest is not None:
            return constraint_id, latest

        names = set()
        for kind in ("rt", "da"):
            names.update(r[0] for r in conn.execute(text(f"""
                SELECT DISTINCT constraint_name FROM {market}_{kind}_constraint_shadow_prices
                WHERE official_constraint_id = :c AND constraint_name IS NOT NULL
            """), {"c": constraint_id}).fetchall())
        if not names:
            return None, None

        name_params = {f"n{i}": n for i, n in enumerate(sorted(names))}
        placeholders = ", ".join(f":{k}" for k in name_params)
        candidates = set()
        for kind in ("rt", "da"):
            candidates.update(r[0] for r in conn.execute(text(f"""
                SELECT DISTINCT official_constraint_id FROM {market}_{kind}_constraint_shadow_prices
                WHERE constraint_name IN ({placeholders})
            """), name_params).fetchall())
        candidates.discard(constraint_id)
        if not candidates:
            return None, None

        cand_params = {f"c{i}": c for i, c in enumerate(sorted(candidates))}
        row = conn.execute(text(f"""
            SELECT official_constraint_id, MAX(computed_at) AS latest
            FROM {market}_zonal_coefficients
            WHERE official_constraint_id IN ({", ".join(f":{k}" for k in cand_params)})
            GROUP BY official_constraint_id
            ORDER BY latest DESC
            LIMIT 1
        """), cand_params).fetchone()
    if row is None:
        return None, None
    warn(f"Constraint {constraint_id} has no zonal coefficients; using those of constraint {row[0]} "
         f"(same name: {', '.join(sorted(names))}), computed {row[1]}.")
    return row[0], row[1]


def zonal_coefficients(engine, market: str, constraint_id: int, computed_at, vendor: str = "meteologica",
                       coefficient_types=COEFFICIENT_TYPES, leverage_side: str = "all",
                       threshold_min: float | None = None, threshold_max: float | None = None) -> pd.DataFrame:
    """Coefficients to use, with tios-core's selection rules (``fetch_zonal_coefficients``):

    - thresholds apply to |coefficient| (min defaults to 0, max to 1 when either is set)
    - ``high`` keeps load > 0 and wind/solar < 0; ``low`` keeps the opposite
    - vendor ``prt`` uses PRT load for MISO zones, Meteologica load for other ISOs, and
      Meteologica wind/solar
    """
    _check("market", market, osearch.MARKETS)
    _check("vendor", vendor, VENDORS)
    _check("leverage_side", leverage_side, LEVERAGE_SIDES)
    for t in coefficient_types:
        _check("coefficient type", t, COEFFICIENT_TYPES)

    with engine.connect() as conn:
        df = pd.read_sql(text(f"""
            SELECT zone, coefficient_type, vendor, iso, coefficient
            FROM {market}_zonal_coefficients
            WHERE official_constraint_id = :c AND computed_at = :t
        """), conn, params={"c": constraint_id, "t": computed_at})

    df = df[df["coefficient_type"].isin(coefficient_types) & df["coefficient"].notna()]
    if threshold_min is not None or threshold_max is not None:
        lo, hi = threshold_min or 0.0, threshold_max if threshold_max is not None else 1.0
        df = df[df["coefficient"].abs().between(lo, hi)]

    is_load = df["coefficient_type"] == "load"
    if leverage_side == "high":
        df = df[(is_load & (df["coefficient"] > 0)) | (~is_load & (df["coefficient"] < 0))]
    elif leverage_side == "low":
        df = df[(is_load & (df["coefficient"] < 0)) | (~is_load & (df["coefficient"] > 0))]

    if vendor == "meteologica" or "load" not in coefficient_types:
        df = df[df["vendor"] == "meteologica"]
    else:
        is_load = df["coefficient_type"] == "load"
        df = df[((~is_load) & (df["vendor"] == "meteologica"))
                | (is_load & (df["vendor"] == "prt") & (df["iso"] == "miso"))
                | (is_load & (df["vendor"] == "meteologica") & (df["iso"] != "miso"))]
    return df.reset_index(drop=True)


# --- ZONAL DATA ---

def zonal_table(iso: str, vendor: str, coefficient_type: str) -> tuple[str, str, str]:
    """(table base name, zone column, MW column) for one ISO/vendor/type, as tios-core picks them.

    The MySQL forecast table is ``base + 's'``; the pseudo-actual cache is
    ``base + '_snapshots'``. SPP solar uses the ISO's own solar forecast in place of
    Meteologica's, because Meteologica does not have the needed SPP regions.
    """
    if vendor == "prt":
        return f"{iso}_prt_loadtemp_forecast", "region_zone", "load_mwh"
    if iso == "spp" and coefficient_type == "solar":
        return "spp_solargen_output_forecast", "region", "power_mw"
    return f"{iso}_meteologica_{coefficient_type}_forecast", "region", "power_mw"


def _gate_table(iso: str, vendor: str, coefficient_type: str) -> str:
    # tios-core checks this MySQL table to decide whether the ISO has data at all.
    if vendor == "prt":
        return f"{iso}_prt_loadtemp_forecasts"
    return f"{iso}_meteologica_{coefficient_type}_forecasts"


_existing_tables = {"loaded": 0.0, "names": set()}


def _table_exists(engine, table: str) -> bool:
    if time.time() - _existing_tables["loaded"] > 3600:
        with engine.connect() as conn:
            _existing_tables["names"] = {r[0] for r in conn.execute(text("SHOW TABLES")).fetchall()}
        _existing_tables["loaded"] = time.time()
    return table in _existing_tables["names"]


def _forecast_values(engine, table: str, zone_col: str, mw_col: str, zones, start_utc, end_utc, tz) -> pd.DataFrame:
    """MySQL forecast rows as [time_utc, zone, mw]. Local dates are padded by a day so a
    UTC range is fully covered whatever the ISO's time zone."""
    params = {f"z{i}": z for i, z in enumerate(zones)}
    params.update({"d0": (start_utc - datetime.timedelta(days=1)).date(),
                   "d1": (end_utc + datetime.timedelta(days=1)).date()})
    with engine.connect() as conn:
        df = pd.read_sql(text(f"""
            SELECT dt, hr, {zone_col} AS zone, {mw_col} AS mw
            FROM {table}
            WHERE dt BETWEEN :d0 AND :d1 AND {zone_col} IN ({", ".join(f":{k}" for k in params if k.startswith("z"))})
        """), conn, params=params)
    if df.empty:
        return pd.DataFrame(columns=["time_utc", "zone", "mw"])
    df["time_utc"] = _local_he_to_utc(df, tz)
    df = df.dropna(subset=["time_utc"])
    return df.loc[(df["time_utc"] >= start_utc) & (df["time_utc"] <= end_utc), ["time_utc", "zone", "mw"]]


_s3_cache = {}
_s3_lock = threading.Lock()


def _read_cache_month(table: str, month: datetime.date) -> pd.DataFrame | None:
    """One month of the S3 pseudo-actual cache, or None when the file does not exist."""
    key = (table, month.year, month.month)
    with _s3_lock:
        hit = _s3_cache.get(key)
        if hit is not None and time.time() - hit[0] < S3_CACHE_SECONDS:
            return hit[1]

    import pyarrow.fs as pafs
    import pyarrow.parquet as pq

    path = f"{PSEUDO_ACTUALS_BUCKET}/{PSEUDO_ACTUALS_PREFIX}/{month:%Y/%m}/{table}.parquet"
    filesystem = pafs.S3FileSystem(region=AWS_REGION)
    try:
        df = pq.read_table(path, filesystem=filesystem).to_pandas()
    except FileNotFoundError:
        df = None
    with _s3_lock:
        _s3_cache[key] = (time.time(), df)
    return df


def _pseudo_actual_values(table: str, zone_col: str, mw_col: str, zones, start_utc, end_utc,
                          warn=_no_warn) -> pd.DataFrame:
    """Pseudo actuals from the S3 cache as [time_utc, zone, mw]."""
    months, m = [], datetime.date(start_utc.year, start_utc.month, 1)
    while m <= end_utc.date():
        months.append(m)
        m = (m + datetime.timedelta(days=32)).replace(day=1)

    frames = []
    for month in months:
        try:
            df = _read_cache_month(table, month)
        except Exception as e:  # credentials, network, permissions
            warn(f"Could not read s3://{PSEUDO_ACTUALS_BUCKET}/{PSEUDO_ACTUALS_PREFIX}/{month:%Y/%m}/{table}.parquet: {e}")
            continue
        if df is None:
            warn(f"No pseudo-actual cache for {table} in {month:%Y-%m}; those zones are left out for that month.")
            continue
        df = df[df[zone_col].isin(zones)]
        frames.append(df[["time_utc", zone_col, mw_col]].rename(columns={zone_col: "zone", mw_col: "mw"}))
    if not frames:
        return pd.DataFrame(columns=["time_utc", "zone", "mw"])
    df = pd.concat(frames)
    df["time_utc"] = pd.to_datetime(df["time_utc"])
    return df[(df["time_utc"] >= start_utc) & (df["time_utc"] <= end_utc)]


# --- AUTOFLOW ---

def autoflow(engine, market: str, constraint_id: int, start_date: datetime.date, end_date: datetime.date | None = None,
             source: str = "pseudo_actuals", vendor: str = "meteologica", coefficient_types=COEFFICIENT_TYPES,
             leverage_side: str = "all", threshold_min: float | None = None, threshold_max: float | None = None,
             warn=_no_warn) -> tuple[dict | None, pd.DataFrame, pd.DataFrame]:
    """Hourly autoflow on a constraint for market days ``start_date`` through ``end_date``.

    Returns (metadata, hourly, leverage):

    - metadata: constraint_id used for coefficients, computed_at, source, vendor, and zone
      counts; None when there are no coefficients
    - hourly: one row per market (dt, hr) with ``autoflow`` and one column per coefficient
      type, in MW (+ loads the constraint)
    - leverage: one row per zone and hour with coefficient, MW, and leverage
      (coefficient * MW), for finding which zones moved
    """
    _check("market", market, osearch.MARKETS)
    _check("source", source, SOURCES)
    end_date = end_date or start_date
    empty = pd.DataFrame()

    used_id, computed_at = coefficient_constraint(engine, market, constraint_id, warn)
    if used_id is None:
        warn(f"No zonal coefficients for {market.upper()} constraint {constraint_id} or any constraint with its name. "
             f"EnergyCore only computes them for constraints in the '{market}' autoflow_constraints parameter.")
        return None, empty, empty

    coeffs = zonal_coefficients(engine, market, used_id, computed_at, vendor, coefficient_types, leverage_side,
                                threshold_min, threshold_max)
    meta = {"constraint_id": used_id, "computed_at": computed_at, "source": source, "vendor": vendor,
            "zones_total": len(coeffs), "zones_used": 0}
    if coeffs.empty:
        warn("No zonal coefficients match the vendor, type, side, and threshold settings.")
        return meta, empty, empty

    market_tz = market_time_zone(engine, market)
    day_rows = pd.DataFrame({"dt": [start_date, end_date + datetime.timedelta(days=1)], "hr": [1, 1]})
    start_utc, end_utc = _local_he_to_utc(day_rows, market_tz)
    end_utc = end_utc - datetime.timedelta(hours=1)

    frames, skipped = [], []
    for (ctype, iso, row_vendor), group in coeffs.groupby(["coefficient_type", "iso", "vendor"]):
        if not _table_exists(engine, _gate_table(iso, row_vendor, ctype)):
            skipped.append(f"{iso} {ctype} ({len(group)} zones, no data table)")
            continue
        base, zone_col, mw_col = zonal_table(iso, row_vendor, ctype)
        zones = group["zone"].tolist()
        if source == "forecast":
            iso_tz = market_time_zone(engine, iso)
            if iso_tz is None:
                warn(f"No time zone for '{iso}' in the markets table; assuming {market.upper()} time for its forecasts.")
                iso_tz = market_tz
            values = _forecast_values(engine, base + "s", zone_col, mw_col, zones, start_utc, end_utc, iso_tz)
        else:
            values = _pseudo_actual_values(base + "_snapshots", zone_col, mw_col, zones, start_utc, end_utc, warn)
        if values.empty:
            skipped.append(f"{iso} {ctype} ({len(group)} zones, no data for these hours)")
            continue
        values = values.merge(group[["zone", "coefficient"]], on="zone")
        values["coefficient_type"], values["iso"] = ctype, iso
        frames.append(values)

    if skipped:
        warn("Autoflow zones left out: " + "; ".join(skipped) + ".")
    if not frames:
        return meta, empty, empty

    lev = pd.concat(frames, ignore_index=True)
    lev = lev.groupby(["time_utc", "coefficient_type", "iso", "zone", "coefficient"], as_index=False)["mw"].mean()
    lev["leverage"] = lev["coefficient"] * lev["mw"]

    # Same rule as tios-core: drop zones with data for fewer than 90% of the hours.
    total_hours = lev["time_utc"].nunique()
    counts = lev.groupby(["coefficient_type", "iso", "zone"])["time_utc"].nunique()
    sparse = counts[counts < REQUIRED_ZONE_READINGS * total_hours]
    if len(sparse):
        warn(f"Autoflow dropped {len(sparse)} zones with data for under {REQUIRED_ZONE_READINGS:.0%} of hours: "
             + ", ".join(f"{iso} {zone} {ct}" for ct, iso, zone in sparse.index[:10])
             + (" ..." if len(sparse) > 10 else "") + ".")
        keep = counts[counts >= REQUIRED_ZONE_READINGS * total_hours].index
        lev = lev.set_index(["coefficient_type", "iso", "zone"]).loc[lambda d: d.index.isin(keep)].reset_index()

    lev = pd.concat([lev, _utc_to_local_he(lev["time_utc"], market_tz)], axis=1)
    meta["zones_used"] = lev.groupby(["coefficient_type", "iso", "zone"]).ngroups

    by_type = lev.pivot_table(index=["dt", "hr"], columns="coefficient_type", values="leverage", aggfunc="sum")
    hourly = by_type.reindex(columns=[t for t in COEFFICIENT_TYPES if t in by_type.columns])
    hourly.insert(0, "autoflow", hourly.sum(axis=1))
    hourly = hourly.round(2).reset_index()
    return meta, hourly, lev[["dt", "hr", "time_utc", "coefficient_type", "iso", "zone", "coefficient", "mw",
                              "leverage"]]


def zone_changes(leverage: pd.DataFrame, day: datetime.date, base_hr: int, compare_hr: int) -> pd.DataFrame:
    """Per zone, leverage at ``base_hr`` and ``compare_hr`` on ``day`` and the change, largest change first."""
    if leverage.empty:
        return pd.DataFrame()
    df = leverage[(leverage["dt"] == day) & leverage["hr"].isin([base_hr, compare_hr])]
    pivot = df.pivot_table(index=["coefficient_type", "iso", "zone", "coefficient"], columns="hr",
                           values=["mw", "leverage"]).reindex(columns=pd.MultiIndex.from_product(
                               [["mw", "leverage"], [base_hr, compare_hr]]))
    pivot.columns = ["base_mw", "compare_mw", "base_leverage", "compare_leverage"]
    pivot = pivot.dropna(subset=["base_leverage", "compare_leverage"])
    pivot["d_leverage"] = pivot["compare_leverage"] - pivot["base_leverage"]
    return pivot.reindex(pivot["d_leverage"].abs().sort_values(ascending=False).index).reset_index()
