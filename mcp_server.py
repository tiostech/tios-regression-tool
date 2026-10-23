"""MCP server that gives AI clients (Claude Code, Gemini CLI, ...) the outage search.

It runs on your own machine and talks to the client over stdin/stdout, so the AI
work is billed to your client subscription, not to an API key. It uses the same
config/mysql.yml (or TIOS_DB_PASSWORD) and SSH tunnel as the Streamlit app, and only
reads from the database.

Run it from the client, not by hand -- see the "AI tools (MCP)" section of app.py.
To check that it starts:  .venv/bin/python mcp_server.py   (then Ctrl-C)
"""

import datetime
import functools
import logging
import os
import threading
import time

import pandas as pd
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from sqlalchemy.exc import DBAPIError

from lib import autoflow
from lib import constraint_drivers as drivers
from lib import db
from lib import factors
from lib import forecast_models
from lib import links
from lib import market_conditions
from lib import monitored_elements
from lib import network
from lib import outage_impacts
from lib import outage_groups
from lib import outage_search as osearch
from lib import shadow_forecasts
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
        "get_autoflow gives autoflow over a date range; get_shadow_forecasts gives the desk's RT shadow price "
        "forecasts for a day, get_forecast_model shows the models and features behind a constraint's forecast, "
        "and get_forecast_accuracy scores the forecasts against actuals over a date range; "
        "get_outage_impacts checks outages (including planned ones on future days) against a constraint or "
        "monitored element, or one monitored element/outage pair with outage_id. "
        "Constraint, monitored element, and outage names in the output are often markdown links to their "
        "EnergyCore pages; keep those links when you mention them in your answer. get_monitored_element gives an "
        "element's description and the elements it is linked to, often the same line in another market under a "
        "different name; check it before saying a constraint is not shared with another market. "
        "get_outage_group gives an outage's groups and every group member; get_outage_quicklinks gives the "
        "monitored elements the desk quicklinked to an outage or group (in any market), and "
        "get_monitored_element_notes gives all desk notes on elements over a date range. "
        "get_constraint_factors gives a constraint's promoted shift factors: the most positive and most "
        "negative pnodes."
    ),
)

_engine = None
_notes_cache = {}
_notes_locks = {m: threading.Lock() for m in osearch.MARKETS}
_link_graph_cache = None


def engine():
    global _engine
    if _engine is None:
        _engine = db.standalone_engine()
    return _engine


def market_notes(market: str) -> pd.DataFrame:
    """All quick notes for a market, reloaded after NOTES_CACHE_SECONDS. A load takes 20 seconds
    or more; a call that arrives during one waits for it instead of starting another."""
    with _notes_locks[market]:
        loaded = _notes_cache.get(market)
        if loaded is None or time.time() - loaded[0] > NOTES_CACHE_SECONDS:
            loaded = (time.time(), osearch.load_market_notes(engine(), market))
            _notes_cache[market] = loaded
        return loaded[1]


def _preload_notes() -> None:
    """Loads every market's notes at startup, so the first tool call does not wait for them.
    A failed load (say, the tunnel is closed) is left for the first call to retry."""
    for market in osearch.MARKETS:
        try:
            market_notes(market)
        except Exception:
            log.warning("Could not preload %s notes", market, exc_info=True)


def _link_graph() -> dict:
    """Links between monitored elements from every market's descriptions, reloaded after NOTES_CACHE_SECONDS."""
    global _link_graph_cache
    if _link_graph_cache is None or time.time() - _link_graph_cache[0] > NOTES_CACHE_SECONDS:
        _link_graph_cache = (time.time(), monitored_elements.link_graph(engine()))
    return _link_graph_cache[1]


# MySQL client errors that mean the database could not be reached: can't connect, server has gone
# away, lost connection. With the SSH tunnel closed, connecting to its local port gives 2003.
CONNECTION_ERROR_CODES = {2003, 2005, 2006, 2013}
ACCESS_DENIED_CODE = 1045

log = logging.getLogger(__name__)


def _db_address() -> str:
    try:
        _, host, port, database = db.settings()
        return f"{database} at {host}:{port}"
    except Exception:
        return "the database"


