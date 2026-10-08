"""MCP server that gives AI clients (Claude Code, Gemini CLI, ...) the outage search.

It runs on your own machine and talks to the client over stdin/stdout, so the AI
work is billed to your client subscription, not to an API key. It uses the same
config/mysql.yml (or TIOS_DB_PASSWORD) and SSH tunnel as the Streamlit app, and only
reads from the database.

Run it from the client, not by hand -- see the "AI tools (MCP)" section of app.py.
To check that it starts:  .venv/bin/python mcp_server.py   (then Ctrl-C)
"""

import datetime
import os
import time

import pandas as pd
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from lib import autoflow
from lib import constraint_drivers as drivers
from lib import db
from lib import market_conditions
from lib import network
from lib import outage_impacts
from lib import outage_search as osearch
from lib import upcoming_outages

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
BRIEFING_PROMPT_PATH = os.path.join(_REPO_ROOT, "prompts", "outage_briefing.md")
DRIVERS_PROMPT_PATH = os.path.join(_REPO_ROOT, "prompts", "binding_drivers.md")

# Same lifetime as the page's notes cache.
NOTES_CACHE_SECONDS = 4 * 60 * 60
# Same cap as the page's main search.
NOTE_RESULT_LIMIT = 2000

ID_TYPES = {"equipment": "Equipment ID", "group": "Group ID", "text": "Manual string"}

# The binding-constraint tools look back at days with complete data by default: final RT
# prices, full 5-minute intervals, settled outage records, and pseudo actuals for every hour.
ANALYSIS_DAYS_AHEAD = -2

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                            open_world_hint=False)

server = MCPServer(
    name="energycore-outages",
    instructions=(
        "Read-only access to the trading desk's transmission outage search (tioscore_production). "
        "Use list_upcoming_outages to find outages planned to start on a day, then "
        "get_outage_documentation for the outages that have history. The outage_briefing prompt "
        "holds the desk's report instructions. To explain why constraints bound on a day, use "
        "list_binding_constraints, then get_constraint_drivers for the constraints of interest; the "
        "binding_drivers prompt holds the instructions. These analysis tools default to two days ago, the latest day "
        "with complete data. get_market_conditions gives energy prices, RTEP, and the stress index; "
        "get_autoflow gives autoflow over a date range, and "
        "get_outage_impacts checks outages (including planned ones on future days) against a constraint."
    ),
)

_engine = None
_notes_cache = {}


def engine():
    global _engine
    if _engine is None:
        _engine = db.standalone_engine()
    return _engine


def market_notes(market: str) -> pd.DataFrame:
    """All quick notes for a market, reloaded after NOTES_CACHE_SECONDS. The first load takes about a minute."""
    loaded = _notes_cache.get(market)
    if loaded is None or time.time() - loaded[0] > NOTES_CACHE_SECONDS:
        loaded = (time.time(), osearch.load_market_notes(engine(), market))
        _notes_cache[market] = loaded
    return loaded[1]


def _check_choice(name: str, value: str, choices) -> None:
    if value not in choices:
        raise ValueError(f"Unknown {name} '{value}'. Use one of: {', '.join(choices)}")


def _resolve_date(date: str | None, days_ahead: int) -> datetime.date:
    if date:
        return datetime.date.fromisoformat(date)
    return datetime.date.today() + datetime.timedelta(days=days_ahead)


def _warnings_section(warnings: list[str]) -> list[str]:
    if not warnings:
        return []
    return ["", "=== WARNINGS (some data could not be loaded; results may be incomplete) ==="] + list(dict.fromkeys(warnings))


