"""Links to EnergyCore pages, so the MCP tools can make constraint, element, and outage names clickable.

URL shapes are the ones the desk already uses in its quick notes. ``md`` wraps a label
as a markdown link; clients like Claude Code and Claude Desktop render those.
"""

import datetime
from urllib.parse import urlencode

import pandas as pd

BASE = "https://energycore.tioscapital.com"


def _valid(value) -> bool:
    return value is not None and not (isinstance(value, float) and pd.isna(value)) and str(value).strip() != ""


def _id(value) -> str:
    return str(int(value)) if isinstance(value, float) else str(value).strip()


def monitored_element(market: str, me_id) -> str | None:
    return f"{BASE}/{market.lower()}/monitored_elements/{_id(me_id)}" if _valid(me_id) else None


def constraint(market: str, constraint_id, start: datetime.date, end: datetime.date | None = None) -> str | None:
    """The constraint's shadow price chart over ``start``..``end``."""
    if not _valid(constraint_id):
        return None
    query = {"commit": 1, "compact_view": 1, "constraint_id": _id(constraint_id),
             "start_date": start.isoformat(), "end_date": (end or start).isoformat()}
    return f"{BASE}/{market.lower()}/data_graphs/timeseries__constraint_shadows?{urlencode(query)}"


def outage(market: str, equipment_id, active: bool = False) -> str | None:
    """Outage search for one equipment ID: planned outages, or the active-outages table."""
    if not _valid(equipment_id):
        return None
    page, sort = ("active_transmission_outages", "actual_start DESC") if active else ("transmission_outages",
                                                                                         "planned_start DESC")
    query = {"commit": 1, "outage_equipment_ids": _id(equipment_id), "sort1": sort}
    return f"{BASE}/{market.lower()}/{page}/search?{urlencode(query)}"


def outage_group(market: str, group_id) -> str | None:
    return f"{BASE}/{market.lower()}/outage_groups/{_id(group_id)}/edit" if _valid(group_id) else None


def model_group(market: str, group_id) -> str | None:
    return f"{BASE}/{market.lower()}/model_groups/{_id(group_id)}" if _valid(group_id) else None


def model_output(market: str, output_id) -> str | None:
    """One model in a model group (a shadow model output)."""
    return f"{BASE}/{market.lower()}/shadow_model_outputs/{_id(output_id)}" if _valid(output_id) else None


def md(label, url: str | None) -> str:
    """``[label](url)``, or the plain label when there is no URL. Brackets in the label are
    swapped for parentheses so they cannot break the link."""
    text = " ".join(str(label if _valid(label) else "").split())
    if not url:
        return text
    return f"[{text.replace('[', '(').replace(']', ')') or url}]({url})"