def _readable_errors(fn):
    """Turns a tool's failures into ToolError, whose message reaches the client.

    The MCP SDK reports any other exception as a bare "Error executing tool <name>", which is
    how a closed SSH tunnel used to look: the reason only reached the server log.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ToolError:
            raise
        except db.ConfigError as e:
            raise ToolError(f"Database settings problem: {e}") from e
        except DBAPIError as e:
            orig = getattr(e, "orig", None)
            code = orig.args[0] if orig is not None and getattr(orig, "args", None) else None
            orig_args = getattr(orig, "args", None) or ()
            detail = (f"MySQL error {code}: {orig_args[1]}" if code is not None and len(orig_args) > 1
                      else str(orig if orig is not None else e).splitlines()[0])
            if code in CONNECTION_ERROR_CODES or e.connection_invalidated:
                raise ToolError(
                    f"Can't reach {_db_address()}, so the SSH tunnel is probably not open. Start the tunnel "
                    f"and try again; this is not a problem with the request. ({detail})") from e
            if code == ACCESS_DENIED_CODE:
                raise ToolError(f"The database refused the login. Check the username and password in the "
                                f"extension settings (or config/mysql.yml). ({detail})") from e
            log.exception("Database error in %s", fn.__name__)
            raise ToolError(f"Database error: {detail}") from e
        except ValueError as e:
            raise ToolError(str(e)) from e
        except Exception as e:
            log.exception("Unexpected error in %s", fn.__name__)
            raise ToolError(f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}") from e
    return wrapper


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
@_readable_errors
def list_upcoming_outages(market: str, days_ahead: int = 2, date: str | None = None,
                          include_ignored: bool = False, check_history: bool = True, window: str = "starts",
                          equipment_types: list[str] | None = None, min_kv: float | None = None,
                          flags: list[str] | None = None) -> str:
    """List transmission outages for one day, one row per equipment ID.

    Args:
        market: miso, spp, pjm, ercot, or caiso.
        days_ahead: Days from today (2 = T+2). Ignored when date is given.
        date: A specific day, YYYY-MM-DD, in market local time.
        include_ignored: Also list outages that EnergyCore's regression ignore rules drop
            (for example cancelled or denied requests, and CAISO hot-line work).
        check_history: Count note matches, group note matches, annotations, and flags for
            each outage.
        window: starts (planned to start on the day), ends (ending on the day), or in_effect
            (out at any time during the day). ends and in_effect use actual times where known.
        equipment_types: Only these equipment types, for example ["Line", "Transformer"].
            Most MISO outages are circuit breakers.
        min_kv: Only outages at this voltage or higher.
        flags: Only outages whose latest desk flag is one of these, for example
            ["alert", "watch"]. MISO and SPP flag types: alert, watch, safe, auto_alert,
            auto_watch, auto_safe. PJM has no flags.

    Returns tab-separated rows. note_matches counts notes naming the equipment ID;
    group_note_matches counts notes naming any of its groups. MISO notes usually name an
    outage by group (GID), so for an outage with group_note_matches, call
    get_outage_documentation with id_type="group" and each of its group_ids. Outages with
    note_matches, group_note_matches, annotations, and flag all empty have no history, so
    get_outage_documentation would return nothing useful for them.
    """
    _check_choice("market", market, osearch.MARKETS)
    _check_choice("window", window, upcoming_outages.WINDOWS)
    target_date = _resolve_date(date, days_ahead)
    warnings = []

    outages = upcoming_outages.list_upcoming_outages(engine(), market, target_date,
                                                     apply_ignore_rules=not include_ignored,
                                                     warn=warnings.append, window=window)
    listed = len(outages)
    if equipment_types:
        wanted = {t.strip().lower() for t in equipment_types}
        outages = [o for o in outages if o["equipment_type"].strip().lower() in wanted]
    if min_kv is not None:
        outages = [o for o in outages if o["kv"] is not None and float(o["kv"]) >= min_kv]
    if flags:
        upcoming_outages.add_flags(engine(), market, outages, warn=warnings.append)
        wanted = {f.strip().lower() for f in flags}
        outages = [o for o in outages if o["flag_type"].lower() in wanted]
    upcoming_outages.add_group_ids(engine(), market, outages, warn=warnings.append)

    lookback = "3 Years"
    if check_history:
        start_date = osearch.lookback_start_date(lookback, pd.Timestamp.now())
        upcoming_outages.add_history(engine(), market, outages, market_notes(market),
                                     start_date.strftime("%Y-%m-%d"), warn=warnings.append)

    window_text = {"starts": "Planned start date", "ends": "End date", "in_effect": "In effect on"}[window]
    filters = [f"types {', '.join(equipment_types)}" if equipment_types else "",
               f"kV >= {min_kv:g}" if min_kv is not None else "", f"flag {', '.join(flags)}" if flags else ""]
    lines = [
        f"Market: {market.upper()}",
        f"{window_text}: {target_date.isoformat()}",
        "Filter: current report (most_recent = 1); "
        + ("ignore rules NOT applied" if include_ignored else "EnergyCore regression_ignore_rules applied")
        + "".join(f"; {f}" for f in filters if f),
        f"Outages: {len(outages)}" + (f" (of {listed} before the type, kV, and flag filters)" if listed != len(outages) else ""),
    ]
    if check_history:
        with_history = [o for o in outages
                        if o["note_matches"] or o["group_note_matches"] or o["annotations"] or o["flag_type"]]
        lines.append(f"With history (note lookback {lookback}): {len(with_history)}")

    header = ["equipment_id", "name", "equipment_type", "kv", "planned_start", "planned_end", "request_status",
              "request_ids", "group_ids"]
    if check_history:
        header += ["note_matches", "group_note_matches", "annotations", "flag"]
    elif flags:
        header += ["flag"]
    lines += ["", "\t".join(header)]

    for o in outages:
        groups = [links.md(g, links.outage_group(market, g)) if market == "miso" else g for g in o["group_ids"]]
        row = [o["outage_equipment_id"], links.md(o["name"], links.outage(market, o["outage_equipment_id"])),
               o["equipment_type"], "" if o["kv"] is None else str(o["kv"]),
               _fmt_dt(o["planned_start"]), _fmt_dt(o["planned_end"]), ", ".join(o["request_statuses"]),
               ", ".join(str(r) for r in o["request_ids"]), ", ".join(groups)]
        if check_history:
            row += [str(o["note_matches"]), str(o["group_note_matches"]), str(o["annotations"]), o["flag_type"]]
        elif flags:
            row += [o["flag_type"]]
        lines.append("\t".join(row))

    return "\n".join(lines + _warnings_section(warnings))


@server.tool(annotations=READ_ONLY)
@_readable_errors
def get_outage_documentation(market: str, outage_id: str, id_type: str = "equipment",
                             notes_market: str | None = None, lookback: str = "3 Years", context: str = "All",
                             max_notes_per_element: int = 25) -> str:
    """Documentation for one outage: the same text as "Copy Full Output to Clipboard" on the
    Outage & Shadow Price Search page.

    It has the match fields, flag notes, outage annotations, quick notes that mention the
    outage (with summed shadow prices), and monitored element descriptions.

    MISO desk notes usually name an outage by its group ("GID 2972"), not its equipment ID,
    so an equipment search often finds no notes for an outage that has plenty. For an outage
    with group IDs (list_upcoming_outages gives them, with group_note_matches), search by
    group: one call per group ID. A group search also finds the notes naming the group's
    member equipment.

    Args:
        market: Market the outage is in: miso, spp, pjm, ercot, or caiso.
        outage_id: One equipment ID, one group ID, or search text, depending on id_type.
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

    lines = []
    if raw_eq_ids:
        lines.append("EnergyCore outage search: " + ", ".join(links.md(e, links.outage(market, e)) for e in raw_eq_ids))
    lines.append(osearch.clipboard_text(notes_market, search_fields, outage_names, flag_notes, me_groups, df_shown,
                                        meta_descriptions, link_names=True))
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
@_readable_errors
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
    df["linked"] = [monitored_elements.short_links(_link_graph(), market, m) for m in df["me_id"]]
    df["name"] = [links.md(n, links.constraint(market, c, day)) for n, c in zip(df["name"], df["constraint_id"])]
    df["me_id"] = [links.md(m, links.monitored_element(market, m)) for m in df["me_id"]]

    lines = [f"{market.upper()} {kind.upper()} binding constraints on {day}"
             + (" (includes preliminary prices)" if df["preliminary"].fillna(0).any() else ""),
             "History columns cover the 3 years before this day; total_30d is the 30 days before it. linked = "
             "elements the desk's descriptions link to (often the same line in another market; see "
             "get_monitored_element).", ""]
    lines += _tsv(df, ["constraint_id", "me_id", "name", "total", "hours", "first_hr", "last_hr", "peak_hr", "peak",
                       "total_30d", "days_bound", "max_day", "last_bound", "linked"])
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
@_readable_errors
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


@server.tool(annotations=READ_ONLY)
@_readable_errors
def get_shadow_forecasts(market: str, constraint_id: int | None = None, date: str | None = None, days_ahead: int = 1,
                         limit: int = 25) -> str:
    """The desk's RT shadow price forecasts for a day, with actual RT shadow prices where the
    day has happened.

    The forecast that counts (forecast) is the manual one if a trader set it, else the custom
    one, else the default model forecast; a manual 0 zeroes out the model. Forecasts are
    expected values ($/MWh per hour), so they spread small amounts over many hours.

    Args:
        market: miso, spp, pjm, ercot, or caiso.
        constraint_id: One RT constraint, for its hourly forecast components next to actuals.
            Omit to list the day's constraints by forecast total.
        date: The day, YYYY-MM-DD.
        days_ahead: Days from today when date is not given (1 = tomorrow, -2 = the latest day
            with complete actuals).
        limit: Most constraints to list when constraint_id is omitted.
    """
    _check_choice("market", market, osearch.MARKETS)
    day = _resolve_date(date, days_ahead)
    df = shadow_forecasts.hourly(engine(), market, day, day, [constraint_id] if constraint_id else None)
    if df.empty:
        return f"No RT shadow price forecasts or actuals for {market.upper()} on {day}."
    prelim = " (actuals include preliminary prices)" if df["preliminary"].any() else ""

    if constraint_id:
        name = next((n for n in df["name"] if n), "")
        me_id = df["me_id"].dropna().iloc[0] if df["me_id"].notna().any() else None
        lines = [f"=== {market.upper()} RT {_constraint_title(market, constraint_id, name, me_id, day)} on {day}{prelim} ===",
                 f"Forecast total {df['forecast'].sum():.0f}, actual total {df['actual'].sum():.0f}."]
        if not df["has_forecast"].any():
            lines.append("No forecast row for this constraint ID; the element may be forecast under another ID "
                         "(see get_forecast_accuracy with by=element).")
        lines += [""] + _tsv(df.sort_values("hr"), ["hr"] + shadow_forecasts.FORECAST_COLUMNS + ["actual"], decimals=1)
        return "\n".join(lines)

    totals = (df.groupby("constraint_id")
              .agg(me_id=("me_id", "first"), name=("name", "first"), element=("element", "first"),
                   forecast_total=("forecast", "sum"), forecast_peak=("forecast", "max"),
                   manual_hours=("manual_fc", lambda s: int(s.notna().sum())), actual_total=("actual", "sum"),
                   has_forecast=("has_forecast", "any"))
              .reset_index())
    totals["name"] = totals["name"].where(totals["name"] != "", totals["element"])
    totals["peak_hr"] = totals["constraint_id"].map(
        df.loc[df.groupby("constraint_id")["forecast"].idxmax()].set_index("constraint_id")["hr"])
    has_actuals = totals["actual_total"].sum() > 0

    def link_names(t: pd.DataFrame) -> pd.DataFrame:
        t = t.copy()
        t["name"] = [links.md(n, links.constraint(market, c, day)) for n, c in zip(t["name"], t["constraint_id"])]
        return t
    shown = totals[totals["forecast_total"] > 0].sort_values("forecast_total", ascending=False).head(limit)
    lines = [f"=== {market.upper()} RT shadow price forecasts on {day}{prelim} ===",
             f"{int((totals['forecast_total'] > 0).sum())} constraints with a nonzero forecast, total "
             f"{totals['forecast_total'].sum():.0f}" + (f"; actual total {totals['actual_total'].sum():.0f}." if has_actuals
                                                        else "; no actuals yet."),
             "manual_hours = hours where a trader set the forecast by hand.", ""]
    cols = ["constraint_id", "me_id", "name", "forecast_total", "peak_hr", "forecast_peak", "manual_hours"]
    lines += _tsv(link_names(_short(shown, "name")), cols + (["actual_total"] if has_actuals else []))
    if has_actuals:
        missed = totals[~totals["has_forecast"] & (totals["actual_total"] > 0)].sort_values("actual_total", ascending=False)
        if not missed.empty:
            lines += ["", "--- Bound with no forecast row for that constraint ID ---"]
            lines += _tsv(link_names(_short(missed.head(limit), "name")), ["constraint_id", "me_id", "name", "actual_total"])
    return "\n".join(lines)


