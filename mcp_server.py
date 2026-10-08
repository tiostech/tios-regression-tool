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

from lib import db
from lib import outage_search as osearch
from lib import upcoming_outages

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
BRIEFING_PROMPT_PATH = os.path.join(_REPO_ROOT, "prompts", "outage_briefing.md")

# Same lifetime as the page's notes cache.
NOTES_CACHE_SECONDS = 4 * 60 * 60
# Same cap as the page's main search.
NOTE_RESULT_LIMIT = 2000

ID_TYPES = {"equipment": "Equipment ID", "group": "Group ID", "text": "Manual string"}

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                            open_world_hint=False)

server = MCPServer(
    name="energycore-outages",
    instructions=(
        "Read-only access to the trading desk's transmission outage search (tioscore_production). "
        "Use list_upcoming_outages to find outages planned to start on a day, then "
        "get_outage_documentation for the outages that have history. The outage_briefing prompt "
        "holds the desk's report instructions."
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
    return ["", "=== WARNINGS (some data could not be loaded; results may be incomplete) ==="] + warnings


def _fmt_dt(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value is not None else ""


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
