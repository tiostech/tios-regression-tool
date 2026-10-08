"""Market-wide conditions for a day: actual energy prices, our energy price forecasts
(RTEP / DAEP), and the stress index, by hour.

- Actual system energy prices: ``{market}_energy_prices`` (DA and RT).
- Our forecasts (RTEP = real-time energy price forecast, DAEP, RTEP uncertainty bands):
  ``{market}_tios_forecast_snapshots``. Forecast names carry a model version that changes
  over time (``tios.rtep.samprf.pm.20241023`` today, something else later), so names are
  never hard-coded here. They are read from the trading configs in ``tios_configs`` as they
  stood on the day (``forecast_names``), falling back to the newest name in the data with
  the same stem. Each forecast is taken as it stood at the day-ahead deadline (the last
  snapshot made by ``AS_OF_HOUR`` on the day before), which is what trading used.
- Stress index: the lion/voltron stress index that ``AnymarketStressIndexRunner`` (tios-core)
  runs through ``coyote_opp_optim_single_market_stress_index_runner`` (tios-quant-analysis),
  published as one CSV per trade date to
  ``s3://tios-quantforecasts-production/working-data/stress-index/{market}/{config}/``.
  The config (folder) name has changed over time (``standard.20240809`` ...
  ``lion-stress-index-v3``), so the folder is chosen per day: the stress-index config in
  effect on that day if its file exists, otherwise the newest file for that day in any
  folder. Inputs are defined by that config in ``tios_configs`` (``"schema": "stress-index"``).
"""

import datetime
import io
import json
import os
import re

import pandas as pd
from sqlalchemy import text

from lib import outage_search as osearch

# Where each forecast role's name is found in the trading configs (tios_configs entries for
# the market): config name prefixes, JSON key suffixes, and the stem the name must start
# with. The first config that yields a name wins.
FORECAST_ROLES = {
    "rtep": {"configs": ("tea-config",), "keys": ("adder_term_rtep_forecast_name", "inc_energy_forecast_type"),
             "stem": "tios.rtep."},
    "daep": {"configs": ("tea-config",), "keys": ("adder_term_daep_forecast_name",), "stem": "tios.daep."},
    "flex_daep": {"configs": ("global-inlineopp-config", "global-config"), "keys": ("energy_forecasts.inc",),
                  "stem": "flex.daep."},
    "rtep_upper": {"configs": ("tea-config",), "keys": ("upper_fc_energy_forecast_type",), "stem": "tios.rtepum."},
    "rtep_lower": {"configs": ("tea-config",), "keys": ("lower_fc_energy_forecast_type",), "stem": "tios.rtepum."},
}
# A model version at the end of a forecast name, e.g. ".20241023" or ".20241023.deltaadjust".
VERSION_SUFFIX = re.compile(r"\.\d{8}(\..*)?$")

# Roles shown by default (the uncertainty bands come in several variants; ask for them by name).
DEFAULT_ROLES = ("rtep", "daep", "flex_daep")

# Hour of the day before the trade date (market time) by which the forecast trading used
# had been made.
AS_OF_HOUR = 11

STRESS_INDEX_BUCKET = os.environ.get("TIOS_STRESS_INDEX_BUCKET", "tios-quantforecasts-production")
STRESS_INDEX_PREFIX = "working-data/stress-index"
AWS_REGION = os.environ.get("TIOS_AWS_REGION", "us-east-1")


def _no_warn(_message):
    pass


def _check_market(market: str) -> None:
    if market not in osearch.MARKETS:
        raise ValueError(f"Unknown market '{market}'. Use one of: {', '.join(osearch.MARKETS)}")


def energy_prices(engine, market: str, day: datetime.date) -> pd.DataFrame:
    """[hr, da_energy_price, rt_energy_price] for the market's system energy price."""
    _check_market(market)
    with engine.connect() as conn:
        return pd.read_sql(text(f"""
            SELECT hr, da_energy_price, rt_energy_price FROM {market}_energy_prices WHERE dt = :d ORDER BY hr
        """), conn, params={"d": day})


# --- CONFIGS ---

