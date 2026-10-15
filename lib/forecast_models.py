"""The regression models behind a constraint's shadow price forecast: EnergyCore's model groups.

Used by ``mcp_server.py``. Like ``lib/constraint_drivers.py``, nothing here imports
Streamlit, and functions that can hit a recoverable database error take a ``warn`` callable.

Each forecast day, a constraint belongs to at most one model group
(``{market}_constraints_regression_model_groups`` -> ``{market}_regression_model_groups``,
one group per ``forecast_dt``). A group runs several models
(``{market}_shadow_model_outputs.shadow_model_group_id``), each a regression of the
constraint's historical shadow prices on a set of features (``regression_dimensions`` in
its ``parameters``), trained on days chosen by the date window and, often, an outage
filter such as ``miso:act:g:10642 and not miso:act:g:485`` (hours when those outage
groups were active). Fit statistics are in ``{market}_shadow_model_output_stats``.

How a group's models become the forecast (checked for MISO constraint 7388 in October 2026):

- ``simple`` mode: the hourly forecast is the sum of simple_weight * scaling_factor * the
  model's hourly output.
- ``shaped`` mode: the daily total is the sum of total_weight * scaling_factor * the model's
  daily total, and shaping_weight picks the models that set the hourly shape.

The group's allocation splits its forecast across the constraints in it. An applied group
writes its forecast into the forecast table's manual column, so "manual" forecasts are
mostly applied models. Traders can still edit after applying (``applied_unlocked``), so
the forecast table can differ from the models.

``parameters`` and ``output`` are Ruby-serialized YAML: ``:symbol`` keys and list-valued
keys (``? - 7388 - 1``), which ``load_yaml`` handles.
"""

import datetime
import re

import numpy as np
import pandas as pd
import yaml
from sqlalchemy import text

from lib import outage_search as osearch

MODEL_SHORT_NAMES = {"linear_model": "linear", "loess_model": "loess"}

# Spec fields that never say anything about what a feature is.
_BOILERPLATE = {"label", "alias", "type_str", "sub_reports", "explicitly_include_constraints_csv", "constraint_name",
                "coefficient_threshold_min", "coefficient_threshold_max"}

_FILTER_TOKEN = re.compile(r"(\w+):(\w+):(\w+):(\d+)")


def _no_warn(_message):
    pass


class _RubyYamlLoader(yaml.SafeLoader):
    """SafeLoader that accepts list-valued mapping keys (as tuples) and ignores ``!ruby/`` tags."""


def _construct_mapping(loader, node, deep=False):
    out = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        out[tuple(key) if isinstance(key, list) else key] = loader.construct_object(value_node, deep=True)
    return out


def _construct_ruby(loader, _suffix, node):
    if isinstance(node, yaml.MappingNode):
        return _construct_mapping(loader, node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_scalar(node)


_RubyYamlLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)
_RubyYamlLoader.add_multi_constructor("!ruby/", _construct_ruby)


def load_yaml(raw):
    """Parses EnergyCore's Ruby YAML; ``:symbol`` keys lose their colon. None for empty input."""
    if not raw:
        return None
    return _strip_symbols(yaml.load(raw, Loader=_RubyYamlLoader))


