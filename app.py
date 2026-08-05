import streamlit as st
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import math
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from xgboost import XGBRegressor
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import r2_score, mean_absolute_error
from sklearn.impute import SimpleImputer
from datetime import timedelta, datetime
from sklearn.model_selection import TimeSeriesSplit, RandomizedSearchCV
import sys
import os
import re
import yaml

# ==============================================================================
# 🔍 MISO CUSTOM PATH: SHADOW PRICE CONSTRAINTS CONFIG
# ==============================================================================
# Add, change, or remove MISO constraint IDs here to adjust the target variable 
# summed up for 'RDT_DA_PRICES'.
# ==============================================================================
MISO_CUSTOM_CONSTRAINTS = [ 29321,224129,224135,224133,224129,224130,224132,224135,224134,15625]
# ==============================================================================


def _mysql_config_path():
    """Path to the MySQL config, kept alongside app.py in config/mysql.yml."""
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "config", "mysql.yml"
    )


def load_mysql_config():
    """
    Loads the MySQL connection settings from config/mysql.yml.

    Expected structure:
        mysql_slave:
          host: 127.0.0.1
          port: 3309
          database: tioscore_production
          username: <user>
          password: <pass>
    Returns the mysql_slave dict (empty dict if the file is missing/invalid).
    """
    config_path = _mysql_config_path()

    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f:
                config_data = yaml.safe_load(f) or {}
            return config_data.get("mysql_slave", {}) or {}
        except Exception as e:
            st.sidebar.error(f"Error parsing YAML config: {e}")
            return {}
    else:
        st.sidebar.warning(f"Config file not found at: {config_path}")
    return {}


def load_credentials_from_file():
    """Returns (username, password) from config/mysql.yml for the sidebar."""
    creds = load_mysql_config()
    return creds.get("username", ""), creds.get("password", "")


st.set_page_config(
    page_title="Regression Tool", layout="wide", initial_sidebar_state="expanded"
)

st.markdown(
    """
<style>
    /* Widens the sidebar container */
    [data-testid="stSidebar"] {
        min-width: 400px;
        max-width: 400px;
    }
    
    /* Style for multi-select tags */
    span[data-baseweb="tag"] { 
        background-color: #e0e7ff !important; 
        color: #3730a3 !important; 
        border: 1px solid #c7d2fe !important; 
    }

    /* Fix tag cut-off */
    span[data-baseweb="tag"] > span {
        max-width: 100% !important;
        overflow: visible !important;
        text-overflow: clip !important;
        white-space: normal !important;
    }
    
    div[data-baseweb="select"] {
        line-height: 1.4;
    }
</style>
""",
    unsafe_allow_html=True,
)

st.title("Regression Tool")

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


def get_engine(uid, pwd):
    cfg = load_mysql_config()
    host = cfg.get("host", "127.0.0.1")
    port = cfg.get("port", 3309)
    database = cfg.get("database", "tioscore_production")
    return create_engine(
        f"mysql+pymysql://{uid}:{pwd}@{host}:{port}/{database}",
        pool_pre_ping=True,
    )


