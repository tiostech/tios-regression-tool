"""Monitored element details: names, stations, the other direction, the desk's description,
and the links between elements that descriptions hold.

Used by ``mcp_server.py``. Like ``lib/constraint_drivers.py``, nothing here imports Streamlit.

The description at the top of an element's EnergyCore page is ``{market}_monelem_meta.description``.
The desk pastes links to related elements into it, most often the same line in another market
(MISO ME 834 Lime Creek-Barton -> SPP ME 1376, which SPP names TMP421_29021, so names alone
cannot pair them), but also the flipped direction or elements that bind together. Links are
not always reciprocal, so ``link_graph`` reads every market's descriptions and records each
link both ways.
"""

import re

import pandas as pd
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from lib import outage_search as osearch

_ME_LINK = re.compile(r"https?://energycore\.tioscapital\.com/(\w+)/monitored_elements/(\d+)")
# Markdown link markup around a URL, and leftover punctuation, when a link's line becomes its context.
_MD_LINK = re.compile(r"\[([^\]]*)\]\(\s*https?://[^)]*\)")

MAX_CONTEXT_CHARS = 80


def _no_warn(_message):
    pass


def _context(line: str) -> str:
    """The text on a link's line without the URLs: 'SPP Flipped (low North wind)', 'Combined with Glenworth Hayward'."""
    line = _MD_LINK.sub(r"\1", line)
    line = re.sub(r"https?://\S+", " ", line)
    line = re.sub(r"\s+", " ", line).strip(" -:|,;")
    return line[:MAX_CONTEXT_CHARS]


def parse_links(description: str) -> list[tuple[str, int, str]]:
    """Element links in a description as [(market, me_id, context)], in order, without repeats."""
    out, seen = [], set()
    for line in re.split(r"\r\n|\n|\r|\\r\\n|\\n|<br\s*/?>", description or ""):
        for mkt, me in _ME_LINK.findall(line):
            key = (mkt.lower(), int(me))
            if key not in seen:
                seen.add(key)
                out.append((key[0], key[1], _context(line)))
    return out


def link_graph(engine, warn=_no_warn) -> dict[tuple[str, int], list[dict]]:
    """Every element link in every market's descriptions, recorded on both ends:
    {(market, me_id): [{market, me_id, context, direction}]}, direction "out" when this
    element's description has the link and "in" when the other element's does."""
    graph = {}

    def add(a, b, context, direction):
        links = graph.setdefault(a, [])
        if not any(l["market"] == b[0] and l["me_id"] == b[1] for l in links):
            links.append({"market": b[0], "me_id": b[1], "context": context, "direction": direction})

    for market in osearch.MARKETS:
        try:
            with engine.connect() as conn:
                rows = conn.execute(text(f"""
                    SELECT official_monitored_element_id, description FROM {market}_monelem_meta
                    WHERE description LIKE '%monitored_elements%'
                """)).fetchall()
        except OperationalError:
            # The database is unreachable (tunnel closed): fail, rather than cache an empty graph.
            raise
        except Exception as e:
            warn(f"Could not read {market.upper()} monitored element descriptions: {e}")
            continue
        for me_id, description in rows:
            for mkt, other, context in parse_links(description):
                if (mkt, other) == (market, int(me_id)):
                    continue
                add((market, int(me_id)), (mkt, other), context, "out")
                add((mkt, other), (market, int(me_id)), context, "in")
    return graph


def cross_market_links(graph: dict, market: str, me_id) -> list[dict]:
    """Links from one element to elements in other markets."""
    if me_id is None or pd.isna(me_id):
        return []
    return [l for l in graph.get((market, int(me_id)), []) if l["market"] != market]


def short_links(graph: dict, market: str, me_id, cross_market_only: bool = False) -> str:
    """'spp:1376, spp:378' for a table cell."""
    if me_id is None or pd.isna(me_id):
        return ""
    links = graph.get((market, int(me_id)), [])
    return ", ".join(f"{l['market']}:{l['me_id']}" for l in links
                     if not cross_market_only or l["market"] != market)


ELEMENT_COLUMNS = ["monitored_element_name", "direction", "non_directional_name", "equipment_type", "is_multi",
                   "from_station", "from_kv", "to_station", "to_kv", "from_area", "to_area", "baa", "last_used"]


def element(engine, market: str, me_id: int) -> dict | None:
    """The element's row from {market}_official_monitored_elements plus its description; None if unknown.

    Only MISO's table has stations, direction, and areas (SPP has the BAA; the rest just a
    name), so missing columns come back as None."""
    with engine.connect() as conn:
        present = {r[0] for r in conn.execute(text(f"SHOW COLUMNS FROM {market}_official_monitored_elements"))}
        cols = ", ".join(f"ome.{c}" if c in present else f"NULL AS {c}" for c in ELEMENT_COLUMNS)
        row = conn.execute(text(f"""
            SELECT ome.id, {cols}, meta.description
            FROM {market}_official_monitored_elements ome
            LEFT JOIN {market}_monelem_meta meta ON meta.official_monitored_element_id = ome.id
            WHERE ome.id = :me
        """), {"me": me_id}).mappings().fetchone()
    return dict(row) if row else None


def names(engine, market: str, me_ids) -> dict[int, str]:
    """{me_id: monitored element name} for elements in one market."""
    ids = sorted({int(i) for i in me_ids if i is not None and not pd.isna(i)})
    if not ids:
        return {}
    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT id, monitored_element_name FROM {market}_official_monitored_elements
            WHERE id IN ({', '.join(str(i) for i in ids)})
        """)).fetchall()
    return {int(i): (n or "").strip() for i, n in rows}


def other_directions(engine, market: str, element_row: dict) -> pd.DataFrame:
    """Elements on the same equipment in the other direction (same non-directional name)."""
    if not element_row.get("non_directional_name"):
        return pd.DataFrame(columns=["me_id", "name", "direction", "last_used"])
    with engine.connect() as conn:
        return pd.read_sql(text(f"""
            SELECT id AS me_id, monitored_element_name AS name, direction, last_used
            FROM {market}_official_monitored_elements
            WHERE non_directional_name = :n AND id <> :me
            ORDER BY last_used DESC
        """), conn, params={"n": element_row["non_directional_name"], "me": element_row["id"]})


def recent_constraints(engine, market: str, me_id: int, limit: int = 10) -> pd.DataFrame:
    """The element's constraints (contingencies), most recently used first."""
    with engine.connect() as conn:
        return pd.read_sql(text(f"""
            SELECT id AS constraint_id, COALESCE(rt_descriptive_name, da_descriptive_name) AS name,
                   contingency_element_name AS contingency, rt_max_limit, last_used
            FROM {market}_official_constraints
            WHERE official_monitored_element_id = :me AND deleted_at IS NULL
            ORDER BY last_used IS NULL, last_used DESC
            LIMIT {int(limit)}
        """), conn, params={"me": me_id})


def short_description(description: str, width: int = 160) -> str:
    """The description on one line, URLs dropped (the links are listed separately), cut to ``width``."""
    flat = osearch.clean_body_text_for_raw(_MD_LINK.sub(r"\1", description or ""))
    flat = re.sub(r"\s+", " ", re.sub(r"https?://\S+", "", flat)).strip(" -")
    return flat if len(flat) <= width else flat[:width - 3] + "..."