def _strip_symbols(value):
    if isinstance(value, dict):
        return {(k[1:] if isinstance(k, str) and k.startswith(":") else k): _strip_symbols(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_symbols(v) for v in value]
    return value


def _check_market(market: str) -> None:
    if market not in osearch.MARKETS:
        raise ValueError(f"Unknown market '{market}'.")


# --- GROUPS AND MODELS ---

def find_group(engine, market: str, constraint_id: int, day: datetime.date) -> tuple[dict | None, str | None]:
    """The model group forecasting ``constraint_id`` on ``day``, as (group, note).

    When the constraint ID has no group, falls back to a group for another constraint on the
    same monitored element that day (IDs churn), and the note says so.
    """
    _check_market(market)
    query = f"""
        SELECT g.id, g.name, g.mode, g.group_state, g.early, g.forecast_dt, g.estimated_proposed_forecast,
               g.forecast_ceiling, g.forecast_floor, g.forecast_hourly_scaling_factors, g.constraint_allocation,
               g.constraint_allocation_mode, g.default_forecast, g.custom_forecast, g.created_at, g.updated_at,
               c.official_constraint_id AS group_constraint_id
        FROM {market}_constraints_regression_model_groups c
        JOIN {market}_regression_model_groups g ON g.id = c.regression_model_group_id
        WHERE g.forecast_dt = :day AND c.official_constraint_id IN ({{ids}})
    """
    with engine.connect() as conn:
        rows = conn.execute(text(query.format(ids=":c")), {"day": day, "c": constraint_id}).mappings().fetchall()
        note = None
        if not rows:
            siblings = [r[0] for r in conn.execute(text(f"""
                SELECT oc.id FROM {market}_official_constraints oc
                JOIN {market}_official_constraints me ON me.official_monitored_element_id = oc.official_monitored_element_id
                WHERE me.id = :c AND oc.id <> :c
            """), {"c": constraint_id}).fetchall()]
            if siblings:
                rows = conn.execute(text(query.format(ids=", ".join(str(int(s)) for s in siblings))),
                                    {"day": day}).mappings().fetchall()
                if rows:
                    note = (f"Constraint {constraint_id} has no model group on {day}; showing the group for constraint "
                            f"{rows[0]['group_constraint_id']} on the same monitored element.")
    if not rows:
        return None, None
    group = dict(rows[0])
    for col in ("forecast_hourly_scaling_factors", "constraint_allocation", "default_forecast", "custom_forecast"):
        group[col] = load_yaml(group[col])
    return group, note


def group_models(engine, market: str, group_id: int) -> pd.DataFrame:
    """The group's models with parsed parameters and output, and fit statistics as columns."""
    _check_market(market)
    with engine.connect() as conn:
        models = pd.read_sql(text(f"""
            SELECT id, name, model_state, date_type, start_at, end_at, scaling_factor, total_weight, shaping_weight,
                   simple_weight, sort_order, outage_equipment_filter, parameters, output, dates, generated_at
            FROM {market}_shadow_model_outputs
            WHERE shadow_model_group_id = :g
            ORDER BY COALESCE(sort_order, 0), id
        """), conn, params={"g": group_id})
        if models.empty:
            return models
        stats = pd.read_sql(text(f"""
            SELECT shadow_model_output_id AS id, name, val
            FROM {market}_shadow_model_output_stats
            WHERE shadow_model_output_id IN ({', '.join(str(int(i)) for i in models['id'])})
        """), conn)
    if not stats.empty:
        models = models.merge(stats.pivot_table(index="id", columns="name", values="val").reset_index(), on="id",
                              how="left")
    models["parameters"] = models["parameters"].map(load_yaml)
    models["output"] = models["output"].map(load_yaml)
    models["spells"] = models["dates"].map(lambda d: training_spells(load_yaml(d)))
    models["training_hours"] = models["spells"].map(lambda sp: sum(h for _, _, h in sp))
    for col in ("scaling_factor", "total_weight", "shaping_weight", "simple_weight"):
        models[col] = models[col].fillna(0.0)
    models["regression"] = models["parameters"].map(
        lambda p: MODEL_SHORT_NAMES.get((p or {}).get("regression_model_str"), (p or {}).get("regression_model_str")))
    models["weighted"] = models[["total_weight", "shaping_weight", "simple_weight"]].sum(axis=1) > 0
    return models.drop(columns=["dates"])


def training_spells(dates) -> list[tuple[str, str, int]]:
    """The ``dates`` column as [(start, end, hours)]: it lists the start and end hour of each
    spell of training data (when the outage filter matched, within the date window)."""
    points = []
    for d in dates or []:
        try:
            points.append(datetime.datetime.fromisoformat(str(d["date"])) + datetime.timedelta(hours=int(d["hour"]) - 1))
        except (KeyError, TypeError, ValueError):
            continue
    spells = []
    for start, end in zip(points[0::2], points[1::2]):
        spells.append((f"{start:%Y-%m-%d} HE{start.hour + 1}", f"{end:%Y-%m-%d} HE{end.hour + 1}",
                       int((end - start).total_seconds() // 3600) + 1))
    return spells


def hourly_output(output, constraint_id: int) -> np.ndarray | None:
    """A model's (or group default's) 24 hourly values for one constraint; None if it has none."""
    hours = (output or {}).get("forecast") or []
    if not hours:
        return None
    values = [(h or {}).get(constraint_id, (h or {}).get(str(constraint_id))) for h in hours]
    if all(v is None for v in values):
        return None
    return np.array([float(v) if v is not None else 0.0 for v in values])


def implied_forecast(group: dict, models: pd.DataFrame, constraint_id: int) -> tuple[float | None, np.ndarray | None]:
    """(daily total, hourly values or None) the weights imply for one constraint. Hourly values
    only for simple mode, where the group's hourly scaling factors also apply; shaped mode's
    hourly split is not reproduced here."""
    outputs = {r.id: hourly_output(r.output, constraint_id) for r in models.itertuples()}
    if group["mode"] == "simple":
        hourly = sum((r.simple_weight * r.scaling_factor * outputs[r.id] for r in models.itertuples()
                      if r.simple_weight and outputs[r.id] is not None), np.zeros(24))
        scaling = group.get("forecast_hourly_scaling_factors") or []
        if len(scaling) == len(hourly):
            hourly = hourly * np.array([float(s) for s in scaling])
        return float(hourly.sum()), hourly
    total = sum(r.total_weight * r.scaling_factor * outputs[r.id].sum() for r in models.itertuples()
                if r.total_weight and outputs[r.id] is not None)
    return float(total), None


# --- FEATURES ---

def _fmt_value(value) -> str:
    if isinstance(value, list):
        return "/".join(str(v) for v in value)
    return str(value)


MAX_SPEC_CHARS = 300

# Settings that are the default or say nothing about which series a feature is.
_DEFAULT_SETTINGS = {
    ("zonal_coefficient_type", "solar/wind/load"), ("autoflow_leverage_side", "high/low"),
    ("cancel_both_side_generators", "1"), ("included_generators", "all"), ("leverage_threshold", "0.01"),
}
# Attributes that are just the series' natural unit.
_DEFAULT_ATTRIBUTES = {"power_mw", "load_mwh", "autoflow", "leverage_val", "avg_output_mw", "mw", "val", "forecast"}
# Prefixes dropped from setting names and values, to shorten them.
_KEY_PREFIXES = ("meteologica_", "maxar_", "hourly_load_", "windgen_", "timeseries_", "crcl_v3_", "dummy_")
_VALUE_PREFIXES = ("Meteologica_", "Meteologica ")
_TYPE_NAMES = {"constraint_generator_leverage": "gen_leverage"}


def _leaf(spec: dict) -> tuple[str, dict]:
    """(type, {setting: value}) for a series spec, with boilerplate and defaults dropped."""
    kind = str(spec.get("type_str", "")).replace("timeseries__", "")
    settings = {}
    for key, value in spec.items():
        if key in _BOILERPLATE or value in (None, "", [], {}):
            continue
        value = _fmt_value(value)
        if (key.startswith("include_") and value == "0") or (key, value) in _DEFAULT_SETTINGS:
            continue
        if key == "attributes" and value in _DEFAULT_ATTRIBUTES:
            continue
        # tios_rt_energy_forecasts(tios_rt_energy_forecast_name=...) -> (name=...)
        for prefix in (kind.rstrip("s") + "_",) + _KEY_PREFIXES:
            key = key[len(prefix):] if key.startswith(prefix) else key
        for prefix in _VALUE_PREFIXES:
            value = value.replace(prefix, "")
        settings[key] = value
    # An ID next to its name adds nothing: location_name=Memphis, location_id=17.
    for key in [k for k in settings if k.endswith("_id") and k[:-3] + "_name" in settings]:
        del settings[key]
    return _TYPE_NAMES.get(kind, kind), settings


def _merge_leaves(leaves: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    """Merges sibling series that differ in one setting: gen_leverage(pref=meteo), gen_leverage(pref=crcl)
    becomes gen_leverage(pref=meteo|crcl)."""
    merged = []
    for kind, settings in leaves:
        for i, (m_kind, m_settings) in enumerate(merged):
            if m_kind != kind or m_settings.keys() != settings.keys():
                continue
            diff = [k for k in settings if settings[k] != m_settings[k]]
            if len(diff) == 1:
                k = diff[0]
                values = m_settings[k].split("|")
                merged[i] = (kind, {**m_settings, k: "|".join(values + [settings[k]])})
                break
        else:
            merged.append((kind, settings))
    return merged


def _render_leaf(kind: str, settings: dict) -> str:
    return kind + (f"({', '.join(f'{k}={v}' for k, v in settings.items())})" if settings else "")


def compact_spec(spec, expand_custom: bool = False, nested: bool = False) -> str:
    """A feature spec as a short formula: delta as a - b, sum as a + b, product as a * b,
    copy as "same". Named custom functions show only their name unless expand_custom."""
    if not isinstance(spec, dict):
        return str(spec)
    kind = str(spec.get("type_str", ""))
    subs = [s for _, s in sorted((spec.get("sub_reports") or {}).items(), key=lambda kv: str(kv[0]))]
    if not kind.startswith("timeseries__functions__"):
        return _render_leaf(*_leaf(spec))

    fn = kind.replace("timeseries__functions__", "")
    if fn == "copy":
        return "same"
    if fn == "constant":
        return str(spec.get("constant_value", ""))
    if fn == "custom":
        name = str(spec.get("custom", "custom"))
        if not expand_custom or not subs:
            return f"«{name}»"
        return f"«{name}» = " + compact_spec(subs[0], expand_custom, nested=True)

    leaves = [s for s in subs if not str(s.get("type_str", "")).startswith("timeseries__functions__")]
    others = [s for s in subs if s not in leaves]
    parts = [_render_leaf(*leaf) for leaf in _merge_leaves([_leaf(s) for s in leaves])]
    parts += [compact_spec(s, expand_custom, nested=True) for s in others]
    joiner = {"sum": " + ", "delta": " - ", "product": " * "}.get(fn)
    if joiner:
        text_ = joiner.join(parts) if parts else fn
        return f"({text_})" if nested and len(parts) > 1 else text_
    return f"{fn}({', '.join(parts)})"


def _cap(text_: str) -> str:
    return text_ if len(text_) <= MAX_SPEC_CHARS else text_[:MAX_SPEC_CHARS] + "..."


def features(parameters: dict, expand_custom: bool = False) -> list[dict]:
    """The model's features: [{name, historical, forecast}], historical being what it was
    trained on and forecast what it is fed for the forecast day ("same" when it is the same
    series), both as compact formulas."""
    params = parameters or {}
    names = params.get("dimension_names") or []
    dims = params.get("regression_dimensions") or {}
    out = []
    for i, (_, dim) in enumerate(sorted(dims.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 0)):
        out.append({"name": names[i] if i < len(names) else "",
                    "historical": _cap(compact_spec((dim or {}).get("historical"), expand_custom)),
                    "forecast": _cap(compact_spec((dim or {}).get("forecast"), expand_custom))})
    return out


def feature_lines(feats: list[dict], with_fed: bool = True) -> list[str]:
    """One string per feature, except that outage on/off dummies (trained on active outages,
    fed planned ones) collapse into a single "outage dummies: id, id, ..." entry."""
    out, dummies = [], []
    for f in feats:
        match = re.fullmatch(r"dummy_active_outages\(outage_equipment_id=(\w+)[^)]*\)", f["historical"])
        if match and (not f["forecast"].startswith("dummy_") or "planned" in f["forecast"] or f["forecast"] == "same"):
            dummies.append(match.group(1))
            continue
        out.append(f["historical"] + ("" if not with_fed or f["forecast"] == "same" else f"; fed {f['forecast']}"))
    if dummies:
        out.append("outage dummies (1 while out; fed planned outages): " + ", ".join(dummies))
    return out


def training_setup(parameters: dict) -> list[str]:
    """Short lines on what the model was trained on, besides its features and outage filter."""
    p = parameters or {}
    lines = []
    target = f"target constraint {p.get('constraint_id')}"
    included = [k.replace("include_", "").replace("_constraints", "") for k in
                ("include_matchmonelem_constraints", "include_matchrtname_constraints",
                 "include_temprtname_constraints", "include_family_constraints") if str(p.get(k)) == "1"]
    if included:
        target += " plus " + ", ".join(included) + " constraints"
    if p.get("explicitly_include_constraints_csv"):
        target += f" plus {p['explicitly_include_constraints_csv']}"
    lines.append(target)
    hours = str(p.get("hours_subset_csv") or "")
    if hours and hours != ",".join(str(h) for h in range(1, 25)):
        lines.append(f"hours {hours}")
    extras = [f"{k}={_fmt_value(p[k])}" for k in ("hour_grouping", "span", "y_intercept", "time_weighting_curve")
              if p.get(k) not in (None, "")]
    if p.get("conditional_filter_names"):
        extras.append("conditional filters: " + ", ".join(map(str, p["conditional_filter_names"])))
    return lines + extras


def outage_condition(parameters: dict, filter_text: str | None) -> tuple:
    """What outages a model's training hours required, as (filter text, [equipment IDs],
    operator, [excluded equipment IDs], excluded operator). Markets use either the filter
    text (``miso:act:g:10642 and ...``) or equipment ID lists (PJM)."""
    p = parameters or {}
    ids = [i.strip() for i in str(p.get("outage_equipment_id") or "").split(",") if i.strip()]
    not_ids = [i.strip() for i in str(p.get("not_outage_equipment_id") or "").split(",") if i.strip()]
    return ((filter_text or "").strip(), tuple(ids), p.get("outage_equipment_operator") or "AND", tuple(not_ids),
            p.get("not_outage_equipment_operator") or "AND")


def outage_filter_groups(engine, filter_text: str, warn=_no_warn) -> dict[tuple[str, str], str]:
    """Names for the outage groups in an outage filter, as {(market, group_id): name}."""
    tokens = _FILTER_TOKEN.findall(filter_text or "")
    names = {}
    by_market = {}
    for mkt, _state, kind, gid in tokens:
        if kind == "g" and mkt in osearch.MARKETS:
            by_market.setdefault(mkt, set()).add(gid)
    for mkt, gids in by_market.items():
        try:
            with engine.connect() as conn:
                for gid, name, kv, frm, to in conn.execute(text(f"""
                    SELECT id, equipment_name, likely_from_kv, likely_from_station, likely_to_station
                    FROM {mkt}_outage_equipment_groups WHERE id IN ({', '.join(sorted(gids))})
                """)).fetchall():
                    stations = "-".join(s for s in (frm, to) if s)
                    names[(mkt, str(gid))] = " ".join(x for x in (f"{kv} kV" if kv else "", stations or name) if x)
        except Exception as e:
            warn(f"Could not look up {mkt.upper()} outage groups for an outage filter: {e}")
    return names