def parse_custom_functions(text):
    """Parses custom variable lines: var_name = expression"""
    funcs = {}
    if not text:
        return funcs
    for line in text.split("\n"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        try:
            var_name, expr = line.split("=", 1)
            funcs[var_name.strip()] = expr.strip()
        except Exception:
            pass
    return funcs


def extract_required_variables(custom_funcs_dict):
    """Extracts strings matching patterns like miso_load_ALTW, ignoring functions and hidden local variables"""
    variables = set()
    defined_custom_vars = list(custom_funcs_dict.keys())

    for expr in custom_funcs_dict.values():
        found = re.findall(r"[a-zA-Z0-9_:\-]+", expr)
        for f in found:
            if f == "-":
                continue
            if (
                not f.replace(".", "", 1).isdigit()
                and f not in ["rolling_average", "mean"]
                and not f.startswith("_")
                and f not in defined_custom_vars
            ):
                variables.add(f)
    return list(variables)


def normalize_var_to_column(var_str):
    """Converts user styles (miso_load_ALTW or miso_wind_Meteologica_North_Iowa) into internal columns"""
    var_clean = var_str.replace(":", "").strip().lower()
    var_clean = re.sub(r"[^a-zA-Z0-9_]", "_", var_clean)
    parts = var_clean.split("_")

    if len(parts) >= 3:
        iso, vtype = parts[0], parts[1]
        rest = "_".join(parts[2:])
        if vtype in ["load", "wind", "solar", "outage"] and iso in ISOS:
            return f"{vtype}_{iso}_{rest}", iso, vtype, rest

    return None, None, None, None


def pull_meteo_features(
    engine, start_dt, end_dt, w_vars, l_vars, s_vars, o_vars, maxar_cities=[], custom_vars_text=""
):
    """
    Parses prefixed variables (formatted as 'iso: region') and gathers weather/load metrics
    dynamically across multiple ISO tables, merging them into a unified frame.
    """
    custom_dict = parse_custom_functions(custom_vars_text)
    extra_vars = extract_required_variables(custom_dict)

    w_vars_working = list(w_vars)
    l_vars_working = list(l_vars)
    s_vars_working = list(s_vars)
    o_vars_working = list(o_vars)

    for ev in extra_vars:
        col_name, iso, vtype, rest = normalize_var_to_column(ev)
        if col_name:
            parts = ev.split("_", 2)
            raw_region = parts[2] if len(parts) > 2 else rest

            reconstructed_space = f"{iso}: {raw_region.replace('_', ' ')}"
            reconstructed_underscore = f"{iso}: {raw_region}"

            if vtype == "wind":
                target_list = w_vars_working
            elif vtype == "load":
                target_list = l_vars_working
            elif vtype == "solar":
                target_list = s_vars_working
            else:
                target_list = o_vars_working

            if reconstructed_space not in target_list:
                target_list.append(reconstructed_space)
            if reconstructed_underscore not in target_list:
                target_list.append(reconstructed_underscore)

    def parse_prefixed_selections(selections):
        mapping = {}
        for item in selections:
            if ":" in item:
                iso, reg = item.split(":", 1)
                mapping.setdefault(iso.strip(), []).append(
                    re.sub(r"\s+", " ", reg.strip())
                )
        return mapping

    def parse_maxar_selections(selections):
        mapping = {}
        for item in selections:
            match = re.search(r"\[([a-zA-Z0-9]+)\]\s+.*\((\d+)\)", item)
            if match:
                iso = match.group(1).lower()
                loc_id = match.group(2)
                mapping.setdefault(iso, []).append(loc_id)
        return mapping

    def fetch_and_pivot_all_isos(target_config, prefix):
        master_pivot = pd.DataFrame()
        iso_mapping = parse_prefixed_selections(target_config)

        for iso, regions in iso_mapping.items():
            if not regions:
                continue

            reg_list = "', '".join(regions)

            if prefix == "load" and iso == "miso":
                table = "miso_prt_loadtemp_forecasts"
                region_col = "region_zone"
                value_col = "load_mwh"
            elif prefix == "outage":
                table = f"{iso}_totalgen_on_outages"
                region_col = "region"
                value_col = "capacity_on_outage_mw"
            else:
                table = f"{iso}_meteologica_{prefix}_forecasts"
                region_col = "region"
                value_col = "power_mw"

            query = f"SELECT dt, hr, {region_col} AS region, {value_col} AS target_val FROM {table} WHERE {region_col} IN ('{reg_list}') AND dt BETWEEN '{start_dt}' AND '{end_dt}'"
            try:
                df = pd.read_sql(query, engine)
                if df.empty:
                    continue

                df = df.groupby(["dt", "hr", "region"], as_index=False)["target_val"].mean()

                df["region"] = (
                    f"{iso}_"
                    + df["region"]
                    .str.replace(r"[^a-zA-Z0-9_]", "_", regex=True)
                    .str.lower()
                )
                df["dt"] = pd.to_datetime(df["dt"])
                df["hr"] = df["hr"].astype(int)

                pivot = df.pivot(
                    index=["dt", "hr"], columns="region", values="target_val"
                ).reset_index()
                pivot.columns = [
                    f"{prefix}_{c}" if c not in ["dt", "hr"] else c
                    for c in pivot.columns
                ]

                if master_pivot.empty:
                    master_pivot = pivot
                else:
                    master_pivot = master_pivot.merge(
                        pivot, on=["dt", "hr"], how="outer"
                    )
            except Exception:
                pass
        return master_pivot

    wind = fetch_and_pivot_all_isos(w_vars_working, "wind")
    load = fetch_and_pivot_all_isos(l_vars_working, "load")
    solar = fetch_and_pivot_all_isos(s_vars_working, "solar")
    outage = fetch_and_pivot_all_isos(o_vars_working, "outage")

    maxar_pivot = pd.DataFrame()
    if maxar_cities:
        maxar_mapping = parse_maxar_selections(maxar_cities)
        
        for iso, locations in maxar_mapping.items():
            if not locations:
                continue
            
            loc_ids_str = ", ".join([str(loc) for loc in locations])
            
            hist_query = f"""
                SELECT dt, hr, location_id, feelslike 
                FROM {iso}_maxar_weather_histories 
                WHERE location_id IN ({loc_ids_str}) AND dt BETWEEN '{start_dt}' AND '{end_dt}'
            """
            
            fc_query = f"""
                SELECT f.dt, f.hr, f.location_id, f.feelslike 
                FROM {iso}_maxar_weather_forecasts f
                INNER JOIN (
                    SELECT dt, hr, location_id, MAX(forecast_time) as max_ft
                    FROM {iso}_maxar_weather_forecasts
                    WHERE forecast_time <= CONCAT(dt, ' 06:00:00') AND location_id IN ({loc_ids_str}) AND dt BETWEEN '{start_dt}' AND '{end_dt}'
                    GROUP BY dt, hr, location_id
                ) latest ON f.dt = latest.dt AND f.hr = latest.hr AND f.location_id = latest.location_id AND f.forecast_time = latest.max_ft
            """
            
            try:
                df_maxar = pd.read_sql(hist_query, engine)
                
                if df_maxar.empty:
                    df_maxar = pd.read_sql(fc_query, engine)
                    
                if df_maxar.empty:
                    continue
                    
                maxar_name_map = {}
                for item in maxar_cities:
                    match = re.search(r"\[([a-zA-Z0-9]+)\]\s+([^\(]+).*\((\d+)\)", item)
                    if match:
                        city_clean = re.sub(r"[^a-zA-Z0-9]", "_", match.group(2).strip()).lower()
                        city_clean = re.sub(r"_+", "_", city_clean).strip("_")
                        maxar_name_map[str(match.group(3))] = f"maxar_{city_clean}"

                df_maxar["region"] = df_maxar["location_id"].astype(str).map(maxar_name_map).fillna(
                    f"maxar_{iso}_loc_" + df_maxar["location_id"].astype(str)
                )
                
                df_maxar["dt"] = pd.to_datetime(df_maxar["dt"])
                df_maxar["hr"] = df_maxar["hr"].astype(int)
                
                pivot = df_maxar.pivot(
                    index=["dt", "hr"], columns="region", values="feelslike"
                ).reset_index()
                
                if maxar_pivot.empty:
                    maxar_pivot = pivot
                else:
                    maxar_pivot = maxar_pivot.merge(pivot, on=["dt", "hr"], how="outer")
            except Exception:
                pass

    f_df = pd.DataFrame()
    for other in [wind, load, solar, outage, maxar_pivot]:
        if other is not None and not other.empty:
            f_df = (
                other if f_df.empty else f_df.merge(other, on=["dt", "hr"], how="outer")
            )

    if not f_df.empty:
        # Drop any duplicate rows for the same dt and hr from SQL outer joins
        f_df = f_df.drop_duplicates(subset=["dt", "hr"]).reset_index(drop=True)

    if not f_df.empty and custom_dict:
        f_df["time_temp"] = pd.to_datetime(f_df["dt"]) + f_df["hr"].apply(lambda x: timedelta(hours=int(x) - 1))
        
        # Deduplicate on timestamp before indexing
        f_df = f_df.drop_duplicates(subset=["time_temp"]).sort_values("time_temp").set_index("time_temp")
        
        full_time_idx = pd.date_range(start=f_df.index.min(), end=f_df.index.max(), freq="1h")
        f_df = f_df.reindex(full_time_idx)
        f_df["dt"] = pd.to_datetime(f_df.index.date)
        f_df["hr"] = f_df.index.hour + 1

        for custom_var, expression in custom_dict.items():
            evaluated_expr = expression

            rolling_matches = re.findall(
                r"rolling_average\(\s*([a-zA-Z0-9_:]+)\s*,\s*(\d+)\s*\)", evaluated_expr
            )
            for raw_var, window_str in rolling_matches:
                col_target, _, _, _ = normalize_var_to_column(raw_var)
                window_size = int(window_str)

                actual_col = (
                    col_target
                    if (col_target in f_df.columns)
                    else (raw_var if raw_var in f_df.columns else None)
                )

                if actual_col:
                    temp_roll_col = f"temp_roll_{actual_col}_{window_size}"
                    f_df[temp_roll_col] = (
                        f_df[actual_col]
                        .shift(1)
                        .rolling(window=f"{window_size}h", min_periods=1)
                        .mean()
                    )

                    func_pattern = rf"rolling_average\(\s*{re.escape(raw_var)}\s*,\s*{window_size}\s*\)"
                    evaluated_expr = re.sub(func_pattern, temp_roll_col, evaluated_expr)

            tokens = sorted(
                list(re.findall(r"[a-zA-Z0-9_:\-]+", evaluated_expr)),
                key=len,
                reverse=True,
            )
            for tok in tokens:
                if tok == "-":
                    continue
                col_target, _, _, _ = normalize_var_to_column(tok)

                if col_target and col_target in f_df.columns:
                    evaluated_expr = re.sub(
                        r"\b" + re.escape(tok) + r"\b",
                        f"`{col_target}`",
                        evaluated_expr,
                    )
                elif tok in f_df.columns:
                    evaluated_expr = re.sub(
                        r"\b" + re.escape(tok) + r"\b", f"`{tok}`", evaluated_expr
                    )

            evaluated_expr = re.sub(r"\bx\b", "*", evaluated_expr)
            try:
                f_df[custom_var] = f_df.eval(evaluated_expr)
            except Exception as eval_err:
                st.sidebar.error(
                    f"Failed to evaluate expression for {custom_var}: {eval_err}"
                )

        temp_cols = [c for c in f_df.columns if c.startswith("temp_roll_")]
        if temp_cols:
            f_df.drop(columns=temp_cols, inplace=True)

        hidden_user_cols = [c for c in f_df.columns if c.startswith("_")]
        if hidden_user_cols:
            f_df.drop(columns=hidden_user_cols, inplace=True)

        f_df = f_df.reset_index(drop=True)

    return f_df


def apply_time_features(df, selected_time_features):
    dt_series = pd.to_datetime(df["dt"])
    df["dow_sin"] = np.sin(2 * np.pi * dt_series.dt.dayofweek / 7.0)
    df["dow_cos"] = np.cos(2 * np.pi * dt_series.dt.dayofweek / 7.0)
    df["is_weekend"] = dt_series.dt.dayofweek.isin([5, 6]).astype(int)

    if selected_time_features and "Hour of Day" in selected_time_features:
        hr_ser = df["hr"].astype(int)
        df["time_hour_sin"] = np.sin(2 * np.pi * hr_ser / 24.0)
        df["time_hour_cos"] = np.cos(2 * np.pi * hr_ser / 24.0)
    if selected_time_features and "Is On-Peak (HE07-HE22)" in selected_time_features:
        is_peak_hr = df["hr"].astype(int).between(7, 22)
        df["time_is_on_peak"] = is_peak_hr.astype(int)
    return df


st.sidebar.header("1. Authentication")
file_user, file_pass = load_credentials_from_file()

db_user = st.sidebar.text_input("Username", value=file_user, key="auth_user")
db_pass = st.sidebar.text_input(
    "Password", type="password", value=file_pass, key="auth_pass"
)

if "plant_options" not in st.session_state:
    st.session_state.plant_options = {}
if "dynamic_wind_regions" not in st.session_state:
    st.session_state.dynamic_wind_regions = []
if "dynamic_load_regions" not in st.session_state:
    st.session_state.dynamic_load_regions = []
if "dynamic_outage_regions" not in st.session_state:
    st.session_state.dynamic_outage_regions = []
if "dynamic_solar_regions" not in st.session_state:
    st.session_state.dynamic_solar_regions = []
if "dynamic_maxar_locations" not in st.session_state:
    st.session_state.dynamic_maxar_locations = []
if "connections_established" not in st.session_state:
    st.session_state.connections_established = False
if "show_help" not in st.session_state:
    st.session_state.show_help = False

auto_connect = bool(
    file_user and file_pass and not st.session_state.connections_established
)

if st.sidebar.button("Establish Connection", key="conn_btn") or auto_connect:
    if not db_user or not db_pass:
        st.sidebar.error("Enter credentials first.")
    else:
        try:
            temp_engine = get_engine(db_user, db_pass)
            new_options = {v_key: {} for v_key in VENDOR_CONFIG.keys()}

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
                        df = pd.read_sql(query, temp_engine)
                        for _, row in df.iterrows():
                            if v_key == "muse" and cfg["meta_extra"] in df.columns:
                                display_name = f"[{iso.upper()}] {row[cfg['meta_name']]} - {row[cfg['meta_extra']]} [{row[cfg['meta_id']]}]"
                            else:
                                display_name = f"[{iso.upper()}] {row[cfg['meta_name']]} [{row[cfg['meta_id']]}]"

                            new_options[v_key][display_name] = {
                                "id": row[cfg["meta_id"]],
                                "iso": iso,
                            }
                    except Exception:
                        pass

                try:
                    w_df = pd.read_sql(
                        f"SELECT DISTINCT region FROM {iso}_meteologica_wind_forecasts",
                        temp_engine,
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
                        temp_engine,
                    )
                    solar_regs.extend([f"{iso}: {r}" for r in s_df["region"].tolist()])
                except Exception:
                    pass

                try:
                    if iso == "miso":
                        l_df = pd.read_sql(
                            "SELECT DISTINCT region_zone AS region FROM miso_prt_loadtemp_forecasts",
                            temp_engine,
                        )
                    else:
                        l_df = pd.read_sql(
                            f"SELECT DISTINCT region FROM {iso}_meteologica_load_forecasts",
                            temp_engine,
                        )
                    load_regs.extend([f"{iso}: {r}" for r in l_df["region"].tolist()])
                except Exception:
                    pass

                try:
                    o_df = pd.read_sql(
                        f"SELECT DISTINCT region FROM {iso}_totalgen_on_outages",
                        temp_engine,
                    )
                    outage_regs.extend(
                        [f"{iso}: {r}" for r in o_df["region"].tolist() if r]
                    )
                except Exception:
                    pass

                try:
                    m_df = pd.read_sql(f"SELECT id, name FROM {iso}_locations WHERE is_maxar = 1", temp_engine)
                    for _, row in m_df.iterrows():
                        maxar_regs.extend([f"[{iso.upper()}] {row['name']} ({int(row['id'])})"])
                except Exception:
                    try:
                        m_df = pd.read_sql(f"SELECT DISTINCT location_id FROM {iso}_maxar_weather_histories", temp_engine)
                        for loc in m_df["location_id"].tolist():
                            maxar_regs.extend([f"[{iso.upper()}] Location {int(loc)} ({int(loc)})"])
                    except Exception:
                        pass

            st.session_state.plant_options = new_options
            st.session_state.dynamic_wind_regions = sorted(wind_regs)
            st.session_state.dynamic_solar_regions = sorted(solar_regs)
            st.session_state.dynamic_load_regions = sorted(load_regs)
            st.session_state.dynamic_outage_regions = sorted(outage_regs)
            st.session_state.dynamic_maxar_locations = sorted(maxar_regs)
            st.session_state.connections_established = True
            st.sidebar.success("Successful connection")

            if auto_connect:
                st.rerun()
        except Exception as e:
            st.sidebar.error(f"Connection Failed: {e}")

st.sidebar.header("2. Plant Selection")
vendor_choice = st.sidebar.selectbox(
    "Data Vendor",
    list(VENDOR_CONFIG.keys()),
    format_func=lambda x: x.upper(),
    key="v_sel",
)
current_vendor_options = st.session_state.plant_options.get(vendor_choice, {})

gen_id = None
target_iso = None

if current_vendor_options:
    dropdown_options = list(current_vendor_options.keys()) + ["RDT_DA_PRICES"]
    
    selected_display = st.sidebar.selectbox(
        "Select Generator",
        options=dropdown_options,
        index=None,
        placeholder="Search Name, Label or ISO...",
        key="g_sel",
    )
    if selected_display:
        if selected_display == "RDT_DA_PRICES":
            gen_id = "RDT_DA_PRICES"
            target_iso = "miso"
        else:
            gen_id = current_vendor_options[selected_display]["id"]
            target_iso = current_vendor_options[selected_display]["iso"]

if not gen_id:
    st.sidebar.warning("⚠️ Select a plant to begin.")

st.sidebar.header("3. Config & Dates")
model_choice = st.sidebar.selectbox("Model", ["Random Forest", "XGBoost"], key="m_sel")
tune_model = True

st.sidebar.header("4. Feature Selection")
selected_wind = st.sidebar.multiselect(
    "Wind Variables (ISO: Region)",
    options=st.session_state.dynamic_wind_regions,
    default=[],
)
selected_load = st.sidebar.multiselect(
    "Load Variables (ISO: Region)",
    options=st.session_state.dynamic_load_regions,
    default=[],
)
selected_solar = st.sidebar.multiselect(
    "Solar Variables (ISO: Region)",
    options=st.session_state.dynamic_solar_regions,
    default=[],
)
selected_outage = st.sidebar.multiselect(
    "Gen on Outage (ISO: Region)",
    options=st.session_state.dynamic_outage_regions,
    default=[],
)

selected_maxar = st.sidebar.multiselect(
    "Maxar Feelslike (City / Location)",
    options=st.session_state.dynamic_maxar_locations,
    default=[],
)

TIME_OPTIONS = ["Hour of Day", "Is On-Peak (HE07-HE22)"]
selected_time_vars = st.sidebar.multiselect(
    "Time Variables", options=TIME_OPTIONS, default=[]
)

st.sidebar.markdown("---")
hc1, hc2 = st.sidebar.columns([0.85, 0.15])
with hc1:
    st.markdown("### 4b. Custom Functions")
with hc2:
    if st.button("❓", help="Click to open/close variable calculation guide"):
        st.session_state.show_help = not st.session_state.show_help

if st.session_state.show_help:
    st.sidebar.info("""
    **Custom Expression Builder Help**
    * **Format:** One equation per line.
    * **Rolling Averages:** Use `rolling_average(variable, window)`.
    * **The Window Parameter:** The second parameter (e.g., `3`) specifies the lookback period in hours.
    * **The Underscore (`_`) Prefix:** Start an internal, hidden variable with an underscore in order to use a custom variable in a rolling average.
    
    **Syntax Examples:**
    * **Standard Formula:**
      `IOWA_STRESS = (miso_load_ALTW + miso_load_MEC) - miso_wind_Meteologica_North_Iowa`
    * **Simple Rolling Average (3-Hour Window):**
      `ALTW_3HR_ROLL_AVG = rolling_average(miso_load_ALTW, 3)`
    * **Multi-Step Rolling Average:**
      `_IOWA_STRESS = (miso_load_ALTW + miso_load_MEC) - miso_wind_Meteologica_North_Iowa`
      `IOWA_STRESS_3HR = rolling_average(_IOWA_STRESS, 3)`
        
        Note that in this case, the model only sees IOWA_STRESS_3HR, not _IOWA_STRESS due to the _ at the beggining

        I recommend saving frequently used formulas in a notes app for quick access (e.g., IOWA_STRESS is commonly used for some of our MISO gen forecasts).
    """)

custom_vars_input = st.sidebar.text_area(
    "Custom Formulas",
    value="",
    placeholder="IOWA_LOAD_SUM_norm = (0.8 * miso_load_MEC) + miso_load_ALTW",
    height=120,
    key="custom_vars_text",
)
st.sidebar.markdown("---")

st.sidebar.header("5. Lookback")
today = datetime.now().date()
train_start = st.sidebar.date_input(
    "Train Start", value=today - timedelta(days=14), key="ds"
)
train_end = st.sidebar.date_input("Train End", value=today, key="de")

if train_start and train_end and train_start <= train_end:
    date_range = [
        train_start + timedelta(days=i)
        for i in range((train_end - train_start).days + 1)
    ]
    excluded_dates = st.sidebar.multiselect(
        "Exclude Dates from Training",
        options=date_range,
        format_func=lambda x: x.strftime("%Y-%m-%d"),
        help="Select specific days to totally ignore during model training.",
    )
else:
    excluded_dates = []

fc_date = st.sidebar.date_input(
    "Forecast Date", value=today + timedelta(days=1), key="dfc"
)

if st.sidebar.button("Run Analysis", key="r_btn"):
    if not all([db_user, db_pass, gen_id, target_iso, train_start, train_end, fc_date]):
        st.error("❌ Missing required inputs or ISO metadata context.")
        st.stop()

    try:
        engine = get_engine(db_user, db_pass)
        cfg = VENDOR_CONFIG[vendor_choice]

        base_data_table = cfg["data_table"].replace("miso_", "")
        dynamic_data_table = f"{target_iso}_{base_data_table}"

        with st.spinner(f"Piping data from {target_iso.upper()} and training model..."):
            if gen_id == "RDT_DA_PRICES":
                constraints_str = ", ".join(map(str, MISO_CUSTOM_CONSTRAINTS))
                actuals_query = f"""
                SELECT dt, hr, SUM(shadow_price) AS avg_output_mw
                FROM tioscore_production.miso_da_constraint_shadow_prices
                WHERE official_constraint_id IN ({constraints_str})
                AND dt BETWEEN '{train_start}' AND '{train_end}'
                GROUP BY dt, hr
                """
            else:
                actuals_query = f"""
                SELECT dt, hr, {cfg['data_mw']} AS avg_output_mw
                FROM {dynamic_data_table}
                WHERE {cfg['data_id']} = '{gen_id}'
                AND dt BETWEEN '{train_start}' AND '{train_end}'
                """
            
            df_actuals = pd.read_sql(actuals_query, engine)
            if df_actuals.empty:
                st.error(f"No data found for ID {gen_id} in {dynamic_data_table}.")
                st.stop()

            df_actuals["dt"] = pd.to_datetime(df_actuals["dt"])
            df_actuals["hr"] = df_actuals["hr"].astype(int)

            df_actuals["time"] = df_actuals["dt"] + df_actuals["hr"].apply(lambda x: timedelta(hours=x - 1))
            last_actual_time = df_actuals["time"].max()

            all_dates = pd.date_range(start=train_start, end=train_end)
            all_hours = list(range(1, 25))
            time_grid = pd.MultiIndex.from_product([all_dates, all_hours], names=['dt', 'hr']).to_frame(index=False)
            time_grid["time"] = time_grid["dt"] + time_grid["hr"].apply(lambda x: timedelta(hours=x - 1))

            time_grid = time_grid[time_grid["time"] <= last_actual_time]

            if gen_id == "RDT_DA_PRICES":
                df_actuals = time_grid.merge(df_actuals.drop(columns=["time"]), on=["dt", "hr"], how="left")
                df_actuals["avg_output_mw"] = df_actuals["avg_output_mw"].fillna(0.0)
            else:
                df_actuals = time_grid.merge(df_actuals.drop(columns=["time"]), on=["dt", "hr"], how="inner")

            if df_actuals.empty:
                st.error("❌ No valid actuals remaining after applying time boundary checks.")
                st.stop()

            train_features = pull_meteo_features(
                engine,
                train_start,
                train_end,
                selected_wind,
                selected_load,
                selected_solar,
                selected_outage,
                maxar_cities=selected_maxar,
                custom_vars_text=custom_vars_input,
            )

            if train_features.empty:
                st.error("No weather data found for the selected features and dates.")
                st.stop()

            train_features["dt"] = pd.to_datetime(train_features["dt"])

            if excluded_dates:
                excluded_dt_series = pd.to_datetime(excluded_dates)
                df_actuals = df_actuals[~df_actuals["dt"].isin(excluded_dt_series)]
                train_features = train_features[
                    ~train_features["dt"].isin(excluded_dt_series)
                ]
                if df_actuals.empty or train_features.empty:
                    st.error("❌ You have excluded all available training data.")
                    st.stop()

            plant_data_full = df_actuals.merge(
                train_features, on=["dt", "hr"], how="inner"
            )
            plant_data_full = apply_time_features(plant_data_full, selected_time_vars)
            plant_data_full = plant_data_full.sort_values(["dt", "hr"]).reset_index(
                drop=True
            )

            if plant_data_full.empty:
                st.error(
                    "No overlapping data. Check if your weather variables have data for this period."
                )
                st.stop()

            cols_to_drop = ["dt", "hr", "avg_output_mw", "time"]

            X_train = plant_data_full.drop(
                columns=[c for c in cols_to_drop if c in plant_data_full.columns]
            )
            y_train = plant_data_full["avg_output_mw"]

            features_list = [c for c in X_train.columns if c not in cols_to_drop]
            X_train_clean = X_train[features_list].copy()

            total_hours = len(plant_data_full)

            if total_hours >= 48:
                X_tr_backtest = X_train_clean.iloc[:-24]
                y_tr_backtest = y_train.iloc[:-24]
                X_va_backtest = X_train_clean.iloc[-24:]
                y_va_backtest = y_train.iloc[-24:]

                if model_choice == "Random Forest":
                    base_model = RandomForestRegressor(random_state=42, n_jobs=4)
                    param_dist = {
                        "n_estimators": [100, 200],
                        "max_depth": [3, 5, 7],
                        "min_samples_leaf": [5, 10, 20]
                    }
                else:
                    base_model = XGBRegressor(random_state=42, n_jobs=4, learning_rate=0.05)
                    param_dist = {
                        "n_estimators": [50, 100, 150],
                        "max_depth": [2, 3, 5],
                        "subsample": [0.7, 0.8, 0.9],
                        "colsample_bytree": [0.7, 0.8, 0.9]
                    }

                if tune_model:
                    tscv = TimeSeriesSplit(n_splits=3, gap=24)
                    search = RandomizedSearchCV(
                        estimator=base_model,
                        param_distributions=param_dist,
                        n_iter=6,
                        cv=tscv,
                        scoring="neg_mean_absolute_error",
                        random_state=42,
                        n_jobs=4
                    )
                    search.fit(X_tr_backtest, y_tr_backtest)
                    test_model = search.best_estimator_
                    st.info(f"Best Hyperparameters: {search.best_params_}")
                else:
                    if model_choice == "Random Forest":
                        test_model = RandomForestRegressor(n_estimators=200, max_depth=4, min_samples_leaf=10, random_state=42, n_jobs=4)
                    else:
                        test_model = XGBRegressor(n_estimators=100, max_depth=3, learning_rate=0.05, subsample=0.8, colsample_bytree=0.8, random_state=42, n_jobs=4)

                test_model.fit(X_tr_backtest, y_tr_backtest)
                backtest_preds = test_model.predict(X_va_backtest)

                cv_r2 = r2_score(y_va_backtest, backtest_preds)
                mae = mean_absolute_error(y_va_backtest, backtest_preds)
            else:
                cv_r2 = np.nan
                mae = np.nan

            imputer = SimpleImputer(strategy="median")
            X_train_imputed = pd.DataFrame(
                imputer.fit_transform(X_train_clean),
                columns=features_list,
                index=X_train_clean.index
            )

            if tune_model and total_hours >= 48:
                model = base_model.set_params(**test_model.get_params())
            else:
                if model_choice == "Random Forest":
                    model = RandomForestRegressor(n_estimators=200, max_depth=4, min_samples_leaf=10, random_state=42, n_jobs=4)
                else:
                    model = XGBRegressor(n_estimators=100, max_depth=3, learning_rate=0.05, subsample=0.8, colsample_bytree=0.8, random_state=42, n_jobs=4)

            model.fit(X_train_imputed, y_train)
            preds_in = model.predict(X_train_imputed)

            insample_r2 = r2_score(y_train, preds_in)

            unique_days = sorted(plant_data_full["dt"].unique())
            wf_preds = np.full(len(plant_data_full), np.nan)

            if len(unique_days) > 1:
                for i in range(1, len(unique_days)):
                    train_days = unique_days[:i]
                    val_day = unique_days[i]
                    
                    train_idx = plant_data_full["dt"].isin(train_days)
                    val_idx = plant_data_full["dt"] == val_day
                    
                    X_tr_wf_raw = X_train_clean[train_idx]
                    y_tr_wf = y_train[train_idx]
                    X_va_wf_raw = X_train_clean[val_idx]
                    
                    if not X_tr_wf_raw.empty and not X_va_wf_raw.empty:
                        fold_imputer = SimpleImputer(strategy="median")
                        X_tr_wf = pd.DataFrame(
                            fold_imputer.fit_transform(X_tr_wf_raw),
                            columns=features_list
                        )
                        X_va_wf = pd.DataFrame(
                            fold_imputer.transform(X_va_wf_raw),
                            columns=features_list
                        )

                        fold_hours = len(X_tr_wf)
                        
                        use_tuning = False
                        if tune_model and fold_hours >= 96:
                            try:
                                gap_val = 24 if fold_hours >= 120 else 12
                                cv_splits = min(3, max(2, (fold_hours - gap_val) // 36))
                                fold_tscv = TimeSeriesSplit(n_splits=cv_splits, gap=gap_val)
                                
                                if model_choice == "Random Forest":
                                    fold_base = RandomForestRegressor(random_state=42, n_jobs=4)
                                else:
                                    fold_base = XGBRegressor(random_state=42, n_jobs=4, learning_rate=0.05)
                                    
                                fold_search = RandomizedSearchCV(
                                    estimator=fold_base,
                                    param_distributions=param_dist,
                                    n_iter=4,
                                    cv=fold_tscv,
                                    scoring="neg_mean_absolute_error",
                                    random_state=42,
                                    n_jobs=4
                                )
                                fold_search.fit(X_tr_wf, y_tr_wf)
                                wf_model = fold_search.best_estimator_
                                use_tuning = True
                            except ValueError:
                                use_tuning = False

                        if not use_tuning:
                            if model_choice == "Random Forest":
                                max_depth_val = 3 if fold_hours < 96 else 4
                                min_samples_leaf_val = 2 if fold_hours < 96 else 10
                                wf_model = RandomForestRegressor(
                                    n_estimators=100,
                                    max_depth=max_depth_val,
                                    min_samples_leaf=min_samples_leaf_val,
                                    random_state=42,
                                    n_jobs=4
                                )
                            else:
                                max_depth_val = 2 if fold_hours < 96 else 3
                                wf_model = XGBRegressor(
                                    n_estimators=50 if fold_hours < 96 else 100,
                                    max_depth=max_depth_val,
                                    learning_rate=0.05,
                                    subsample=0.8,
                                    colsample_bytree=0.8,
                                    random_state=42,
                                    n_jobs=4
                                )
                            wf_model.fit(X_tr_wf, y_tr_wf)

                        wf_preds[val_idx] = wf_model.predict(X_va_wf)

            plant_data_full["wf_preds"] = wf_preds

            insample_r2 = r2_score(y_train, preds_in)

            gap_start = last_actual_time.date()
            gap_start_buffered = gap_start - timedelta(days=1)

            fc_features_buffered = pull_meteo_features(
                engine,
                gap_start_buffered,
                fc_date,
                selected_wind,
                selected_load,
                selected_solar,
                selected_outage,
                maxar_cities=selected_maxar,
                custom_vars_text=custom_vars_input,
            )

            if not fc_features_buffered.empty:
                fc_features_buffered = apply_time_features(
                    fc_features_buffered, selected_time_vars
                )
                fc_features_buffered["time"] = fc_features_buffered[
                    "dt"
                ] + fc_features_buffered["hr"].apply(lambda x: timedelta(hours=x - 1))

                fc_features = fc_features_buffered[
                    fc_features_buffered["dt"] >= pd.to_datetime(gap_start)
                ].copy()

                cols_to_drop_fc = ["dt", "hr", "time", "mw"]
                X_fc = fc_features.drop(
                    columns=[c for c in cols_to_drop_fc if c in fc_features.columns]
                )
                from sklearn.impute import SimpleImputer

                X_fc = X_fc.reindex(columns=features_list, fill_value=np.nan)
                X_fc = pd.DataFrame(
                    imputer.transform(X_fc), 
                    columns=features_list
                )

                fc_features["mw"] = model.predict(X_fc)
                target_ts = pd.to_datetime(fc_date)
                last_actual_ts = pd.to_datetime(last_actual_time)

                bridge_df = fc_features[
                    (pd.to_datetime(fc_features["time"]) > last_actual_ts)
                    & (pd.to_datetime(fc_features["dt"]) < target_ts)
                ].copy()
                forecast_df = fc_features[fc_features["dt"] == target_ts].copy()
                fc_list = [str(int(round(x))) for x in forecast_df["mw"]]
            else:
                fc_list, bridge_df, forecast_df = [], pd.DataFrame(), pd.DataFrame()

            # Calculate Walk-Forward Out-of-Sample R² across all historical folds
            valid_wf_mask = ~np.isnan(plant_data_full["wf_preds"])
            if valid_wf_mask.any():
                wf_oos_r2 = r2_score(
                    plant_data_full.loc[valid_wf_mask, "avg_output_mw"],
                    plant_data_full.loc[valid_wf_mask, "wf_preds"]
                )
            else:
                wf_oos_r2 = np.nan

            # Display metrics across 3 columns
            m1, m2, m3 = st.columns(3)
            m1.metric("In-Sample R²", f"{insample_r2:.3f}")
            m2.metric("Walk-Forward OOS R²", "N/A" if np.isnan(wf_oos_r2) else f"{wf_oos_r2:.3f}")
            
            unit_label = "$" if gen_id == "RDT_DA_PRICES" else "MW"
            m3.metric(
                "Tracking MAE (Last 24h)", "N/A" if np.isnan(mae) else f"{mae:.2f} {unit_label}"
            )

            full_time_range = pd.date_range(
                start=pd.to_datetime(train_start),
                end=pd.to_datetime(train_end) + timedelta(hours=23),
                freq="h",
            )
            plot_df = pd.DataFrame({"time": full_time_range})
            plant_data_full["preds"] = preds_in
            plot_df = plot_df.merge(
                plant_data_full[["time", "avg_output_mw", "preds", "wf_preds"]],
                on="time",
                how="left",
            )

            fig = go.Figure()

            actual_name = "Actual Combined Shadow Price" if gen_id == "RDT_DA_PRICES" else "Actual Generation"
            fc_name = "Forecast Combined Shadow Price" if gen_id == "RDT_DA_PRICES" else "Forecast Generation"

            fig.add_trace(
                go.Scatter(
                    x=plot_df["time"],
                    y=plot_df["avg_output_mw"],
                    name=actual_name,
                    line=dict(color="blue"),
                    connectgaps=False,
                )
            )

            fig.add_trace(
                go.Scatter(
                    x=plot_df["time"],
                    y=plot_df["wf_preds"],
                    name="Historical Walk-Forward (OOS)",
                    line=dict(color="#F2FF00"),
                    connectgaps=False,
                )
            )

            if not forecast_df.empty:
                plot_fc = forecast_df.sort_values("time")
                fig.add_trace(
                    go.Scatter(
                        x=plot_fc["time"],
                        y=plot_fc["mw"],
                        name=fc_name,
                        line=dict(color="#00FF00"),
                    )
                )

            if features_list:
                custom_dict = parse_custom_functions(custom_vars_input)

                hidden_components = set()
                for expression in custom_dict.values():
                    found_tokens = re.findall(r"[a-zA-Z0-9_:\-]+", expression)
                    for tok in found_tokens:
                        if tok == "-":
                            continue
                        col_target, _, _, _ = normalize_var_to_column(tok)
                        if col_target:
                            hidden_components.add(col_target)

                explicit_columns = set()
                for prefix, selections in [
                    ("wind", selected_wind),
                    ("load", selected_load),
                    ("solar", selected_solar),
                    ("outage", selected_outage),
                ]:
                    for item in selections:
                        if ":" in item:
                            iso, reg = item.split(":", 1)
                            reg_clean = re.sub(r"\s+", " ", reg.strip())
                            reg_clean = re.sub(r"[^a-zA-Z0-9_]", "_", reg_clean)
                            explicit_columns.add(
                                f"{prefix}_{iso.strip().lower()}_{reg_clean.lower()}"
                            )

                feat_train = plant_data_full[["time"] + features_list].copy()
                if not fc_features.empty:
                    feat_fc = fc_features[["time"] + features_list].copy()
                    feat_comb = pd.concat(
                        [feat_train, feat_fc], ignore_index=True
                    ).sort_values("time")
                else:
                    feat_comb = feat_train.sort_values("time")

                ignored_time_features = {
                    "dow", "dow_sin", "dow_cos", "is_weekend",
                    "time_hour_sin", "time_hour_cos", "time_is_on_peak"
                }

                for feat in features_list:
                    if feat.startswith("time_") or feat.startswith("dow_") or feat in ignored_time_features:
                        continue
                    
                    is_maxar_feat = feat.startswith("maxar_") or "feelslike" in feat
                    
                    if not is_maxar_feat and (feat in hidden_components and feat not in explicit_columns):
                        continue

                    fig.add_trace(
                        go.Scatter(
                            x=feat_comb["time"],
                            y=feat_comb[feat],
                            name=feat,
                            line=dict(width=1.5, dash="dashdot"),
                            yaxis="y2",
                        )
                    )

            fig.update_layout(
                height=600,
                hovermode="x unified",
                legend=dict(orientation="h", y=1.1),
                yaxis=dict(title="Shadow Price ($)" if gen_id == "RDT_DA_PRICES" else "Generation Output (MW)"),
                yaxis2=dict(
                    title="Feature Values / Custom Indices",
                    overlaying="y",
                    side="right",
                    showgrid=False,
                ),
            )

            st.plotly_chart(fig, use_container_width=True)

            if fc_list:
                st.subheader(f"Hourly Forecast ({fc_date})")
                st.code(", ".join(fc_list))

    except Exception as e:
        st.error(f"Critical Error: {e}")