@server.tool(annotations=READ_ONLY)
@_readable_errors
def get_forecast_accuracy(market: str, start_date: str | None = None, end_date: str | None = None, by: str = "element",
                          min_total: float = 1000, limit: int = 15) -> str:
    """How well the desk's RT shadow price forecasts did over a date range: the biggest misses,
    the best forecasts, and constraints that bound with no forecast at all.

    by=element (default) sums constraints on the same monitored element hour by hour, since
    elements often bind under a new constraint ID while the forecast sits on an older one.

    Columns: abs_error = sum over hours of |forecast - actual| (the dollars missed either way);
    bias = forecast_total - actual_total; norm_error = abs_error / max(actual_total,
    forecast_total), 0 perfect, 1 no overlap, up to 2 (expected-value forecasts rarely get far
    below 0.7); fc_when_bound / fc_when_not = forecast $ in hours that did / did not bind;
    bound_unforecast = actual $ on constraint IDs with no forecast row; corr = hourly correlation.

    Args:
        market: miso, spp, pjm, ercot, or caiso.
        start_date: First day, YYYY-MM-DD. Default: six days before end_date.
        end_date: Last day. Default: two days ago, the latest day with complete data.
        by: element or constraint.
        min_total: The best-forecast list only includes groups that both bound and were
            forecast, with an actual or forecast total of at least this, so tiny constraints
            with near-zero error do not crowd it.
        limit: Rows per list.
    """
    _check_choice("market", market, osearch.MARKETS)
    _check_choice("by", by, shadow_forecasts.GROUPINGS)
    end = _resolve_date(end_date, ANALYSIS_DAYS_AHEAD)
    start = datetime.date.fromisoformat(start_date) if start_date else end - datetime.timedelta(days=6)

    df = shadow_forecasts.hourly(engine(), market, start, end)
    acc = shadow_forecasts.accuracy(df, by)
    if acc.empty:
        return f"No RT shadow price forecasts or actuals for {market.upper()} from {start} to {end}."
    warnings = []
    cross_market = _cross_market_section(market, acc, start, end, min_total, limit, warnings) if by == "element" else []
    acc["linked"] = [monitored_elements.short_links(_link_graph(), market, m) for m in acc["me_id"]]
    acc = _short(acc, "name")
    # Elements link to their page; a constraint (by=constraint, or no element mapped) links to its chart.
    acc["name"] = [links.md(n, links.monitored_element(market, m) if by == "element" and pd.notna(m)
                            else links.constraint(market, c.split(",")[0], start, end) if c else None)
                   for n, m, c in zip(acc["name"], acc["me_id"], acc["constraint_ids"])]
    acc["me_id"] = acc["me_id"].map(lambda v: "" if v is None or pd.isna(v) else str(int(v)))
    acc["constraint_ids"] = acc["constraint_ids"].str.slice(0, 40)
    for col in ("corr", "norm_error"):
        acc[col] = acc[col].map(lambda v: "" if pd.isna(v) else f"{v:.2f}")
    cols = ["me_id", "name", "constraint_ids", "actual_total", "forecast_total", "bias", "abs_error", "norm_error",
            "hours_bound", "fc_when_bound", "fc_when_not", "bound_unforecast", "corr", "linked"]

    act_total, fc_total = df["actual"].sum(), df["forecast"].sum()
    material = acc[(acc[["actual_total", "forecast_total"]].max(axis=1) >= min_total)
                   & (acc["actual_total"] > 0) & (acc["forecast_total"] > 0)]
    lines = [f"=== {market.upper()} RT shadow price forecast accuracy, {start} to {end} (by {by}) ===",
             f"Actual total {act_total:.0f}; forecast total {fc_total:.0f}; summed abs_error "
             f"{acc['abs_error'].sum():.0f}; actual $ on constraint IDs with no forecast row "
             f"{acc['bound_unforecast'].sum():.0f}."
             + (" Actuals include preliminary prices." if df["preliminary"].any() else ""),
             "", f"--- Largest misses (abs_error) ---"]
    lines += _tsv(acc.head(limit), cols)
    lines += ["", f"--- Best forecasts: lowest norm_error with actual or forecast total >= {min_total:.0f} ---"]
    lines += _tsv(material.sort_values(["norm_error", "abs_error"]).head(limit), cols) if not material.empty else ["None."]
    unforecast = acc[acc["bound_unforecast"] > 0].sort_values("bound_unforecast", ascending=False)
    if not unforecast.empty:
        lines += ["", "--- Bound on constraint IDs with no forecast row ---"]
        lines += _tsv(unforecast.head(limit), cols)
    lines += cross_market
    return "\n".join(lines + _warnings_section(warnings))


def _outage_condition_parts(eng, market: str, condition: tuple, names: dict, warnings: list[str]) -> list[str]:
    """The outages a model's training hours required, with group and outage links. ``names``
    holds the equipment already named, so each is named and linked only once."""
    filter_text, ids, op, not_ids, not_op = condition
    parts = []
    if filter_text:
        groups = forecast_models.outage_filter_groups(eng, filter_text, warnings.append)

        def label(match):
            mkt, state, kind, gid = match.groups()
            text_ = f"{kind.upper()}ID {gid}" + (f" {groups[(mkt, gid)]}" if (mkt, gid) in groups else "")
            url = links.outage_group(mkt, gid) if kind == "g" and mkt == "miso" else (
                links.outage(mkt, gid) if kind == "e" else None)
            return f"{mkt}:{state}:" + links.md(text_, url)
        parts.append("filter " + forecast_models._FILTER_TOKEN.sub(label, filter_text))

    def named(eq):
        # Name and link an outage the first time; later conditions just repeat the ID.
        if eq in names:
            return eq
        found = osearch.fetch_outage_names(eng, "Equipment ID", eq, market, [eq], warn=warnings.append)
        names[eq] = found[0] if found else ""
        return links.md(f"{eq} {names[eq]}".strip(), links.outage(market, eq))
    if ids:
        parts.append(f"outages active ({op}): " + ", ".join(named(e) for e in ids))
    if not_ids:
        parts.append(f"not active ({not_op}): " + ", ".join(named(e) for e in not_ids))
    return parts