def _walk(obj, path=""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")
    else:
        yield path, obj


def configs_as_of(engine, market: str, as_of: datetime.datetime) -> dict[str, dict]:
    """Every ``tios_configs`` entry for the market as it stood at ``as_of``: {name: parsed
    JSON}, using the newest version created by then (or the current one if none was)."""
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT name, config, created_at, current FROM tios_configs WHERE name LIKE :m ORDER BY name, created_at
        """), {"m": f"%.{market}"}).fetchall()
    chosen = {}
    for name, cfg, created, current in rows:
        if created is not None and created <= as_of:
            chosen[name] = cfg
        elif name not in chosen and current:
            chosen.setdefault(name, cfg)
    out = {}
    for name, cfg in chosen.items():
        try:
            out[name] = json.loads(cfg)
        except (TypeError, ValueError):
            continue
    return out


def _deadline(day: datetime.date) -> datetime.datetime:
    return datetime.datetime.combine(day - datetime.timedelta(days=1), datetime.time(AS_OF_HOUR))


def forecast_names(engine, market: str, day: datetime.date, roles=DEFAULT_ROLES, warn=_no_warn) -> dict[str, dict]:
    """The forecast name for each role on ``day``: {role: {"name", "source"}}.

    Read from the trading configs in effect at the day-ahead deadline; when a role is not in
    the configs, the newest name in the data with the role's stem is used instead.
    """
    _check_market(market)
    configs = configs_as_of(engine, market, _deadline(day))
    found, families = {}, {}
    for role in roles:
        spec = FORECAST_ROLES[role]
        for cfg_name in sorted(configs):
            if not any(cfg_name.startswith(prefix + ".") for prefix in spec["configs"]):
                continue
            values = [str(v).split("#")[0] for path, v in _walk(configs[cfg_name])
                      if isinstance(v, str) and any(path.endswith(k) for k in spec["keys"])
                      and str(v).startswith(spec["stem"])]
            if values:
                name = max(set(values), key=values.count)
                found[role] = {"name": name, "source": f"tios_configs {cfg_name}"}
                break
    with engine.connect() as conn:
        # A config can name a model that had no forecasts yet on the day (e.g. when only a
        # newer config version exists), so check each name against the data.
        for role, info in list(found.items()):
            exists = conn.execute(text(f"""
                SELECT 1 FROM {market}_tios_forecast_snapshots WHERE dt = :d AND name = :n LIMIT 1
            """), {"d": day, "n": info["name"]}).fetchone()
            if not exists:
                warn(f"{role}: {info['name']} (from {info['source']}) has no forecasts for {day}; "
                     "using the newest version of that model in the data instead.")
                families[role] = VERSION_SUFFIX.sub("", info["name"])
                del found[role]
        missing = [r for r in roles if r not in found]
        if missing:
            for role in missing:
                stem = families.get(role, FORECAST_ROLES[role]["stem"])
                name = conn.execute(text(f"""
                    SELECT name FROM {market}_tios_forecast_snapshots
                    WHERE dt = :d AND name LIKE :p ORDER BY name DESC, forecast_time DESC LIMIT 1
                """), {"d": day, "p": stem.replace("_", "\\_") + "%"}).scalar()
                if name:
                    found[role] = {"name": name, "source": "newest in data (not in configs)"}
                else:
                    warn(f"No {role} forecast name found in the configs or the data for {day}.")
    return found


def energy_forecasts(engine, market: str, day: datetime.date, names: dict[str, str] | None = None,
                     as_of: datetime.datetime | None = None, warn=_no_warn) -> tuple[dict, pd.DataFrame]:
    """Our energy price forecasts for ``day`` as they stood at ``as_of`` (default: the
    day-ahead deadline). ``names`` maps a column name to a forecast name, optionally with a
    ``#column`` suffix as in the configs (e.g. ``#upper_50``); by default the names come from
    ``forecast_names``. Returns ({column: {"name", "source"}}, DataFrame [hr, column...])."""
    _check_market(market)
    as_of = as_of or _deadline(day)
    if names:
        used = {col: {"name": n, "source": "given"} for col, n in names.items()}
    else:
        used = forecast_names(engine, market, day, warn=warn)
    frames = []
    with engine.connect() as conn:
        for col, info in used.items():
            name, _, value_col = info["name"].partition("#")
            value_col = value_col or "forecast"
            if value_col not in VALUE_COLUMNS:
                warn(f"Unknown forecast column '{value_col}' in '{info['name']}'.")
                continue
            df = pd.read_sql(text(f"""
                SELECT s.hr, s.{value_col} AS value
                FROM {market}_tios_forecast_snapshots s
                JOIN (SELECT hr, MAX(forecast_time) AS ft FROM {market}_tios_forecast_snapshots
                      WHERE dt = :d AND name = :n AND forecast_time <= :a GROUP BY hr) best
                  ON best.hr = s.hr AND best.ft = s.forecast_time
                WHERE s.dt = :d AND s.name = :n
            """), conn, params={"d": day, "n": name, "a": as_of})
            if df.empty:
                warn(f"No snapshot of {name} made by {as_of:%Y-%m-%d %H:%M} for {day}.")
                continue
            frames.append(df.groupby("hr", as_index=False)["value"].mean().rename(columns={"value": col}))
    if not frames:
        return used, pd.DataFrame(columns=["hr"])
    out = frames[0]
    for f in frames[1:]:
        out = out.merge(f, on="hr", how="outer")
    return used, out.sort_values("hr").reset_index(drop=True)


VALUE_COLUMNS = {"forecast"} | {f"{side}_{p}" for side in ("lower", "upper") for p in (50, 60, 70, 75, 80, 85, 90, 95, 99)}


def stress_index(engine, market: str, day: datetime.date, warn=_no_warn) -> tuple[str | None, pd.DataFrame]:
    """The stress index for trade date ``day``: (s3 path used, one row per hour with
    stress_level and its inputs). Columns vary by market and config; empty when no file."""
    _check_market(market)
    import pyarrow.fs as pafs
    fs = pafs.S3FileSystem(region=AWS_REGION)
    base = f"{STRESS_INDEX_BUCKET}/{STRESS_INDEX_PREFIX}/{market}"
    filename = f"{market}-stress-index-preview-for-{day:%Y-%m-%d}.csv"
    try:
        folders = [i.base_name for i in fs.get_file_info(pafs.FileSelector(base)) if i.type == pafs.FileType.Directory]
    except Exception as e:
        warn(f"Could not list s3://{base}: {e}")
        return None, pd.DataFrame()

    in_effect = [name.rsplit(".", 1)[0] for name, cfg in configs_as_of(engine, market, _deadline(day)).items()
                 if isinstance(cfg, dict) and cfg.get("schema") == "stress-index"]
    candidates = []
    for info in fs.get_file_info([f"{base}/{folder}/{filename}" for folder in folders]):
        if info.type == pafs.FileType.File:
            folder = info.path.split("/")[-2]
            candidates.append((folder in in_effect, info.mtime, info.path))
    if not candidates:
        warn(f"No stress index file for {day} under s3://{base}/.")
        return None, pd.DataFrame()
    path = max(candidates)[2]
    if not max(candidates)[0] and in_effect:
        warn(f"Stress index for {day} is not under the config in effect ({', '.join(in_effect)}); using {path}.")
    try:
        with fs.open_input_stream(path) as f:
            df = pd.read_csv(io.BytesIO(f.read()))
    except Exception as e:
        warn(f"Could not read s3://{path}: {e}")
        return None, pd.DataFrame()
    return f"s3://{path}", df.drop(columns=[c for c in ("time", "dt", "generated_time") if c in df.columns])


def market_conditions(engine, market: str, day: datetime.date, names: dict[str, str] | None = None,
                      warn=_no_warn) -> tuple[dict, pd.DataFrame]:
    """Hourly energy prices, our forecasts, and the stress index for ``day`` in one table:
    [hr, da_energy_price, rt_energy_price, <forecast columns>, rt_minus_rtep, stress_level,
    reserve margin, net load, ...]. Returns (sources, table), where sources maps each
    forecast column to its name and where it came from, plus "stress_index" to its S3 path."""
    prices = energy_prices(engine, market, day)
    used, forecasts = energy_forecasts(engine, market, day, names=names, warn=warn)
    stress_path, stress = stress_index(engine, market, day, warn=warn)
    df = prices.merge(forecasts, on="hr", how="outer") if not forecasts.empty else prices
    if "rtep" in df and "rt_energy_price" in df:
        df["rt_minus_rtep"] = df["rt_energy_price"] - df["rtep"]
    sources = dict(used)
    if not stress.empty:
        sources["stress_index"] = {"name": stress_path, "source": "S3"}
        keep = ["hr", "stress_level"] + [c for c in stress.columns if "reserve_margin" in c or "net_load" in c
                                         or c.endswith("_trigger") or "feelslike" in c]
        df = df.merge(stress[[c for c in dict.fromkeys(keep) if c in stress.columns]], on="hr", how="left")
    df = df.dropna(subset=["hr"])
    df["hr"] = df["hr"].astype(int)
    return sources, df.sort_values("hr").reset_index(drop=True)
