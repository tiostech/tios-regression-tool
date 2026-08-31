"""Reference data shared across tools: plant lists and forecast region lists.

``load_metadata()`` sweeps every ISO for the generators and regions the tools
offer in their dropdowns. That is roughly 60 small queries, so it is cached with
``st.cache_data`` -- the sweep runs once per TTL for the whole app rather than
once per browser tab.
"""

import pandas as pd
import streamlit as st

from lib.db import engine

ISOS = ["caiso", "ercot", "isone", "miso", "nyiso", "pjm", "spp"]

VENDOR_CONFIG = {
    "gs": {
        "meta_table": "miso_gs_plants",
        "meta_id": "plant_id",
        "meta_name": "plant_name",
        "meta_extra": None,
        "data_table": "miso_gs_plant_hourly_outputs",
        "data_id": "plant_id",
        "data_mw": "avg_output_mw",
    },
    "lpi": {
        "meta_table": "miso_lpi_gen_units",
        "meta_id": "lpi_genunit_id",
        "meta_name": "name",
        "meta_extra": None,
        "data_table": "miso_lpi_gen",
        "data_id": "lpi_genunit_id",
        "data_mw": "avg_mw",
    },
    "muse": {
        "meta_table": "miso_muse_plants",
        "meta_id": "id",
        "meta_name": "name",
        "meta_extra": "label",
        "data_table": "miso_muse_plant_hourly_outputs",
        "data_id": "plant_id",
        "data_mw": "avg_output_mw",
    },
}

# How long the reference-data sweep stays cached. Plant and region lists change
# rarely, so an hour keeps the tools responsive without going stale.
METADATA_TTL = 3600


@st.cache_data(ttl=METADATA_TTL, show_spinner="Loading plant and region lists...")
def load_metadata():
    """
    Returns a dict of dropdown options:

        {
          "plants":  {vendor: {display_name: {"id": ..., "iso": ...}}},
          "wind":    ["miso: meteologica ...", ...],
          "solar":   [...],
          "load":    [...],
          "outage":  [...],
          "maxar":   [...],
        }

    Individual per-ISO queries are allowed to fail -- not every ISO has every
    table -- so a missing table drops that one list rather than the whole sweep.
    """
    eng = engine()

    plants = {v_key: {} for v_key in VENDOR_CONFIG.keys()}
    wind_regs, solar_regs, load_regs, outage_regs, maxar_regs = [], [], [], [], []

    for iso in ISOS:
        for v_key, cfg in VENDOR_CONFIG.items():
            base_meta_table = cfg["meta_table"].replace("miso_", "")
            dynamic_meta_table = f"{iso}_{base_meta_table}"
            cols = [cfg["meta_id"], cfg["meta_name"]]
            if cfg["meta_extra"]:
                cols.append(cfg["meta_extra"])

            try:
                query = f"SELECT {', '.join(cols)} FROM {dynamic_meta_table}"
                df = pd.read_sql(query, eng)
                for _, row in df.iterrows():
                    if v_key == "muse" and cfg["meta_extra"] in df.columns:
                        display_name = f"[{iso.upper()}] {row[cfg['meta_name']]} - {row[cfg['meta_extra']]} [{row[cfg['meta_id']]}]"
                    else:
                        display_name = f"[{iso.upper()}] {row[cfg['meta_name']]} [{row[cfg['meta_id']]}]"

                    plants[v_key][display_name] = {
                        "id": row[cfg["meta_id"]],
                        "iso": iso,
                    }
            except Exception:
                pass

        try:
            w_df = pd.read_sql(
                f"SELECT DISTINCT region FROM {iso}_meteologica_wind_forecasts",
                eng,
            )
            wind_regs.extend(
                [
                    f"{iso}: {r}"
                    for r in w_df["region"].tolist()
                    if r.lower().startswith("meteologica")
                ]
            )
        except Exception:
            pass

        try:
            s_df = pd.read_sql(
                f"SELECT DISTINCT region FROM {iso}_meteologica_solar_forecasts",
                eng,
            )
            solar_regs.extend([f"{iso}: {r}" for r in s_df["region"].tolist()])
        except Exception:
            pass

        try:
            if iso == "miso":
                # 1. Fetch PRT BA load regions
                prt_df = pd.read_sql(
                    "SELECT DISTINCT region_zone AS region FROM miso_prt_loadtemp_forecasts",
                    eng,
                )
                load_regs.extend([f"miso: PRT - {r}" for r in prt_df["region"].tolist() if r])

                # 2. Fetch Meteologica load regions (North, Central, South, ALL, LRZs)
                meteo_df = pd.read_sql(
                    "SELECT DISTINCT region FROM miso_meteologica_load_forecasts",
                    eng,
                )
                load_regs.extend([f"miso: {r}" for r in meteo_df["region"].tolist() if r])
            else:
                l_df = pd.read_sql(
                    f"SELECT DISTINCT region FROM {iso}_meteologica_load_forecasts",
                    eng,
                )
                load_regs.extend([f"{iso}: {r}" for r in l_df["region"].tolist() if r])
        except Exception:
            pass

        try:
            o_df = pd.read_sql(
                f"SELECT DISTINCT region FROM {iso}_totalgen_on_outages",
                eng,
            )
            outage_regs.extend(
                [f"{iso}: {r}" for r in o_df["region"].tolist() if r]
            )
        except Exception:
            pass

        try:
            m_df = pd.read_sql(f"SELECT id, name FROM {iso}_locations WHERE is_maxar = 1", eng)
            for _, row in m_df.iterrows():
                maxar_regs.extend([f"[{iso.upper()}] {row['name']} ({int(row['id'])})"])
        except Exception:
            try:
                m_df = pd.read_sql(f"SELECT DISTINCT location_id FROM {iso}_maxar_weather_histories", eng)
                for loc in m_df["location_id"].tolist():
                    maxar_regs.extend([f"[{iso.upper()}] Location {int(loc)} ({int(loc)})"])
            except Exception:
                pass

    return {
        "plants": plants,
        "wind": sorted(wind_regs),
        "solar": sorted(solar_regs),
        "load": sorted(load_regs),
        "outage": sorted(outage_regs),
        "maxar": sorted(maxar_regs),
    }