@server.tool(annotations=READ_ONLY)
@_readable_errors
def get_forecast_model(market: str, constraint_id: int, date: str | None = None, days_ahead: int = 1,
                       all_details: bool = False) -> str:
    """The regression models behind a constraint's RT shadow price forecast for a day: its
    EnergyCore model group, each model's features, training setup, outage filter, fit
    statistics, and weight in the forecast, and the hourly outputs next to the forecast table.

    simple-mode groups average the models' hourly outputs by simple_weight; shaped-mode
    groups set the daily total from the total_weight models and the hourly shape from the
    shaping_weight models (each model also has a scaling_factor). An applied group writes its
    forecast into the forecast table's manual column, but traders can edit it afterwards.

    Features are written as formulas: a - b (delta), a + b (sum), a * b (product), «name»
    for a saved custom function, and "same" when the forecast day uses the same series it
    was trained on. Outage conditions shared by several models are listed once (O1, O2, ...).

    Args:
        market: miso, spp, pjm, ercot, or caiso.
        constraint_id: RT constraint ID. If it has no group that day, a group for another
            constraint on the same monitored element is used.
        date: The forecast day, YYYY-MM-DD.
        days_ahead: Days from today when date is not given (1 = tomorrow).
        all_details: Show models with no weight in full and expand «custom» functions into
            their formulas (by default unweighted models get one line of features).
    """
    _check_choice("market", market, osearch.MARKETS)
    day = _resolve_date(date, days_ahead)
    eng = engine()
    warnings = []

    group, note = forecast_models.find_group(eng, market, constraint_id, day)
    if group is None:
        return (f"No {market.upper()} model group forecasts constraint {constraint_id} on {day}, nor any other "
                "constraint on its monitored element.")
    cid = int(group["group_constraint_id"])
    models = forecast_models.group_models(eng, market, group["id"])
    shadow = shadow_forecasts.hourly(eng, market, day, day, [cid]).sort_values("hr")
    name = next((n for n in shadow["name"] if n), "")
    me_id = shadow["me_id"].dropna().iloc[0] if shadow["me_id"].notna().any() else None

    group_link = links.md(f"{group['id']} {group['name']}", links.model_group(market, group["id"]))
    lines = [f"=== {market.upper()} model group {group_link} for {_constraint_title(market, cid, name, me_id, day)} "
             f"on {day} ==="]
    if note:
        lines.append(note)
    allocation = group["constraint_allocation"] or {}
    lines.append(f"Mode {group['mode']}, state {group['group_state']}"
                 + (", early" if group["early"] else "")
                 + f"; group total {group['estimated_proposed_forecast'] or 0:.0f}"
                 + (f", allocated {', '.join(f'{c}: {float(p):.0%}' for c, p in allocation.items())}"
                    f" ({group['constraint_allocation_mode']})" if allocation else "")
                 + (f"; floor {group['forecast_floor']:g}" if group["forecast_floor"] is not None else "")
                 + (f"; ceiling {group['forecast_ceiling']:g}" if group["forecast_ceiling"] is not None else "")
                 + f". Created {_fmt_dt(group['created_at'])}, updated {_fmt_dt(group['updated_at'])}.")
    scaling = group["forecast_hourly_scaling_factors"] or []
    if scaling and any(float(s) != 1.0 for s in scaling):
        lines.append("Hourly scaling factors: " + ", ".join(f"{float(s):g}" for s in scaling))
    if models.empty:
        return "\n".join(lines + ["No models in this group."])

    total, implied_hourly = forecast_models.implied_forecast(group, models, cid)
    table_total = shadow["forecast"].sum()
    lines.append(f"The weights imply {total:.0f} for constraint {cid}; the forecast table has {table_total:.0f} "
                 f"(manual {shadow['manual_fc'].sum():.0f}, default {shadow['default_fc'].sum():.0f})."
                 + ("" if abs(total - table_total) <= max(5, 0.05 * table_total) else
                    " They differ, so the forecast was edited after the models ran, or the group was not applied."))

    models["total_for_constraint"] = [None if (h := forecast_models.hourly_output(o, cid)) is None else h.sum()
                                      for o in models["output"]]
    models = models.sort_values("weighted", ascending=False, kind="stable")
    detailed = models["weighted"] | all_details

    # Outage conditions are usually shared by many models: list each once, as O1, O2, ...
    models["condition"] = [forecast_models.outage_condition(m.parameters, m.outage_equipment_filter)
                           for m in models.itertuples()]
    labels = {c: f"O{i + 1}" for i, c in enumerate(dict.fromkeys(models["condition"]))}
    models["outages"] = models["condition"].map(labels)

    # Model names often share a long prefix (the outage set); say it once.
    names = models["name"].fillna("").tolist()
    prefix = os.path.commonprefix(names) if len(names) > 1 else ""
    prefix = prefix[:prefix.rfind(" ") + 1] if len(prefix) > 15 else ""
    models["short_name"] = [n[len(prefix):] or n for n in names]

    shown = models.copy()
    shown["model"] = [links.md(i, links.model_output(market, i)) for i in shown["id"]]
    shown["window"] = [f"{a}..{b} {t}" for a, b, t in zip(shown["start_at"], shown["end_at"], shown["date_type"])]
    shown = _short(shown, "short_name", 70)
    cols = ["model", "short_name", "regression", "model_state", "total_weight", "shaping_weight", "simple_weight",
            "scaling_factor", "window", "outages", "training_hours", "total_for_constraint"]
    cols += [c for c in ("cv_adjusted_rsquare", "weighted_rsquare", "mae", "rmse") if c in shown]
    same = [c for c in ("regression", "model_state", "scaling_factor", "window", "outages")
            if shown[c].astype(str).nunique() == 1 and len(shown) > 1]
    lines += ["", "--- Models (weights: total and shaping for shaped mode, simple for simple mode) ---"]
    if prefix:
        lines.append(f"Names below drop the prefix every model shares: \"{prefix.strip()}\".")
    if same:
        lines.append("All models: " + "; ".join(f"{c} {_tsv(shown.head(1), [c], decimals=2)[1]}" for c in same) + ".")
    lines += _tsv(shown, [c for c in cols if c not in same], decimals=2)

    lines += ["", "--- Training data: outage conditions and spells (stretches where the condition held) ---"]
    outage_names = {}
    for condition, label in labels.items():
        first = models[models["condition"] == condition].iloc[0]
        parts = _outage_condition_parts(eng, market, condition, outage_names, warnings) or ["no outage condition"]
        lines.append(f"{label}: " + "; ".join(parts))
        if first["spells"]:
            spells = "; ".join(f"{a} to {b}" for a, b, _ in first["spells"][-5:])
            n = len(first["spells"])
            lines.append(f"    model {first['id']}: {n} spell{'s' if n != 1 else ''}, {first['training_hours']} hours"
                         + (f"; latest 5: {spells}" if n > 5 else f": {spells}"))

    setups = {m.id: forecast_models.training_setup(m.parameters) for m in models[detailed].itertuples()}
    shared_setup = [x for x in next(iter(setups.values()), []) if all(x in v for v in setups.values())]
    lines += ["", "--- Features: trained on; fed on the forecast day (\"same\" = the same series) ---"]
    if shared_setup:
        lines.append("Setup for every model below: " + "; ".join(shared_setup) + ".")
    for m, full in zip(models.itertuples(), detailed):
        feats = forecast_models.features(m.parameters, expand_custom=all_details)
        if not full:
            lines.append(f"* {m.id} ({m.regression}, no weight): "
                         + ("; ".join(forecast_models.feature_lines(feats, with_fed=False))
                            or "no features (average of matching hours)"))
            continue
        weights = ", ".join(f"{k} {getattr(m, k + '_weight'):g}" for k in ("total", "shaping", "simple")
                            if getattr(m, k + "_weight")) or "no weight"
        lines.append(f"* {m.id} ({m.regression}, {weights}): {m.short_name}")
        lines += [f"    - {f}" for f in forecast_models.feature_lines(feats)] or [
            "    - no features (an average of matching hours)"]
        own_setup = [x for x in setups[m.id] if x not in shared_setup]
        if own_setup:
            lines.append("    - setup: " + "; ".join(own_setup))

    hourly = shadow[["hr", "default_fc", "manual_fc", "forecast", "actual"]].copy()
    group_default = forecast_models.hourly_output(group["default_forecast"], cid)
    if group_default is not None:
        hourly.insert(1, "group_default", list(group_default[:len(hourly)]) + [None] * (len(hourly) - len(group_default)))
    if implied_hourly is not None:
        hourly.insert(1, "implied", implied_hourly[:len(hourly)])
    out_cols = []
    for m in models[models["weighted"]].itertuples():
        h = forecast_models.hourly_output(m.output, cid)
        if h is not None:
            hourly[f"m{m.id}"] = list(h[:len(hourly)]) + [None] * (len(hourly) - len(h))
            out_cols.append(f"m{m.id}")
    lines += ["", f"--- Hourly: weighted models' outputs for constraint {cid} (m<model id>), "
                  "then the forecast table ---"]
    has_actuals = hourly["actual"].sum() > 0
    cols = ["hr"] + out_cols + [c for c in ("implied", "group_default") if c in hourly] + \
           ["default_fc", "manual_fc", "forecast"] + (["actual"] if has_actuals else [])
    lines += _tsv(hourly, cols, decimals=1)
    return "\n".join(lines + _warnings_section(warnings))