def _fmt_dt(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value is not None and not pd.isna(value) else ""


@server.tool(annotations=READ_ONLY)
def list_upcoming_outages(market: str, days_ahead: int = 2, date: str | None = None,
                          include_ignored: bool = False, check_history: bool = True) -> str:
    """List transmission outages planned to start on one day, one row per equipment ID.

    Args:
        market: miso, spp, pjm, ercot, or caiso.
        days_ahead: Days from today (2 = T+2). Ignored when date is given.
        date: A specific day, YYYY-MM-DD. Compared with planned_start in market local time.
        include_ignored: Also list outages that EnergyCore's regression ignore rules drop
            (for example cancelled or denied requests, and CAISO hot-line work).
        check_history: Count note matches, annotations, and flags for each outage. The first
            call for a market loads its notes, which takes about a minute.

    Returns tab-separated rows. Outages with note_matches, annotations, and flag all empty
    have no history, so get_outage_documentation would return nothing useful for them.
    """
    _check_choice("market", market, osearch.MARKETS)
    target_date = _resolve_date(date, days_ahead)
    warnings = []

    outages = upcoming_outages.list_upcoming_outages(engine(), market, target_date,
                                                     apply_ignore_rules=not include_ignored,
                                                     warn=warnings.append)
    upcoming_outages.add_group_ids(engine(), market, outages, warn=warnings.append)

    lookback = "3 Years"
    if check_history:
        start_date = osearch.lookback_start_date(lookback, pd.Timestamp.now())
        upcoming_outages.add_history(engine(), market, outages, market_notes(market),
                                     start_date.strftime("%Y-%m-%d"), warn=warnings.append)

    lines = [
        f"Market: {market.upper()}",
        f"Planned start date: {target_date.isoformat()}",
        "Filter: current report (most_recent = 1); "
        + ("ignore rules NOT applied" if include_ignored else "EnergyCore regression_ignore_rules applied"),
        f"Outages: {len(outages)}",
    ]
    if check_history:
        with_history = [o for o in outages if o["note_matches"] or o["annotations"] or o["flag_type"]]
        lines.append(f"With history (note lookback {lookback}): {len(with_history)}")

    header = ["equipment_id", "name", "equipment_type", "kv", "planned_start", "planned_end", "request_status",
              "request_ids", "group_ids"]
    if check_history:
        header += ["note_matches", "annotations", "flag"]
    lines += ["", "\t".join(header)]

    for o in outages:
        row = [o["outage_equipment_id"], o["name"], o["equipment_type"], "" if o["kv"] is None else str(o["kv"]),
               _fmt_dt(o["planned_start"]), _fmt_dt(o["planned_end"]), ", ".join(o["request_statuses"]),
               ", ".join(str(r) for r in o["request_ids"]), ", ".join(o["group_ids"])]
        if check_history:
            row += [str(o["note_matches"]), str(o["annotations"]), o["flag_type"]]
        lines.append("\t".join(row))

    return "\n".join(lines + _warnings_section(warnings))


@server.tool(annotations=READ_ONLY)
def get_outage_documentation(market: str, outage_id: str, id_type: str = "equipment",
                             notes_market: str | None = None, lookback: str = "3 Years", context: str = "All",
                             max_notes_per_element: int = 25) -> str:
    """Documentation for one outage: the same text as "Copy Full Output to Clipboard" on the
    Outage & Shadow Price Search page.

    It has the match fields, flag notes, outage annotations, quick notes that mention the
    outage (with summed shadow prices), and monitored element descriptions.

    Args:
        market: Market the outage is in: miso, spp, pjm, ercot, or caiso.
        outage_id: Equipment ID, group ID, or search text, depending on id_type.
        id_type: equipment, group, or text (comma-separated search terms).
        notes_market: Market whose notes and shadow prices to search, when different from the
            outage market (for example MISO outages in SPP or PJM notes). Default: market.
        lookback: 3 Years, 1 Month, 6 Months, 1 Year, 5 Years, or All Time.
        context: All, rt_shadow, rt_shadow_forecast, or da_shadow.
        max_notes_per_element: Most quick notes to show for each monitored element (newest
            first), to keep the output a manageable size. 0 shows all of them.
    """
    _check_choice("market", market, osearch.MARKETS)
    notes_market = notes_market or market
    _check_choice("notes_market", notes_market, osearch.MARKETS)
    _check_choice("id_type", id_type, ID_TYPES)
    _check_choice("lookback", lookback, osearch.LOOKBACK_OPTIONS)
    _check_choice("context", context, osearch.CONTEXT_OPTIONS)

    eng = engine()
    warnings = []
    search_type = ID_TYPES[id_type]

    search_fields, raw_eq_ids, _ = osearch.build_search_fields(eng, search_type, outage_id, notes_market, market,
                                                               warn=warnings.append)
    if not search_fields:
        return "\n".join(["No search fields could be built from that input, so there is nothing to search for."]
                         + _warnings_section(warnings))

    outage_names = osearch.fetch_outage_names(eng, search_type, outage_id, market, raw_eq_ids, warn=warnings.append)
    annotations = osearch.fetch_outage_annotations(eng, raw_eq_ids, market, notes_market, warn=warnings.append)
    flag_notes = osearch.dedupe_flag_notes(
        osearch.fetch_transmission_outage_flags(eng, notes_market, raw_eq_ids, warn=warnings.append))

    start_date = osearch.lookback_start_date(lookback, pd.Timestamp.now())
    start_date_str = start_date.strftime("%Y-%m-%d") if start_date is not None else None
    df, was_limited = osearch.execute_main_search_query(eng, market_notes(notes_market), notes_market,
                                                        search_fields, context, start_date_str,
                                                        result_limit=NOTE_RESULT_LIMIT)

    me_groups = osearch.group_by_monitored_element(eng, df, annotations, notes_market)
    meta_descriptions = {}
    if me_groups:
        meta_descriptions = osearch.fetch_monelem_meta_descriptions(
            eng, notes_market, [g['me_id'] for g in me_groups if g['me_id']])

    df_shown = df
    if max_notes_per_element and not df.empty:
        df_shown = df.groupby('official_monitored_element_id', sort=False).head(max_notes_per_element)

    lines = [osearch.clipboard_text(notes_market, search_fields, outage_names, flag_notes, me_groups, df_shown,
                                    meta_descriptions)]
    if len(df_shown) < len(df):
        lines.append(f"(Showing the newest {max_notes_per_element} notes per monitored element: "
                     f"{len(df) - len(df_shown)} of {len(df)} matching notes omitted.)")
    if was_limited:
        lines.append(f"(More than {NOTE_RESULT_LIMIT} notes matched; only the newest {NOTE_RESULT_LIMIT} were used.)")

    return "\n".join(lines + _warnings_section(warnings))


def _tsv(df: pd.DataFrame, columns: list[str], decimals: int = 0) -> list[str]:
    """Tab-separated header and rows; floats rounded, NaN shown as empty."""
    def cell(v):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return ""
        if isinstance(v, float):
            return f"{v:.{decimals}f}"
        return " ".join(str(v).split())
    rows = df[columns].astype(object).itertuples(index=False, name=None)
    return ["\t".join(columns)] + ["\t".join(cell(v) for v in row) for row in rows]


@server.tool(annotations=READ_ONLY)
def list_binding_constraints(market: str, date: str | None = None, kind: str = "rt", limit: int = 15) -> str:
    """Constraints that bound on one day, largest absolute daily shadow price total first,
    with each monitored element's binding history for comparison.

    Args:
        market: miso, spp, pjm, ercot, or caiso.
        date: The day, YYYY-MM-DD. Default: two days ago, the latest day with complete data.
        kind: rt (real-time) or da (day-ahead). Today's RT is usually preliminary and only
            runs through the latest published hour.
        limit: Most constraints to list.

    Returns tab-separated rows. Pass constraint_id to get_constraint_drivers. RT and DA use
    different constraint and monitored element IDs for the same line.
    """
    _check_choice("market", market, osearch.MARKETS)
    _check_choice("kind", kind, drivers.KINDS)
    day = _resolve_date(date, ANALYSIS_DAYS_AHEAD)
    eng = engine()

    df = drivers.binding_constraints(eng, market, day, kind, limit)
    if df.empty:
        return f"No {kind.upper()} shadow prices for {market.upper()} on {day}."
    hist = drivers.binding_history(eng, market, df["me_id"].tolist(), day, kind)
    df = df.merge(hist, on="me_id", how="left")

    lines = [f"{market.upper()} {kind.upper()} binding constraints on {day}"
             + (" (includes preliminary prices)" if df["preliminary"].fillna(0).any() else ""),
             "History columns cover the 3 years before this day; total_30d is the 30 days before it.", ""]
    lines += _tsv(df, ["constraint_id", "me_id", "name", "total", "hours", "first_hr", "last_hr", "peak_hr", "peak",
                       "total_30d", "days_bound", "max_day", "last_bound"])
    return "\n".join(lines)


def _market_conditions_section(eng, market: str, day: datetime.date, shadow: pd.DataFrame | None,
                               warnings: list[str], forecast_names: dict[str, str] | None = None) -> list[str]:
    """Energy prices, RTEP/DAEP, and the stress index by hour, next to the shadow prices."""
    used, table = market_conditions.market_conditions(eng, market, day, names=forecast_names, warn=warnings.append)
    lines = ["", "--- Market conditions: energy prices, our forecasts (as of the day-ahead deadline), stress index ---"]
    if table.empty:
        return lines + ["No data."]
    if used:
        lines.append("Sources: " + "; ".join(f"{k} = {v['name']} ({v['source']})" for k, v in used.items()) + ".")
    if shadow is not None and not shadow.empty:
        table = table.merge(shadow, on="hr", how="left")
        binding = table["shadow_price"].fillna(0) != 0
        summary = []
        for col in ("rt_energy_price", "rtep", "rt_minus_rtep", "stress_level"):
            if col in table and binding.any() and (~binding).any():
                summary.append(f"{col} {table.loc[binding, col].mean():.1f} while binding vs "
                               f"{table.loc[~binding, col].mean():.1f} otherwise")
        if summary:
            lines.append("Averages: " + "; ".join(summary) + ".")
    extra = [c for c in (forecast_names or {}) if c not in ("rtep", "daep", "flex_daep")]
    cols = [c for c in ["hr", "da_energy_price", "rt_energy_price", "rtep", "rt_minus_rtep", "daep", "flex_daep"]
            + extra + ["stress_level"] if c in table.columns]
    cols += [c for c in table.columns if ("reserve_margin" in c or "net_load" in c) and c not in cols]
    if "shadow_price" in table.columns:
        cols.append("shadow_price")
    lines += _tsv(table, cols, decimals=1)
    return lines


@server.tool(annotations=READ_ONLY)
def get_market_conditions(market: str, date: str | None = None, forecasts: str | None = None) -> str:
    """Hourly market-wide conditions for a day: actual DA and RT system energy prices, our
    energy price forecasts (RTEP, DAEP) as they stood at the day-ahead deadline, RT minus
    RTEP, and the stress index with its reserve margin and net load inputs.

    Forecast names change as models are updated, so by default they are read from the
    trading configs in effect on the day (tios_configs), and the stress index file from the
    stress-index config in effect; the output says which names were used.

    Args:
        market: miso, spp, pjm, ercot, or caiso.
        date: The day, YYYY-MM-DD. Default: two days ago, the latest day with complete data.
        forecasts: Optional comma-separated column=forecast_name pairs to show instead, e.g.
            "rtep=tios.rtep.expsm, upper=tios.rtep.samprf.pm.20241023#upper_80".
    """
    _check_choice("market", market, osearch.MARKETS)
    day = _resolve_date(date, ANALYSIS_DAYS_AHEAD)
    warnings = []
    names = None
    if forecasts:
        names = dict(p.split("=", 1) for p in (x.strip() for x in forecasts.split(",")) if "=" in p)
        names = {k.strip(): v.strip() for k, v in names.items()}
    lines = [f"=== {market.upper()} market conditions on {day} ==="]
    lines += _market_conditions_section(engine(), market, day, None, warnings, forecast_names=names)[1:]
    return "\n".join(lines + _warnings_section(warnings))


def _autoflow_header(meta: dict) -> str:
    return (f"Coefficients from constraint {meta['constraint_id']}, computed {_fmt_dt(meta['computed_at'])}; "
            f"{meta['zones_used']} of {meta['zones_total']} zones have data ({meta['source']}, {meta['vendor']}). "
            "+ MW loads the constraint, - relieves it."
            + (" For hours that have not happened yet, pseudo actuals are the latest forecast."
               if meta["source"] == "pseudo_actuals" else ""))


def _autoflow_section(eng, market: str, constraint_id: int, day: datetime.date, shadow: pd.DataFrame,
                      base_hr: int | None, compare_hr: int | None, source: str, warnings: list[str]) -> list[str]:
    """The autoflow lines of get_constraint_drivers: hourly totals next to shadow prices, then
    the zones whose leverage changed most between base_hr and compare_hr."""
    lines = ["", f"--- Autoflow ({source}) ---"]
    meta, hourly, leverage = autoflow.autoflow(eng, market, constraint_id, day, source=source, warn=warnings.append)
    if meta is None or hourly.empty:
        return lines + ["No autoflow for this constraint (see warnings)."]

    lines.append(_autoflow_header(meta))
    table = hourly.drop(columns="dt").merge(shadow, on="hr", how="left")
    lines += _tsv(table, [c for c in ("hr", "autoflow", "solar", "wind", "load", "shadow_price") if c in table.columns])

    if base_hr is not None and compare_hr is not None:
        by_hr = hourly.set_index("hr")["autoflow"]
        if base_hr in by_hr.index and compare_hr in by_hr.index:
            lines.append(f"Change HE{base_hr} -> HE{compare_hr}: {by_hr[compare_hr] - by_hr[base_hr]:+.0f} MW.")
            changes = autoflow.zone_changes(leverage, day, base_hr, compare_hr).head(8)
            if not changes.empty:
                lines += [f"Zones that moved most, HE{base_hr} -> HE{compare_hr}:"]
                changes["coefficient"] = changes["coefficient"].map(lambda v: f"{v:+.4f}")
                lines += _tsv(changes, ["coefficient_type", "iso", "zone", "coefficient", "base_mw", "compare_mw",
                                        "d_leverage"], decimals=1)
    return lines


@server.tool(annotations=READ_ONLY)
def get_autoflow(market: str, constraint_id: int, start_date: str | None = None, end_date: str | None = None,
                 source: str = "pseudo_actuals", vendor: str = "meteologica", coefficient_types: str = "solar,wind,load",
                 leverage_side: str = "all", threshold_min: float | None = None, threshold_max: float | None = None,
                 top_zones: int = 15) -> str:
    """Hourly autoflow on a constraint: the flow (MW) that zonal wind, solar, and load put on it,
    computed the same way as EnergyCore's Autoflow analysis. + loads the constraint.

    Args:
        market: miso, spp, pjm, ercot, or caiso.
        constraint_id: Official constraint ID. If it has no zonal coefficients (EnergyCore only
            computes them for its autoflow_constraints list), another ID with the same name is used.
        start_date: First market day, YYYY-MM-DD. Default: two days ago.
        end_date: Last market day. Default: start_date.
        source: pseudo_actuals (latest forecast for each hour, from EnergyCore's S3 cache) or
            forecast (the day-ahead forecast in MySQL).
        vendor: meteologica or prt (PRT load for MISO zones).
        coefficient_types: Comma-separated subset of solar, wind, load.
        leverage_side: all, high, or low.
        threshold_min: Keep zones with |coefficient| at least this.
        threshold_max: Keep zones with |coefficient| at most this.
        top_zones: Zones to list by average absolute leverage over the period.
    """
    _check_choice("market", market, osearch.MARKETS)
    _check_choice("source", source, autoflow.SOURCES)
    start = _resolve_date(start_date, ANALYSIS_DAYS_AHEAD)
    end = datetime.date.fromisoformat(end_date) if end_date else start
    types = tuple(t.strip() for t in coefficient_types.split(",") if t.strip())
    warnings = []

    meta, hourly, leverage = autoflow.autoflow(engine(), market, constraint_id, start, end, source, vendor, types,
                                               leverage_side, threshold_min, threshold_max, warn=warnings.append)
    if meta is None or hourly.empty:
        return "\n".join([f"No autoflow for {market.upper()} constraint {constraint_id}."] + _warnings_section(warnings))

    lines = [f"=== Autoflow on {market.upper()} constraint {constraint_id}, {start} to {end} ===", _autoflow_header(meta), ""]
    lines += _tsv(hourly, [c for c in ("dt", "hr", "autoflow", "solar", "wind", "load") if c in hourly.columns], decimals=1)

    zones = (leverage.groupby(["coefficient_type", "iso", "zone", "coefficient"])
             .agg(avg_mw=("mw", "mean"), avg_leverage=("leverage", "mean")).reset_index())
    zones = zones.reindex(zones["avg_leverage"].abs().sort_values(ascending=False).index).head(top_zones)
    zones["coefficient"] = zones["coefficient"].map(lambda v: f"{v:+.4f}")
    lines += ["", f"--- Largest zones by average |leverage| ---"]
    lines += _tsv(zones, ["coefficient_type", "iso", "zone", "coefficient", "avg_mw", "avg_leverage"], decimals=1)
    return "\n".join(lines + _warnings_section(warnings))


def _short(df: pd.DataFrame, col: str, width: int = 70) -> pd.DataFrame:
    df = df.copy()
    df[col] = df[col].astype(str).str.slice(0, width)
    return df


def _outage_sections(eng, market: str, constraint_id: int, day: datetime.date, kind: str, shadow: pd.DataFrame,
                     me_id, top: int, warnings: list[str]) -> list[str]:
    """Transmission outage impacts, their timing against 5-minute prices, EnergyCore's outage
    matching and desk outage notes, and generator outages, for get_constraint_drivers."""
    binding = shadow.loc[shadow["shadow_price"] != 0, "hr"].astype(int)
    first_hr, last_hr = (int(binding.min()), int(binding.max())) if len(binding) else (1, 24)
    peak_hr = int(shadow.set_index("hr")["shadow_price"].abs().idxmax())
    start = datetime.datetime.combine(day, datetime.time()) + datetime.timedelta(hours=first_hr - 1)
    end = datetime.datetime.combine(day, datetime.time()) + datetime.timedelta(hours=last_hr)

    lines = ["", f"--- Transmission outage impacts (network model; outages in effect HE{first_hr}-HE{last_hr}) ---"]
    summary, impacts = outage_impacts.outage_impacts(eng, market, constraint_id, start, end, day, peak_hr,
                                                     warn=warnings.append)
    if summary.get("version") is None or "base_post_mw" not in summary:
        lines.append("Not available (see warnings).")
    else:
        limit = summary.get("limit") or {}
        lim = limit.get("limit_mw")
        pct = (lambda mw: f" ({100 * mw / lim:.0f}% of limit)") if lim else (lambda mw: "")
        lines += [
            f"Case {summary['version']}: monitored {summary.get('monitored_branch')}; contingency "
            f"{', '.join(summary.get('contingency_branches') or []) or 'none mapped (pre-contingency flows)'}.",
            f"Limit: {lim:.0f} MW from {limit.get('source')}." if lim else "Limit: unknown.",
            f"Flow in the binding direction (from {summary.get('direction_source')}): base case "
            f"{summary['base_pre_mw']:.0f} MW, after the contingency {summary['base_post_mw']:.0f} MW"
            f"{pct(summary['base_post_mw'])}, plus all significant outages {summary['post_all_mw']:.0f} MW"
            f"{pct(summary['post_all_mw'])}. (The case is a fixed snapshot, not today's dispatch.)",
            f"{summary.get('outages_in_effect', 0)} outages in effect, {summary.get('outages_mapped', 0)} mapped to the "
            f"model, {summary.get('outages_screened_in', 0)} with a possible impact of at least "
            f"{outage_impacts.MIN_SCREEN_MW:.0f} MW. individual = this outage alone; in_combination = what it "
            "adds on top of the others. + loads the constraint.",
        ]
        if not impacts.empty:
            shown = _short(impacts.head(top), "name")
            shown["in_active"] = shown["in_active"].map(lambda v: "yes" if v else "")
            shown["check"] = shown["check"].map(lambda v: "check" if v else "")
            shown["start"], shown["end"] = shown["start"].map(_fmt_dt), shown["end"].map(_fmt_dt)
            cols = ["market", "outage_equipment_id", "name", "in_active", "request_status", "start", "end",
                    "pre_ctg_lodf_pct", "individual_mw"] + [c for c in ("individual_pct",) if c in shown] + \
                   ["in_combination_mw"] + [c for c in ("in_combination_pct",) if c in shown] + ["check"]
            lines += _tsv(shown, cols, decimals=1)

    if kind == "rt":
        intervals = outage_impacts.interval_shadow_prices(eng, market, constraint_id, day)
        spells = outage_impacts.binding_spells(intervals)
        lines += ["", "--- Timing: 5-minute shadow prices and active-table outage changes ---"]
        if spells:
            lines.append("Binding spells: " + "; ".join(f"{_fmt_dt(a)[11:]}-{_fmt_dt(b)[11:]} (${tot:,.0f})"
                                                     for a, b, tot in spells))
            window_start = spells[0][0] - datetime.timedelta(hours=2)
            window_end = spells[-1][1] + datetime.timedelta(minutes=30)
            events = outage_impacts.event_lineup(intervals,
                                                 outage_impacts.active_events(eng, market, window_start, window_end))
            if not events.empty:
                ids = set(impacts.loc[impacts["market"] == market, "outage_equipment_id"]) if not impacts.empty else set()
                events["impact"] = events["outage_equipment_id"].isin(ids)
                mapped = set()
                if summary.get("version") is not None:
                    mapped = set(network.outage_branches(eng, market, summary["version"],
                                                         events["outage_equipment_id"])["outage_equipment_id"])
                # Outages the model maps but finds insignificant are left out even when their timing
                # lines up: the active table updates in 15-minute batches, so many unrelated
                # outages share each timestamp.
                # Capacitors, breakers, and disconnects do not move flow in the model.
                device = events["name"].astype(str).str.contains(outage_impacts.NON_BRANCH_DEVICE)
                high_kv = pd.to_numeric(events["kv"], errors="coerce").fillna(0) >= outage_impacts.MIN_UNMAPPED_TIMING_KV
                unmapped = ~events["outage_equipment_id"].isin(mapped) & ~device & high_kv
                keep = events[events["impact"] | (events["lines_up"] & unmapped)].head(top)
                lines.append(f"{len(events)} active-table changes from {_fmt_dt(window_start)} to {_fmt_dt(window_end)} "
                             "(the table updates every 15 minutes, so times are approximate). Showing outages with a "
                             "modeled impact, and unmapped lines/transformers of 100 kV and up whose timing lines up with a "
                             "price change.")
                if keep.empty:
                    lines.append("None of them.")
                else:
                    keep = _short(keep, "name", 50)
                    keep["time"] = keep["time"].map(_fmt_dt)
                    keep["impact"] = keep["impact"].map(lambda v: "yes" if v else "")
                    keep["lines_up"] = keep["lines_up"].map(lambda v: "yes" if v else "")
                    lines += _tsv(keep, ["time", "event", "outage_equipment_id", "name", "before", "after",
                                         "lines_up", "impact"])
        else:
            lines.append("No 5-minute shadow prices.")

    eq_ids = impacts.loc[impacts["market"] == market, "outage_equipment_id"].tolist() if not impacts.empty else []
    match_me = me_id if me_id is not None else summary.get("me_id")
    matches = outage_impacts.tioscore_matches(eng, market, match_me, day, eq_ids, warn=warnings.append)
    lines += ["", f"--- EnergyCore outage matching and desk outage notes (monitored element {match_me}) ---"]
    if not matches["detections"].empty:
        lines.append("Shadow-outage detections (best score first):")
        lines += _tsv(_short(matches["detections"].head(8), "match_strings"),
                      ["trade_dt", "overall_score", "temporal_lineup_score", "outage_combination", "avg_rt_shadow",
                       "match_strings"], decimals=2)
    if not matches["alerts"].empty:
        lines.append("Monitored element alerts (new outages matching past congested outages):")
        lines += _tsv(matches["alerts"].head(8), ["for_date", "outage_market_urn", "new_outage_equipment_id",
                                                  "new_outage_element_string", "match_type",
                                                  "past_outage_period_total_rt_shadow"])
    if not matches["annotations"].empty:
        lines.append("Outage annotations on this element (newest first):")
        lines += _tsv(_short(matches["annotations"].head(8), "notes", 150),
                      ["annotation_id", "name", "outage_equipment_id", "start_dt", "stop_dt", "notes"])
    if not matches["flags"].empty:
        lines.append("Desk flags and notes on the outages above:")
        lines += _tsv(_short(matches["flags"].head(top), "notes", 200), ["outage_equipment_id", "flag_type", "updated_at",
                                                                          "notes"])
    if all(df.empty for df in matches.values()):
        lines.append("Nothing found.")

    gen_out = drivers.generator_outages(eng, market, day, constraint_id, warn=warnings.append)
    lines += ["", "--- Generator outages and derates in effect (IIR) at plants that matter ---"]
    if gen_out.empty:
        lines.append("None found.")
    else:
        lines.append("load_mw = sf * capacity offline: the flow added if the unit would otherwise have run. "
                     "+ loads the constraint.")
        gen_out = _short(gen_out.head(top), "comments", 80)
        gen_out["sf"] = gen_out["sf"].map(lambda v: f"{v:+.3f}")
        lines += _tsv(gen_out, ["unit_name", "prim_fuel", "capacity_offline", "sf", "load_mw", "outage_type", "status",
                                "start", "end", "comments"])
    return lines


@server.tool(annotations=READ_ONLY)
def get_outage_impacts(market: str, constraint_id: int, date: str | None = None, start_hr: int = 1, end_hr: int = 24,
                       top: int = 20) -> str:
    """How much each transmission outage in effect loads one constraint, from EnergyCore's
    PowerWorld network model (LODFs), including the constraint's contingency. Works for future
    days too, using planned outages, so it can check an upcoming outage against a constraint.

    Args:
        market: miso, spp, pjm, ercot, or caiso. MISO also includes mapped SPP and PJM outages.
        constraint_id: Official constraint ID.
        date: The day, YYYY-MM-DD. Default: two days ago, the latest day with complete data.
        start_hr: First hour ending of the period to collect outages for.
        end_hr: Last hour ending of the period.
        top: Most outages to list.
    """
    _check_choice("market", market, osearch.MARKETS)
    day = _resolve_date(date, ANALYSIS_DAYS_AHEAD)
    warnings = []
    eng = engine()
    start = datetime.datetime.combine(day, datetime.time()) + datetime.timedelta(hours=start_hr - 1)
    end = datetime.datetime.combine(day, datetime.time()) + datetime.timedelta(hours=end_hr)
    summary, impacts = outage_impacts.outage_impacts(eng, market, constraint_id, start, end, day, end_hr,
                                                     warn=warnings.append)
    lines = [f"=== Outage impacts on {market.upper()} constraint {constraint_id} ({summary.get('name')}), "
             f"{day} HE{start_hr}-HE{end_hr} ==="]
    if impacts.empty:
        return "\n".join(lines + ["No outage impacts (see warnings)."] + _warnings_section(warnings))
    limit = summary.get("limit") or {}
    lines += [f"Case {summary['version']}; monitored {summary.get('monitored_branch')}; contingency "
              f"{', '.join(summary.get('contingency_branches') or []) or 'none mapped'}; limit "
              f"{limit.get('limit_mw') or 'unknown'} ({limit.get('source')}).",
              f"Binding-direction flow after contingency {summary['base_post_mw']:.0f} MW; with all significant "
              f"outages {summary['post_all_mw']:.0f} MW. + loads the constraint.", ""]
    shown = _short(impacts.head(top), "name")
    shown["start"], shown["end"] = shown["start"].map(_fmt_dt), shown["end"].map(_fmt_dt)
    cols = ["market", "outage_equipment_id", "name", "in_active", "request_status", "start", "end", "pre_ctg_lodf_pct",
            "individual_mw"] + [c for c in ("individual_pct",) if c in shown] + ["in_combination_mw"] + \
           [c for c in ("in_combination_pct",) if c in shown] + ["check"]
    lines += _tsv(shown, cols, decimals=1)
    return "\n".join(lines + _warnings_section(warnings))


@server.tool(annotations=READ_ONLY)
def get_constraint_drivers(market: str, constraint_id: int, date: str | None = None, kind: str = "rt",
                           base_hr: int | None = None, compare_hr: int | None = None, source: str = "auto",
                           autoflow_source: str = "pseudo_actuals", include_outages: bool = True,
                           note_days: int = 14, max_notes: int = 30, top_plants: int = 15,
                           top_outages: int = 12) -> str:
    """Data for explaining why one constraint bound on a day: hourly shadow prices, generator
    impacts from shift factors, zonal load, tie flows, autoflow, transmission outage impacts
    from the network model with their timing against 5-minute prices, EnergyCore's outage
    matching and the desk's outage notes, generator outages, and recent desk notes on the
    element.

    Generator impacts compare output at base_hr with compare_hr. load_mw is the flow the
    change added to the constraint: positive loads it, negative relieves it. Rows flagged
    possible_redispatch are gas/coal/oil plants that moved in the relieving direction while
    the constraint was binding; they may be responding to the constraint, not causing it.

    Args:
        market: miso, spp, pjm, ercot, or caiso.
        constraint_id: From list_binding_constraints.
        date: The day, YYYY-MM-DD. Default: two days ago, the latest day with complete data.
        kind: rt or da, matching where constraint_id came from.
        base_hr: Hour ending to measure changes from. Default: the latest hour before the
            peak with the lowest shadow price, ideally before binding started.
        compare_hr: Hour ending to measure changes to. Default: the peak hour.
        source: Plant output source: auto (default: Muse, Genscape, and Meteologica combined per
            pnode, LPI only where nothing else reports; differences between them are listed),
            or one of muse, gs, lpi, meteologica.
        autoflow_source: pseudo_actuals (default: the latest forecast for each hour, close to
            what happened), forecast (the day-ahead forecast), or none to skip autoflow.
        include_outages: Include the transmission outage, timing, matching, and generator
            outage sections (the slowest part, 10-20 s).
        note_days: Days of desk notes to include, counting back from date.
        max_notes: Most notes to include (newest first). 0 includes all of them.
        top_plants: Most plants to list in the generator impacts.
        top_outages: Most transmission and generator outages to list.
    """
    _check_choice("market", market, osearch.MARKETS)
    _check_choice("kind", kind, drivers.KINDS)
    _check_choice("source", source, drivers.GENERATOR_SOURCES)
    _check_choice("autoflow_source", autoflow_source, autoflow.SOURCES + ("none",))
    day = _resolve_date(date, ANALYSIS_DAYS_AHEAD)
    eng = engine()
    warnings = []

    hourly = drivers.constraint_hourly(eng, market, day, [constraint_id], kind)
    if hourly.empty:
        return f"{market.upper()} {kind.upper()} constraint {constraint_id} has no shadow prices on {day}."
    default_base, default_compare = drivers.default_hours(hourly)
    compare_hr = compare_hr or default_compare
    base_hr = base_hr or default_base

    me_id, name = drivers.constraint_info(eng, market, day, constraint_id, kind)

    lines = [f"=== {market.upper()} {kind.upper()} CONSTRAINT {constraint_id}: {name} (monitored element {me_id}) on {day} ===",
             "", "--- Hourly shadow prices ---"]
    lines += _tsv(hourly, ["hr", "shadow_price"])
    lines.append(f"Total {hourly['shadow_price'].sum():.0f} over {len(hourly)} hours.")
    lines += _market_conditions_section(eng, market, day, hourly, warnings)

    if base_hr is None:
        warnings.append(f"The peak is HE{compare_hr}, so there is no earlier hour to compare with. "
                        "Pass base_hr to choose one.")
    else:
        binding_hours = set(hourly.loc[hourly["shadow_price"] != 0, "hr"].astype(int))
        if base_hr in binding_hours:
            warnings.append(f"The base hour HE{base_hr} is itself a binding hour, so some of the market's "
                            "redispatch is already in the baseline.")
        lines += ["", f"--- Generator impacts, HE{base_hr} -> HE{compare_hr} ({source}) ---",
                  "load_mw = -sf * d_mw: + loads the constraint, - relieves it. "
                  "sf < 0 means more output loads it."]
        meta, impacts = drivers.generator_impacts(eng, market, day, constraint_id, base_hr, compare_hr,
                                                  binding_hours, source, warn=warnings.append)
        if meta:
            lines.append(f"Factor set: {meta['source_type']}, fit at {_fmt_dt(meta['reading_time'])}, "
                         f"updated {_fmt_dt(meta['updated_at'])}.")
        if not impacts.empty:
            fuel = impacts["fuel"].fillna("")
            solar = fuel.str.contains("Solar", case=False)
            redispatch = impacts["possible_redispatch"]
            lines.append(f"Net from {len(impacts)} mapped plants: {impacts['load_mw'].sum():+.0f} MW "
                         f"(solar {impacts.loc[solar, 'load_mw'].sum():+.0f}, "
                         f"other {impacts.loc[~solar, 'load_mw'].sum():+.0f}; "
                         f"excluding possible redispatch {impacts.loc[~redispatch, 'load_mw'].sum():+.0f}).")
            shown = impacts.head(top_plants).copy()
            shown["name"] = shown["name"].str.slice(0, 60)
            shown["sf"] = shown["sf"].map(lambda v: f"{v:+.3f}")
            shown["possible_redispatch"] = shown["possible_redispatch"].map(lambda v: "yes" if v else "")
            lines += _tsv(shown, ["name", "fuel", "sf", "base_mw", "compare_mw", "d_mw", "load_mw",
                                  "possible_redispatch", "sources", "disagreement"])

        load = drivers.zonal_load(eng, market, day, base_hr, compare_hr)
        lines += ["", f"--- Zonal load, HE{base_hr} -> HE{compare_hr} ---"]
        lines += _tsv(load, ["zone", "base_mw", "compare_mw", "d_mw"]) if not load.empty else [
            "No zonal load for both hours."]

        ties = drivers.tie_flows(eng, market, day, base_hr, compare_hr, warn=warnings.append)
        if not ties.empty:
            lines += ["", f"--- Tie flows: actual minus scheduled, HE{base_hr} -> HE{compare_hr} ---"]
            lines += _tsv(ties, ["tie", "base_unscheduled_mw", "compare_unscheduled_mw", "d_mw"])

    if autoflow_source != "none":
        lines += _autoflow_section(eng, market, constraint_id, day, hourly, base_hr, compare_hr, autoflow_source,
                                   warnings)

    if include_outages:
        lines += _outage_sections(eng, market, constraint_id, day, kind, hourly, me_id, top_outages, warnings)

    if me_id is None:
        defn = network.constraint_definition(eng, market, constraint_id, warnings.append)
        me_id = defn["me_id"]
        if me_id is None:
            warnings.append(f"Constraint {constraint_id} is not mapped to a monitored element, so there are no "
                            "desk notes for it.")
    notes = drivers.element_notes(eng, market, [me_id], day - datetime.timedelta(days=note_days), day)
    total_notes = len(notes)
    if max_notes:
        notes = notes.head(max_notes)
    lines += ["", f"--- Desk notes on monitored element {me_id}, last {note_days} days (newest first) ---"]
    if notes.empty:
        lines.append("No notes.")
    for _, n in notes.iterrows():
        lines.append(f"{n['dt']:%Y-%m-%d} {n['context']}: {n['body']}")
    if len(notes) < total_notes:
        lines.append(f"({total_notes - len(notes)} older notes omitted; raise max_notes to see them.)")

    return "\n".join(lines + _warnings_section(warnings))


@server.prompt()
def binding_drivers(market: str, date: str = "") -> str:
    """Explain why the day's highest constraints bound, in the style of the desk's notes."""
    _check_choice("market", market, osearch.MARKETS)
    day = date or _resolve_date(None, ANALYSIS_DAYS_AHEAD).isoformat()

    with open(DRIVERS_PROMPT_PATH, "r") as f:
        template = f.read()
    if template.startswith("<!--"):
        template = template.split("-->", 1)[1].lstrip()
    return template.replace("{{market}}", market).replace("{{MARKET}}", market.upper()).replace("{{date}}", day)


@server.prompt()
def outage_briefing(market: str, date: str = "") -> str:
    """Summarize and categorize the outages planned to start on a day (default T+2)."""
    _check_choice("market", market, osearch.MARKETS)
    day = date or _resolve_date(None, 2).isoformat()

    with open(BRIEFING_PROMPT_PATH, "r") as f:
        template = f.read()
    # Drop the leading HTML comment, which is only for people editing the file.
    if template.startswith("<!--"):
        template = template.split("-->", 1)[1].lstrip()
    return template.replace("{{market}}", market).replace("{{MARKET}}", market.upper()).replace("{{date}}", day)


if __name__ == "__main__":
    server.run("stdio")
