from datetime import date, datetime, timedelta
import pandas as pd
import plotly.express as px
import streamlit as st

from lib import db

# ------------------------------------------------------------------------------
# Page Configuration
# ------------------------------------------------------------------------------
st.set_page_config(
    page_title="AutoFlow Congestion Divergence",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.title("AutoFlow Congestion Divergence")
st.markdown(
    """
    Identifies constraints where the **target evaluation forecast AutoFlow** is highest relative to its 
    **recent historical baseline** using **today's current coefficients**.
    """
)

if not db.gate():
    st.stop()

# ------------------------------------------------------------------------------
# Sidebar Controls
# ------------------------------------------------------------------------------
st.sidebar.header("Configuration")

selected_iso = st.sidebar.selectbox(
    "Select ISO",
    options=["miso", "spp", "ercot", "pjm"],
    index=0,
    format_func=lambda x: x.upper()
)

lookback_days = st.sidebar.slider(
    "Historical Baseline Period (Days)",
    min_value=3,
    max_value=30,
    value=14
)

target_eval_date = st.sidebar.date_input(
    "Evaluation Forecast Date", 
    date.today() + timedelta(days=1)
)

ranking_metric = st.sidebar.radio(
    "Rank Constraints By:",
    options=["Peak Hourly Divergence (Max Eval vs Historical Avg)", "Daily Avg Divergence"],
    index=0
)

top_n = st.sidebar.number_input("Top N Constraints", min_value=5, max_value=50, value=10)

run_button = st.sidebar.button("Run Divergence Analysis", type="primary")

# ------------------------------------------------------------------------------
# Data Fetching Functions
# ------------------------------------------------------------------------------
@st.cache_data(ttl=600)
def fetch_current_coefficients(iso: str):
    """
    Fetches the latest active coefficient set as of today for all active constraints.
    Strictly filters for vendor='meteologica' (excluding PRT).
    """
    query = f"""
    WITH latest_times AS (
        SELECT official_constraint_id, MAX(computed_at) AS max_computed
        FROM tioscore_production.{iso}_zonal_coefficients
        WHERE vendor = 'meteologica'
        GROUP BY official_constraint_id
    )
    SELECT 
        z.official_constraint_id,
        LOWER(TRIM(z.zone)) AS zone,
        LOWER(TRIM(z.coefficient_type)) AS coefficient_type,
        z.coefficient,
        z.computed_at
    FROM tioscore_production.{iso}_zonal_coefficients z
    INNER JOIN latest_times lt 
      ON z.official_constraint_id = lt.official_constraint_id 
     AND z.computed_at = lt.max_computed
    WHERE z.vendor = 'meteologica'
    """
    return pd.read_sql(query, db.engine())


@st.cache_data(ttl=600)
def fetch_variable_timeseries(iso: str, start_dt: date, end_dt: date):
    """
    Fetches Meteologica load, wind, and solar timeseries across the full range (baseline + forecast).
    Strictly excludes PRT tables/vendors.
    """
    dfs = []
    
    # Load Forecasts
    load_q = f"""
    SELECT dt, hr, LOWER(region) AS zone, 'load' AS coefficient_type, power_mw AS forecast_mw
    FROM tioscore_production.{iso}_meteologica_load_forecasts
    WHERE dt BETWEEN '{start_dt}' AND '{end_dt}'
    """
    try:
        df_l = pd.read_sql(load_q, db.engine())
        dfs.append(df_l)
    except Exception:
        pass

    # Wind Forecasts
    wind_q = f"""
    SELECT dt, hr, LOWER(region) AS zone, 'wind' AS coefficient_type, power_mw AS forecast_mw
    FROM tioscore_production.{iso}_meteologica_wind_forecasts
    WHERE dt BETWEEN '{start_dt}' AND '{end_dt}'
    """
    try:
        df_w = pd.read_sql(wind_q, db.engine())
        dfs.append(df_w)
    except Exception:
        pass

    # Solar Forecasts
    solar_q = f"""
    SELECT dt, hr, LOWER(region) AS zone, 'solar' AS coefficient_type, power_mw AS forecast_mw
    FROM tioscore_production.{iso}_meteologica_solar_forecasts
    WHERE dt BETWEEN '{start_dt}' AND '{end_dt}'
    """
    try:
        df_s = pd.read_sql(solar_q, db.engine())
        dfs.append(df_s)
    except Exception:
        pass

    if not dfs:
        return pd.DataFrame()

    combined = pd.concat(dfs, ignore_index=True)
    combined["zone"] = combined["zone"].str.strip().str.lower()
    combined["dt"] = pd.to_datetime(combined["dt"]).dt.date
    return combined


def calculate_autoflow_timeseries(coeff_df: pd.DataFrame, var_df: pd.DataFrame) -> pd.DataFrame:
    """
    Multiplies today's current coefficients by the variable timeseries across all dates/hours:
    AutoFlow(t, h) = SUM(Current_Coefficient * Variable(t, h))
    """
    if coeff_df.empty or var_df.empty:
        return pd.DataFrame()

    c_df = coeff_df.groupby(
        ["official_constraint_id", "zone", "coefficient_type"], as_index=False
    )["coefficient"].mean()

    merged = pd.merge(
        c_df,
        var_df,
        on=["zone", "coefficient_type"],
        how="inner"
    )

    merged["term_val"] = merged["coefficient"] * merged["forecast_mw"]

    autoflow_hourly = merged.groupby(
        ["official_constraint_id", "dt", "hr"], as_index=False
    )["term_val"].sum().rename(columns={"term_val": "autoflow_mw"})

    return autoflow_hourly

# ------------------------------------------------------------------------------
# Main Execution Flow
# ------------------------------------------------------------------------------
if run_button or "autoflow_ran" in st.session_state:
    st.session_state["autoflow_ran"] = True

    baseline_start_dt = target_eval_date - timedelta(days=lookback_days)
    baseline_end_dt = target_eval_date - timedelta(days=1)

    with st.spinner("Fetching today's AutoFlow coefficients & historical/forecast variables..."):
        coeffs = fetch_current_coefficients(selected_iso)
        variables = fetch_variable_timeseries(selected_iso, baseline_start_dt, target_eval_date)

    if coeffs.empty:
        st.warning(f"No active AutoFlow coefficients found for {selected_iso.upper()} today.")
        st.stop()

    if variables.empty:
        st.warning(f"No Meteologica variable data found between {baseline_start_dt} and {target_eval_date}.")
        st.stop()

    with st.spinner("Computing AutoFlow timeseries and evaluating divergence..."):
        af_ts = calculate_autoflow_timeseries(coeffs, variables)

    if af_ts.empty:
        st.error("Unable to calculate AutoFlow timeseries. Verify zone mappings.")
        st.stop()

    # Split into Historical Baseline vs Evaluation Date
    baseline_af = af_ts[af_ts["dt"] < target_eval_date]
    eval_af = af_ts[af_ts["dt"] == target_eval_date]

    if eval_af.empty:
        st.warning(f"No variable data found for evaluation date {target_eval_date}.")
        st.stop()

    # Aggregate Historical Baseline per constraint
    hist_summary = baseline_af.groupby("official_constraint_id").agg(
        hist_avg_autoflow=("autoflow_mw", "mean"),
        hist_peak_autoflow=("autoflow_mw", "max")
    ).reset_index()

    # Aggregate Evaluation Date per constraint
    eval_summary = eval_af.groupby("official_constraint_id").agg(
        eval_avg_autoflow=("autoflow_mw", "mean"),
        eval_peak_autoflow=("autoflow_mw", "max")
    ).reset_index()

    # Combine Baseline and Evaluation Date
    summary = pd.merge(eval_summary, hist_summary, on="official_constraint_id", how="inner")

    summary["avg_delta_mw"] = summary["eval_avg_autoflow"] - summary["hist_avg_autoflow"]
    summary["peak_delta_mw"] = summary["eval_peak_autoflow"] - summary["hist_avg_autoflow"]

    sort_col = "peak_delta_mw" if "Peak" in ranking_metric else "avg_delta_mw"

    # Retain positive stress increase filter
    bullish_summary = summary[summary[sort_col] > 0].sort_values(
        by=sort_col, ascending=False
    ).head(top_n)

    # --------------------------------------------------------------------------
    # Results Display
    # --------------------------------------------------------------------------
    st.subheader(f"Top {len(bullish_summary)} Constraints with Strong Congestion Shifts")
    st.caption(f"Comparing evaluation forecast date ({target_eval_date}) AutoFlow against {lookback_days}-day historical baseline using today's current coefficients.")

    if bullish_summary.empty:
        st.info("No positive divergence constraints found for the selected timeframe.")
    else:
        formatted_summary = bullish_summary.copy()
        
        # Build ECore link column
        formatted_summary["ecore_url"] = formatted_summary["official_constraint_id"].apply(
            lambda cid: f"https://energycore.tioscapital.com/{selected_iso.lower()}/constraints/{cid}"
        )
        
        formatted_summary = formatted_summary[[
            "ecore_url", "official_constraint_id", "eval_peak_autoflow", "eval_avg_autoflow", 
            "hist_avg_autoflow", "peak_delta_mw", "avg_delta_mw"
        ]]
        
        formatted_summary.columns = [
            "ECore Link", "Constraint ID", f"Eval Date Peak AutoFlow (MW)", f"Eval Date Avg AutoFlow (MW)",
            f"Past {lookback_days}-Day Avg AutoFlow (MW)", "Peak Divergence Shift (MW)", "Avg Divergence Shift (MW)"
        ]

        st.dataframe(
            formatted_summary,
            column_config={
                "ECore Link": st.column_config.LinkColumn(
                    "Constraint Link",
                    display_text=r"https://energycore\.tioscapital\.com/[^/]+/constraints/(.*)"
                ),
                "Constraint ID": st.column_config.NumberColumn("Constraint ID", format="%d"),
                f"Eval Date Peak AutoFlow (MW)": st.column_config.NumberColumn(format="%.2f"),
                f"Eval Date Avg AutoFlow (MW)": st.column_config.NumberColumn(format="%.2f"),
                f"Past {lookback_days}-Day Avg AutoFlow (MW)": st.column_config.NumberColumn(format="%.2f"),
                "Peak Divergence Shift (MW)": st.column_config.NumberColumn(format="%.2f"),
                "Avg Divergence Shift (MW)": st.column_config.NumberColumn(format="%.2f"),
            },
            hide_index=True,
            use_container_width=True
        )

        # ----------------------------------------------------------------------
        # Expandable Timeseries Charts per Constraint
        # ----------------------------------------------------------------------
        st.markdown("---")
        st.subheader("Constraint AutoFlow Timeseries Drill-Down")

        eval_start_ts = pd.to_datetime(f"{target_eval_date} 00:00:00")

        for _, row in bullish_summary.iterrows():
            cid = int(row["official_constraint_id"])
            peak_shift = row["peak_delta_mw"]
            
            with st.expander(f"Constraint {cid} — Peak Shift: +{peak_shift:.2f} MW"):
                chart_data = af_ts[af_ts["official_constraint_id"] == cid].copy()
                chart_data["timestamp"] = pd.to_datetime(
                    chart_data["dt"].astype(str) + " " + (chart_data["hr"] - 1).astype(str).str.zfill(2) + ":00:00"
                )
                chart_data = chart_data.sort_values("timestamp")

                fig = px.line(
                    chart_data,
                    x="timestamp",
                    y="autoflow_mw",
                    labels={"autoflow_mw": "AutoFlow Stress (MW)", "timestamp": "Date & Hour"},
                    title=f"Constraint {cid}: AutoFlow Timeseries (Past {lookback_days} Days to Eval Date {target_eval_date})"
                )

                # Add vertical line marking where the evaluation date forecast begins
                fig.add_vline(
                    x=eval_start_ts.timestamp() * 1000,
                    line_width=2,
                    line_dash="dash",
                    line_color="red",
                    annotation_text=f"Eval Date ({target_eval_date})",
                    annotation_position="top left"
                )

                st.plotly_chart(fig, use_container_width=True)

    # --------------------------------------------------------------------------
    # Calculation Breakdown
    # --------------------------------------------------------------------------
    st.markdown("---")
    with st.expander("How This Tool Works"):
        st.markdown(
            f"""
            1. **Pulls Today's Active Coefficients:** Grabs the single newest set of active AutoFlow coefficients computed today for each constraint (strictly using Meteologica and excluding PRT).
            2. **Pulls Hourly Variable Timeseries:** Queries hourly Meteologica load, wind, and solar variables across the full range—from your selected lookback window (past {lookback_days} days) through your chosen evaluation date ({target_eval_date}).
            3. **Calculates Hourly AutoFlow:** Applies today's fixed coefficients across every hour of both the historical lookback and the evaluation date to build a continuous AutoFlow MW timeseries.
            4. **Aggregates Baseline vs. Evaluation Date:** Averages historical AutoFlow values over the lookback period to establish a baseline, then calculates both peak hourly and daily average AutoFlow for the evaluation date ({target_eval_date}).
            5. **Compares & Ranks Divergence:** Subtracts the historical average baseline from the evaluation date forecasted values, filters for constraints with positive stress increases, and ranks the top $N$ constraints with the strongest congestion shifts.
            """
        )