@server.tool(annotations=READ_ONLY)
@_readable_errors
def get_monitored_element(market: str, me_id: int, max_description_chars: int = 4000) -> str:
    """One monitored element: name, type, stations and areas, the same equipment in the other
    direction, the desk's description from its EnergyCore page, the elements its description
    links to (often the same line in another market, which names alone cannot pair: MISO 834
    Lime Creek-Barton is SPP 1376 TMP421_29021), and its recent constraints.

    Args:
        market: miso, spp, pjm, ercot, or caiso.
        me_id: Monitored element ID. RT and DA use different IDs for the same line.
        max_description_chars: Cut the description after this many characters. 0 for all of it.
    """
    _check_choice("market", market, osearch.MARKETS)
    eng = engine()
    row = monitored_elements.element(eng, market, me_id)
    if row is None:
        return f"No {market.upper()} monitored element {me_id}."

    def station(name, kv, area):
        return " ".join(x for x in (name, f"{kv:g} kV" if kv else "", f"({area})" if area else "") if x)
    title = links.md(f"{me_id} {row['monitored_element_name']}", links.monitored_element(market, me_id))
    lines = [f"=== {market.upper()} monitored element {title} ==="]
    details = []
    if row["equipment_type"]:
        details.append(f"type {row['equipment_type']}{' (multi)' if row['is_multi'] else ''}")
    if row["from_station"] or row["to_station"]:
        details.append(f"{station(row['from_station'], row['from_kv'], row['from_area'])} -> "
                       f"{station(row['to_station'], row['to_kv'], row['to_area'])}")
    for key, label in (("direction", "direction"), ("non_directional_name", "non-directional name"), ("baa", "BAA")):
        if row[key]:
            details.append(f"{label} {row[key]}")
    details.append(f"last used {row['last_used']}")
    summary = "; ".join(details)
    lines.append(summary[0].upper() + summary[1:] + ".")

    others = monitored_elements.other_directions(eng, market, row)
    if not others.empty:
        lines.append("Other directions: " + ", ".join(
            f"{links.md(f'{o.me_id} {o.name}', links.monitored_element(market, o.me_id))} (last used {o.last_used})"
            for o in others.itertuples()))

    linked = _link_graph().get((market, int(me_id)), [])
    lines += ["", "--- Linked elements (from desk descriptions; out = this element's description links it, "
                  "in = the other element's does) ---"]
    if linked:
        table = pd.DataFrame(linked)
        found = {}
        for mkt, ids in table.groupby("market")["me_id"]:
            found.update({(mkt, k): v for k, v in monitored_elements.names(eng, mkt, ids).items()})
        table["element"] = [links.md(f"{m}:{i} {found.get((m, i), '')}".strip(), links.monitored_element(m, i))
                            for m, i in zip(table["market"], table["me_id"])]
        lines += _tsv(table, ["element", "direction", "context"])
    else:
        lines.append("None.")

    description = osearch.clean_annotation_notes(row["description"] or "")
    if max_description_chars and len(description) > max_description_chars:
        description = description[:max_description_chars] + f"... ({len(description)} characters in all)"
    lines += ["", "--- Description ---", description or "None."]

    constraints = monitored_elements.recent_constraints(eng, market, me_id)
    lines += ["", "--- Constraints on this element, most recently used first ---"]
    if constraints.empty:
        lines.append("None.")
    else:
        constraints["name"] = [
            links.md(n, links.constraint(market, c, u - datetime.timedelta(days=30), u) if isinstance(u, datetime.date)
                     else None) for n, c, u in zip(constraints["name"], constraints["constraint_id"], constraints["last_used"])]
        lines += _tsv(constraints, ["constraint_id", "name", "contingency", "rt_max_limit", "last_used"])
    return "\n".join(lines)


def _linked_elements_line(eng, market: str, me_id) -> list[str]:
    """Description and linked elements lines for a tool header; empty when there is nothing."""
    if me_id is None or pd.isna(me_id):
        return []
    lines = []
    row = monitored_elements.element(eng, market, int(me_id))
    if row and row["description"]:
        lines.append("Element description: " + monitored_elements.short_description(row["description"], 300))
    linked = _link_graph().get((market, int(me_id)), [])
    if linked:
        lines.append("Linked elements: " + "; ".join(
            links.md(f"{l['market']}:{l['me_id']}", links.monitored_element(l["market"], l["me_id"]))
            + (f" ({l['context']})" if l["context"] else "") for l in linked)
            + " (get_monitored_element has details).")
    return lines


def _cross_market_section(market: str, acc: pd.DataFrame, start: datetime.date, end: datetime.date,
                          min_total: float, limit: int, warnings: list[str]) -> list[str]:
    """Elements linked to elements in other markets, with both sides' actual and forecast totals."""
    graph = _link_graph()
    pairs = [(r, l) for r in acc.itertuples() if pd.notna(r.me_id)
             for l in monitored_elements.cross_market_links(graph, market, r.me_id)]
    if not pairs:
        return []
    other = {}
    for mkt in {l["market"] for _, l in pairs if l["market"] in osearch.MARKETS}:
        try:
            oacc = shadow_forecasts.accuracy(shadow_forecasts.hourly(engine(), mkt, start, end), "element")
            other[mkt] = oacc.dropna(subset=["me_id"]).assign(me_id=lambda d: d["me_id"].astype(int)).set_index("me_id")
        except Exception as e:
            warnings.append(f"Could not load {mkt.upper()} forecasts for linked elements: {e}")
    rows = []
    for r, l in pairs:
        o = other.get(l["market"])
        o_row = o.loc[l["me_id"]] if o is not None and l["me_id"] in o.index else None
        o_act = float(o_row["actual_total"]) if o_row is not None else 0.0
        o_fc = float(o_row["forecast_total"]) if o_row is not None else 0.0
        if max(r.actual_total, r.forecast_total, o_act, o_fc) < min_total:
            continue
        rows.append({"me_id": int(r.me_id), "name": r.name,
                     "linked": links.md(f"{l['market']}:{l['me_id']}", links.monitored_element(l["market"], l["me_id"])),
                     "linked_name": "" if o_row is None else str(o_row["name"])[:50], "context": l["context"][:40],
                     "actual": r.actual_total, "forecast": r.forecast_total, "linked_actual": o_act,
                     "linked_forecast": o_fc, "combined_actual": r.actual_total + o_act,
                     "combined_forecast": r.forecast_total + o_fc})
    if not rows:
        return []
    table = pd.DataFrame(rows).sort_values("combined_actual", ascending=False).head(limit)
    return ["", f"--- Shared with other markets (elements linked in desk descriptions, combined actual or forecast "
                f">= {min_total:.0f}; a link can be the flipped direction, see context) ---"] + \
        _tsv(table, ["me_id", "name", "linked", "linked_name", "context", "actual", "forecast", "linked_actual",
                     "linked_forecast", "combined_actual", "combined_forecast"])


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
@_readable_errors
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

    title = links.md(f"{market.upper()} constraint {constraint_id}", links.constraint(market, constraint_id, start, end))
    lines = [f"=== Autoflow on {title}, {start} to {end} ===", _autoflow_header(meta), ""]
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


def _link_outages(df: pd.DataFrame, market: str, col: str = "name", active: bool = False) -> pd.DataFrame:
    """Makes ``col`` a link to each row's outage search, using the row's market column when there is one."""
    df = df.copy()
    markets = df["market"] if "market" in df else pd.Series(market, index=df.index)
    df[col] = [links.md(label, links.outage(m, eq, active))
               for label, m, eq in zip(df[col], markets, df["outage_equipment_id"])]
    return df


def _constraint_title(market: str, constraint_id, name: str, me_id, start: datetime.date,
                      end: datetime.date | None = None) -> str:
    """'constraint 123: [NAME](chart) (monitored element [456](page))' for tool headers."""
    title = f"constraint {constraint_id}: {links.md(name or constraint_id, links.constraint(market, constraint_id, start, end))}"
    if me_id is not None and not pd.isna(me_id):
        title += f" (monitored element {links.md(int(me_id), links.monitored_element(market, me_id))})"
    return title


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
            shown = _link_outages(_short(impacts.head(top), "name"), market)
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
                    keep = _link_outages(_short(keep, "name", 50), market, active=True)
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
        flags = _short(matches["flags"].head(top), "notes", 200)
        flags["outage_equipment_id"] = [links.md(e, links.outage(market, e)) for e in flags["outage_equipment_id"]]
        lines += _tsv(flags, ["outage_equipment_id", "flag_type", "updated_at",
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
@_readable_errors
def get_outage_impacts(market: str, constraint_id: int | None = None, date: str | None = None, start_hr: int = 1,
                       end_hr: int = 24, top: int = 20, monitored_element_id: int | None = None,
                       outage_id: int | None = None, outage_market: str | None = None) -> str:
    """How much each transmission outage in effect loads one constraint or monitored element,
    from EnergyCore's PowerWorld network model. Works for future days too, using planned
    outages, so it can check an upcoming outage against an element.

    Columns: pre_ctg_lodf_pct is the plain LODF from the outage to the monitored element, the
    same number as EnergyCore's LODF tool. individual_mw is the change in post-contingency
    flow from this outage alone, and in_combination_mw what it adds on top of the other
    outages; both include the constraint's contingency.

    Args:
        market: miso, spp, pjm, ercot, or caiso. MISO also includes mapped SPP and PJM outages.
        constraint_id: Official constraint ID. Give this or monitored_element_id.
        date: The day, YYYY-MM-DD. Default: two days ago, the latest day with complete data.
        start_hr: First hour ending of the period to collect outages for.
        end_hr: Last hour ending of the period.
        top: Most outages to list.
        monitored_element_id: Official monitored element ID, instead of constraint_id. The
            contingency comes from the element's most recently used constraint.
        outage_id: Show only this outage equipment ID: one monitored element/outage pair. It is
            computed even when it is too small to make the list or is not in effect that day.
        outage_market: Market of outage_id, when it is a neighbor's (MISO: spp or pjm).
            Default: market.
    """
    _check_choice("market", market, osearch.MARKETS)
    if (constraint_id is None) == (monitored_element_id is None):
        raise ValueError("Give either constraint_id or monitored_element_id.")
    outage_market = outage_market or market
    _check_choice("outage_market", outage_market, (market,) + outage_impacts.NEIGHBOR_MARKETS.get(market, ()))
    outage_key = f"{outage_market}:{int(outage_id)}" if outage_id is not None else None
    day = _resolve_date(date, ANALYSIS_DAYS_AHEAD)
    warnings = []
    eng = engine()
    start = datetime.datetime.combine(day, datetime.time()) + datetime.timedelta(hours=start_hr - 1)
    end = datetime.datetime.combine(day, datetime.time()) + datetime.timedelta(hours=end_hr)
    summary, impacts = outage_impacts.outage_impacts(eng, market, constraint_id, start, end, day, end_hr,
                                                     warn=warnings.append, me_id=monitored_element_id,
                                                     outage_key=outage_key)
    used_constraint = summary.get("constraint_id")
    if constraint_id is not None or used_constraint is not None:
        subject = _constraint_title(market, used_constraint, summary.get("name"), summary.get("me_id"), day)
    else:
        me_id = summary.get("me_id")
        subject = (f"monitored element {links.md(me_id, links.monitored_element(market, me_id))} "
                   f"{summary.get('me_name') or ''}").rstrip()
    lines = [f"=== Outage impacts on {market.upper()} {subject}, {day} HE{start_hr}-HE{end_hr} ==="]
    if constraint_id is None:
        lines.append(f"Contingency from constraint {used_constraint}, the element's most recently used constraint."
                     if used_constraint is not None else "The element has no constraint, so there is no contingency.")
    if outage_key and not impacts.empty:
        impacts = impacts[impacts["outage_keys"].map(lambda keys: outage_key in keys)]
    if impacts.empty:
        return "\n".join(lines + ["No outage impacts (see warnings)."] + _warnings_section(warnings))
    limit = summary.get("limit") or {}
    lines += [f"Case {summary['version']}; monitored {summary.get('monitored_branch')}; contingency "
              f"{', '.join(summary.get('contingency_branches') or []) or 'none mapped'}; limit "
              f"{limit.get('limit_mw') or 'unknown'} ({limit.get('source')}).",
              f"Binding-direction flow after contingency {summary['base_post_mw']:.0f} MW; with all significant "
              f"outages {summary['post_all_mw']:.0f} MW. + loads the constraint.", ""]
    shown = _link_outages(_short(impacts.head(top), "name"), market)
    shown["start"], shown["end"] = shown["start"].map(_fmt_dt), shown["end"].map(_fmt_dt)
    cols = ["market", "outage_equipment_id", "name", "in_active", "request_status", "start", "end", "pre_ctg_lodf_pct",
            "individual_mw"] + [c for c in ("individual_pct",) if c in shown] + ["in_combination_mw"] + \
           [c for c in ("in_combination_pct",) if c in shown] + ["check"]
    lines += _tsv(shown, cols, decimals=1)
    return "\n".join(lines + _warnings_section(warnings))


@server.tool(annotations=READ_ONLY)
@_readable_errors
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

    lines = [f"=== {market.upper()} {kind.upper()} {_constraint_title(market, constraint_id, name, me_id, day)} on {day} ==="]
    lines += _linked_elements_line(eng, market, me_id)
    lines += ["", "--- Hourly shadow prices ---"]
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



def _equipment_label(market: str, equipment_id: str, names: dict) -> str:
    name = names.get(str(equipment_id), "")
    return links.md(f"{equipment_id} {name}".strip(), links.outage(market, equipment_id))


def _group_label(market: str, group_id: str) -> str:
    return links.md(f"GID {group_id}", links.outage_group(market, group_id))


def _resolve_groups(eng, market: str, outage_id: str, id_type: str) -> tuple[str, list[str]]:
    """(cleaned ID, the groups it starts from): the equipment's groups, or the group itself."""
    cleaned = osearch.clean_id(outage_id)
    if not cleaned:
        raise ValueError(f"'{outage_id}' is not an equipment or group ID.")
    if id_type == "equipment":
        return cleaned, outage_groups.groups_for_equipment(eng, market, cleaned)
    return cleaned, [cleaned]


@server.tool(annotations=READ_ONLY)
@_readable_errors
def get_outage_group(market: str, outage_id: str, id_type: str = "equipment", include_removed: bool = False) -> str:
    """An outage's equipment groups and every member of them, for any equipment or group ID
    (not only outages in effect on some day).

    For an equipment ID: the groups it belongs to. For a group ID: that group. Either way each
    group's associated groups (which the outage search also follows) and the full member list,
    with names, are listed. MISO desk notes name outages by group ("GID 2972"); PJM has few groups.

    Args:
        market: Market the outage is in: miso, spp, pjm, ercot, or caiso.
        outage_id: One outage equipment ID or group ID.
        id_type: equipment or group.
        include_removed: Also list members marked removed (EnergyCore ignores them).
    """
    _check_choice("market", market, osearch.MARKETS)
    _check_choice("id_type", id_type, ["equipment", "group"])
    eng = engine()
    warnings = []
    cleaned, start_groups = _resolve_groups(eng, market, outage_id, id_type)

    lines = [f"Market: {market.upper()}"]
    if id_type == "equipment":
        names = outage_groups.equipment_names(eng, market, [cleaned], warn=warnings.append)
        lines.append(f"Outage equipment: {_equipment_label(market, cleaned, names)}")
        if not start_groups:
            return "\n".join(lines + ["", "This equipment is not a member of any outage group."]
                             + _warnings_section(warnings))
        lines.append("Member of: " + ", ".join(_group_label(market, g) for g in start_groups))

    assoc = outage_groups.associations(eng, market, start_groups)
    forward = assoc[assoc["group_id"].isin(start_groups)]
    backward = assoc[assoc["associated_group_id"].isin(start_groups) & ~assoc["group_id"].isin(start_groups)]
    all_groups = list(dict.fromkeys(start_groups + forward["associated_group_id"].tolist()
                                    + backward["group_id"].tolist()))

    info = outage_groups.groups(eng, market, all_groups)
    if info.empty:
        return "\n".join(lines + ["", f"No {market.upper()} outage group {cleaned}."] + _warnings_section(warnings))
    mem = outage_groups.members(eng, market, all_groups, include_removed=include_removed)
    names = outage_groups.equipment_names(eng, market, mem["equipment_id"].tolist()
                                          + info["basis_outage_id"].dropna().astype(str).tolist(), warn=warnings.append)

    for g in all_groups:
        row = info[info["group_id"] == g]
        if row.empty:
            continue
        r = row.iloc[0]
        how = ("starting group" if g in start_groups
               else "associated (followed by the outage search)" if g in forward["associated_group_id"].values
               else "has this group as an associated group (not followed by the outage search)")
        lines += ["", f"=== {_group_label(market, g)} {r['equipment_name'] or ''} ({how}) ==="]
        details = [f"{k} {r[c]}" for k, c in (("type", "likely_equipment_type"), ("from", "likely_from_station"),
                                              ("to", "likely_to_station"), ("kV", "kv"))
                   if r[c] is not None and not pd.isna(r[c]) and str(r[c]).strip()]
        if r["basis_outage_id"] is not None and not pd.isna(r["basis_outage_id"]):
            details.append("basis outage " + _equipment_label(market, str(int(r["basis_outage_id"])), names))
        if r["manual"]:
            details.append("manual")
        if details:
            lines.append("; ".join(details))
        gm = mem[mem["group_id"] == g].copy()
        lines.append(f"Members: {len(gm)}")
        if not gm.empty:
            gm["equipment"] = [_equipment_label(market, e, names) for e in gm["equipment_id"]]
            cols = ["equipment_id", "equipment", "score", "manual"] + (["removed"] if include_removed else [])
            lines += _tsv(gm, cols, decimals=2)

    every = sorted(set(mem["equipment_id"]), key=lambda x: int(x) if x.isdigit() else 0)
    lines += ["", f"All member equipment IDs ({len(every)}): " + ", ".join(every)]
    return "\n".join(lines + _warnings_section(warnings))


@server.tool(annotations=READ_ONLY)
@_readable_errors
def get_outage_quicklinks(market: str, outage_id: str, id_type: str = "equipment", include_groups: bool = True,
                          max_links: int = 150) -> str:
    """Monitored elements the desk quicklinked to an outage: its whole quicklink history, for
    every outage request, from elements in any market (a MISO outage is often quicklinked from
    SPP or PJM elements).

    Args:
        market: Market the outage is in: miso, spp, pjm, ercot, or caiso.
        outage_id: One outage equipment ID or group ID.
        id_type: equipment or group.
        include_groups: For an equipment ID, also the links to its groups and their associated
            groups, and for a group ID, also the links to its associated groups and to every
            member's equipment. Links are often made to the group rather than the equipment.
        max_links: Most individual quicklinks to list (newest first). 0 lists all of them. The
            per-element summary always covers every link.

    Returns one row per quicklink and a summary per monitored element. Follow up with
    get_monitored_element_notes for the elements' note history.
    """
    _check_choice("market", market, osearch.MARKETS)
    _check_choice("id_type", id_type, ["equipment", "group"])
    eng = engine()
    warnings = []
    cleaned, start_groups = _resolve_groups(eng, market, outage_id, id_type)

    equipment_ids, group_ids = ([cleaned], []) if id_type == "equipment" else ([], [cleaned])
    if include_groups:
        assoc = outage_groups.associations(eng, market, start_groups)
        group_ids = list(dict.fromkeys(start_groups + assoc[assoc["group_id"].isin(start_groups)]["associated_group_id"].tolist()))
        if id_type == "group":
            equipment_ids = outage_groups.members(eng, market, group_ids)["equipment_id"].unique().tolist()

    links_df = outage_groups.quicklinks(eng, market, equipment_ids, group_ids, warn=warnings.append)
    names = outage_groups.equipment_names(eng, market, equipment_ids or [cleaned], warn=warnings.append)

    target = (_equipment_label(market, cleaned, names) if id_type == "equipment" else _group_label(market, cleaned))
    lines = [f"Market: {market.upper()}", f"Outage: {target}"]
    if include_groups:
        lines.append(f"Searched: {len(equipment_ids)} equipment ID(s)"
                     + (f", groups {', '.join(_group_label(market, g) for g in group_ids)}" if group_ids else ", no groups"))
    if links_df.empty:
        return "\n".join(lines + ["", "No quicklinks."] + _warnings_section(warnings))

    me_names = {}
    for mkt, ids in links_df.groupby("me_market")["me_id"]:
        me_names.update({(mkt, k): v for k, v in monitored_elements.names(eng, mkt, ids).items()})
    links_df["element"] = [links.md(f"{m}:{i} {me_names.get((m, int(i)), '')}".strip(), links.monitored_element(m, i))
                           for m, i in zip(links_df["me_market"], links_df["me_id"])]
    links_df["me"] = [f"{m}:{int(i)}" for m, i in zip(links_df["me_market"], links_df["me_id"])]
    links_df["outage"] = [o if k == "equipment" else f"GID {o}" for k, o in zip(links_df["outage_kind"], links_df["outage_id"])]
    links_df["created"] = links_df["created_at"].map(_fmt_dt)

    summary = (links_df.groupby(["me_market", "me_id", "element"], sort=False)
               .agg(links=("outage", "size"), first_linked=("created_at", "min"), last_linked=("created_at", "max"))
               .reset_index().sort_values(["links", "last_linked"], ascending=False))
    summary["first_linked"] = summary["first_linked"].map(_fmt_dt)
    summary["last_linked"] = summary["last_linked"].map(_fmt_dt)
    lines += ["", f"--- Monitored elements ({len(summary)}) ---"] + _tsv(summary, ["element", "links", "first_linked", "last_linked"])
    shown = links_df.head(max_links) if max_links else links_df
    lines += ["", f"--- Quicklinks ({len(links_df)}, newest first) ---"] + _tsv(shown, ["me", "outage", "request_id", "created"])
    if len(shown) < len(links_df):
        lines.append(f"({len(links_df) - len(shown)} older quicklinks omitted; raise max_links or set it to 0 to see them.)")
    for mkt, ids in summary.groupby("me_market", sort=False)["me_id"]:
        lines.append(f"{mkt.upper()} monitored element IDs: " + ", ".join(str(int(i)) for i in ids))
    return "\n".join(lines + _warnings_section(warnings))


@server.tool(annotations=READ_ONLY)
@_readable_errors
def get_monitored_element_notes(market: str, me_ids: list[int], lookback: str = "5 Years", start_date: str | None = None,
                                end_date: str | None = None, context: str = "All", max_notes: int = 500) -> str:
    """Every desk quick note on one or more monitored elements over a date range, newest first,
    without deleted notes. Unlike get_outage_documentation it does not search note text, so it
    finds notes that never name the outage.

    Args:
        market: Market of the monitored elements: miso, spp, pjm, ercot, or caiso.
        me_ids: Monitored element IDs, for example the ones get_outage_quicklinks lists.
        lookback: 1 Month, 6 Months, 1 Year, 3 Years, 5 Years, or All Time. Ignored when start_date is given.
        start_date: First day, YYYY-MM-DD.
        end_date: Last day, YYYY-MM-DD. Default: today.
        context: All, rt_shadow, rt_shadow_forecast, or da_shadow.
        max_notes: Most notes to show in all (newest first). 0 shows all of them.
    """
    _check_choice("market", market, osearch.MARKETS)
    _check_choice("lookback", lookback, osearch.LOOKBACK_OPTIONS)
    _check_choice("context", context, osearch.CONTEXT_OPTIONS)
    if not me_ids:
        raise ValueError("Give at least one monitored element ID.")
    eng = engine()
    if start_date:
        start = datetime.date.fromisoformat(start_date).isoformat()
    else:
        s = osearch.lookback_start_date(lookback, pd.Timestamp.now())
        start = s.strftime("%Y-%m-%d") if s is not None else None
    end = datetime.date.fromisoformat(end_date).isoformat() if end_date else None

    notes = outage_groups.element_notes(eng, market, me_ids, start, end, context)
    names = monitored_elements.names(eng, market, me_ids)
    lines = [f"Market: {market.upper()}", f"Dates: {start or 'all time'} to {end or 'today'}; context {context}"]
    counts = notes.groupby("me_id").size() if not notes.empty else pd.Series(dtype=int)
    lines += ["", "--- Elements ---"]
    for i in sorted({int(x) for x in me_ids}):
        label = links.md(f"{i} {names.get(i, '')}".strip(), links.monitored_element(market, i))
        lines.append(f"{label}: {int(counts.get(i, 0))} notes")
    total = len(notes)
    if max_notes:
        notes = notes.head(max_notes)
    lines += ["", f"--- Notes ({total}, newest first) ---"]
    if notes.empty:
        lines.append("No notes.")
    for n in notes.itertuples():
        body = osearch.clean_body_text_for_raw(n.body)
        lines.append(f"{n.dt:%Y-%m-%d} ME {n.me_id} {n.context}: {body}")
    if len(notes) < total:
        lines.append(f"({total - len(notes)} older notes omitted; raise max_notes or set it to 0 to see them.)")
    return "\n".join(lines)


@server.tool(annotations=READ_ONLY)
@_readable_errors
def get_constraint_factors(market: str, constraint_id: int, top_n: int = 100, node_types: list[str] | None = None,
                           zones: list[str] | None = None, collapse_ties: bool = False) -> str:
    """A constraint's promoted shift factors: the ones EnergyCore uses (in the shadow-based
    nodal congestion forecasts, congestion = factor x shadow price). Lists the most positive
    and most negative pnodes and which factor source won.

    EnergyCore picks one source per constraint (in PJM: trader-frozen, dependency-recent,
    rt-matrix-recent, dependency, rt-matrix, trader-csv, trader-copy, auto-copy, da-matrix,
    tlr). auto-copy and trader-copy borrow another constraint's factors; the copied-from
    constraint and its original source are shown.

    Args:
        market: miso, spp, pjm, ercot, or caiso.
        constraint_id: Official constraint ID.
        top_n: How many of the most positive and of the most negative factors to list.
        node_types: Only these pnode types, for example ["aggregate", "interface"] or ["GEN"].
        zones: Only pnodes in these zones (PJM, SPP; MISO pnodes have no zone).
        collapse_ties: One row per distinct factor value with a count and example pnodes.
            Equivalent buses at a station share a factor, so a plain list can be one station
            over and over.
    """
    _check_choice("market", market, osearch.MARKETS)
    if top_n < 1:
        raise ValueError("top_n must be at least 1.")
    eng = engine()
    name, me_id, me_name = factors.constraint_name(eng, market, constraint_id)
    today = datetime.date.today()
    title = links.md(f"{constraint_id} {name}".strip(), links.constraint(market, constraint_id, today - datetime.timedelta(days=30), today))
    lines = [f"=== {market.upper()} constraint {title} ==="]
    if me_id is not None:
        lines.append("Monitored element: " + links.md(f"{me_id} {me_name}".strip(), links.monitored_element(market, me_id)))

    m = factors.master(eng, market, constraint_id)
    if m is None:
        return "\n".join(lines + ["", "This constraint has no promoted factors."])
    source = m["source_type"] or "?"
    if m["aggregate_type"]:
        source += f" (original source {m['aggregate_type']})"
    lines.append(f"Factor source: {source}")
    if m["source_official_constraint_id"] and m["source_official_constraint_id"] != constraint_id:
        src_name, _, _ = factors.constraint_name(eng, market, m["source_official_constraint_id"])
        src = m["source_official_constraint_id"]
        lines.append("Copied from constraint: " + links.md(f"{src} {src_name}".strip(),
                                                          links.constraint(market, src, today - datetime.timedelta(days=30), today)))
    stamps = [f"{label} {_fmt_dt(m[c])}" for label, c in (("reading", "reading_time"), ("solve from", "solve_start_dt"),
                                                          ("to", "solve_stop_dt"), ("updated", "updated_at"),
                                                          ("expires", "expires_at"), ("frozen until", "frozen_until"))
              if m[c] is not None and not pd.isna(m[c])]
    if stamps:
        lines.append("Times: " + "; ".join(stamps))
    if (str(m["source_type"]).endswith("-recent") and m["expires_at"] is not None and not pd.isna(m["expires_at"])
            and m["expires_at"] <= datetime.datetime.now()):
        lines.append(f"Note: past expires_at, so EnergyCore's next aggregation relabels the source "
                     f"{str(m['source_type']).removesuffix('-recent')}; the factors stay the same.")
    flags = [f for f, c in (("temporary", "temporary"), ("manual", "manual")) if m[c]]
    if flags:
        lines.append("Flags: " + ", ".join(flags))

    df = factors.details(eng, market, constraint_id)
    total = len(df)
    df = factors.filter_details(df, node_types, zones)
    lines.append(f"Pnodes with factors: {total}" + (f" ({len(df)} after the node type and zone filters)" if len(df) != total else "")
                 + f"; positive {int((df['factor'] > 0).sum())}, negative {int((df['factor'] < 0).sum())}, "
                   f"zero {int((df['factor'] == 0).sum())}")
    if df.empty:
        return "\n".join(lines)
    pos, neg = factors.extremes(df, top_n, collapse_ties=collapse_ties)
    cols = ["factor", "pnodes", "examples", "node_types", "zones"] if collapse_ties else ["pnode_id", "pnode", "node_type", "zone", "factor"]
    if not df["zone"].astype(bool).any():
        cols.remove("zones" if collapse_ties else "zone")
    unit = "factor values" if collapse_ties else "pnodes"

    def fmt(side: pd.DataFrame) -> pd.DataFrame:
        side = side.copy()
        side["factor"] = side["factor"].map(lambda f: f"{f:.5f}")
        return side

    lines += ["", f"--- Most positive ({len(pos)} {unit}) ---"] + (_tsv(fmt(pos), cols) if not pos.empty else ["None."])
    lines += ["", f"--- Most negative ({len(neg)} {unit}) ---"] + (_tsv(fmt(neg), cols) if not neg.empty else ["None."])
    return "\n".join(lines)


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
    threading.Thread(target=_preload_notes, name="preload-notes", daemon=True).start()
    server.run("stdio")
