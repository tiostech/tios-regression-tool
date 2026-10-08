"""The PowerWorld network model behind EnergyCore: branches, LODFs, and the mappings from
monitored elements, contingencies, and outage equipment to model branches.

EnergyCore stores each PowerWorld case as a set of versioned tables:

- ``{market}_pw_branches_YYYYMMDD``: every branch, with its base-case flow (``line_mw``,
  positive from bus1 to bus2) and limit
- ``{market}_pw_lodfs_YYYYMMDD``: line outage distribution factors, in percent. Taking
  branch k out moves ``lodf/100 * line_mw(k)`` onto the monitored branch.
- ``{market}_netmap_monitored_elements`` / ``_netmap_outage_elements`` (and, for MISO,
  ``_netmap_contingency_elements``): which model branch each monitored element, outage
  equipment ID, or contingency is, per case ``version_dt``. Rows a trader mapped by hand
  have ``manual = 1``; automatic rows they replaced have ``defer = 1``.

Flows after several branches are out use the exact DC formula, not a sum of single LODFs:
for the outaged set S, solve ``(I - L_SS) f' = f_S`` (L_SS the LODFs among the outaged
branches, zero diagonal), then ``flow_m = f_m + L_mS f'``. A constraint "A flo B" is the
flow on A with B (the contingency) also out, so its post-contingency flow puts B in S.

The base case is a fixed snapshot (the latest case, a few months old), not today's system,
so impacts are best read as MW per outage on a typical dispatch, and as % of the limit.
"""

import datetime
import re
import time

import numpy as np
import pandas as pd
from sqlalchemy import text

from lib import outage_search as osearch


def _no_warn(_message):
    pass


def _in(prefix: str, values) -> tuple[str, dict]:
    values = list(values)
    return ", ".join(f":{prefix}{i}" for i in range(len(values))), {f"{prefix}{i}": v for i, v in enumerate(values)}


_tables = {"loaded": 0.0, "names": set()}


def _table_names(engine) -> set:
    if time.time() - _tables["loaded"] > 3600:
        with engine.connect() as conn:
            _tables["names"] = {r[0] for r in conn.execute(text("SHOW TABLES")).fetchall()}
        _tables["loaded"] = time.time()
    return _tables["names"]


# --- CASE VERSION ---

def latest_case(engine, market: str) -> datetime.date | None:
    """The newest netmap version_dt that has both a branch table and an LODF table."""
    osearch_markets = osearch.MARKETS
    if market not in osearch_markets:
        raise ValueError(f"Unknown market '{market}'")
    names = _table_names(engine)
    with engine.connect() as conn:
        versions = [r[0] for r in conn.execute(text(f"""
            SELECT DISTINCT version_dt FROM {market}_netmap_monitored_elements ORDER BY version_dt DESC
        """)).fetchall()]
    for v in versions:
        suffix = v.strftime("%Y%m%d")
        if f"{market}_pw_lodfs_{suffix}" in names and f"{market}_pw_branches_{suffix}" in names:
            return v
    return None


def _suffix(version: datetime.date) -> str:
    return version.strftime("%Y%m%d")


# --- MAPPINGS ---

def _pick_netmap_rows(df: pd.DataFrame, key: str) -> pd.DataFrame:
    """Per key, the rows to use: hand-mapped rows when there are any, else the non-deferred
    automatic rows, else the best-scoring automatic rows."""
    picked = []
    for _, g in df.groupby(key):
        manual = g[g["manual"] == 1]
        if not manual.empty:
            picked.append(manual)
            continue
        active = g[g["defer"] != 1]
        if active.empty:
            active = g[g["score"] == g["score"].max()]
        picked.append(active)
    return pd.concat(picked) if picked else df.iloc[0:0]


def monitored_branches(engine, market: str, version: datetime.date, me_id: int) -> pd.DataFrame:
    """Model branches for a monitored element: [pw_branch_id, flow_reversed, series_sequence].
    Series elements carry the same flow, so callers normally use the first row."""
    with engine.connect() as conn:
        df = pd.read_sql(text(f"""
            SELECT official_monitored_element_id AS me_id, pw_branch_id, pw_short_name,
                   pw_branch_flow_likely_reversed AS flow_reversed, series_sequence, score, `manual`, `defer`
            FROM {market}_netmap_monitored_elements
            WHERE version_dt = :v AND official_monitored_element_id = :m AND pw_branch_id IS NOT NULL
        """), conn, params={"v": version, "m": me_id})
    if df.empty:
        return df
    return _pick_netmap_rows(df, "me_id").sort_values("series_sequence", na_position="first")


def outage_branches(engine, market: str, version: datetime.date, equipment_ids) -> pd.DataFrame:
    """Model branches for outage equipment IDs: [outage_equipment_id, pw_branch_id]."""
    ids = sorted({int(i) for i in equipment_ids if pd.notna(i)})
    if not ids:
        return pd.DataFrame(columns=["outage_equipment_id", "pw_branch_id"])
    frames = []
    for start in range(0, len(ids), 1000):
        placeholders, params = _in("e", ids[start:start + 1000])
        params["v"] = version
        with engine.connect() as conn:
            frames.append(pd.read_sql(text(f"""
                SELECT outage_equipment_id, pw_branch_id, series_sequence, score, `manual`, `defer`
                FROM {market}_netmap_outage_elements
                WHERE version_dt = :v AND outage_equipment_id IN ({placeholders}) AND pw_branch_id IS NOT NULL
            """), conn, params=params))
    df = pd.concat(frames)
    if df.empty:
        return df[["outage_equipment_id", "pw_branch_id"]]
    df = _pick_netmap_rows(df, "outage_equipment_id")
    return df[["outage_equipment_id", "pw_branch_id"]].drop_duplicates()


def _branches_by_buses(engine, market: str, version: datetime.date, triples) -> list[int]:
    """Branch IDs for (bus1, bus2, circuit) triples, matched in either direction."""
    found = []
    with engine.connect() as conn:
        for b1, b2, ckt in triples:
            row = conn.execute(text(f"""
                SELECT branch_id FROM {market}_pw_branches_{_suffix(version)}
                WHERE ((bus1_num = :a AND bus2_num = :b) OR (bus1_num = :b AND bus2_num = :a)) AND line_circuit = :c
                LIMIT 1
            """), {"a": str(b1), "b": str(b2), "c": str(ckt)}).fetchone()
            if row:
                found.append(int(row[0]))
    return found


# PJM contingency elements look like "LINE 500 KV CONASTON-PEACHBOT 5012" or
# "XFORMER 230 KV ..."; breaker entries ("CONASTON 500 KV CONASTON 5012/500-4 GCB B")
# only describe the switching and are skipped.
PJM_CTG_ELEMENT = re.compile(r"^(LINE|XFORMER|TRANSFORMER|PS|PHASE SHIFTER|SERIES DEVICE|SER DEV)\s+"
                             r"(\d+(?:\.\d+)?)\s+KV\s+(.+)$", re.IGNORECASE)


def contingency_branches(engine, market: str, version: datetime.date, contingency: str | None,
                         warn=_no_warn) -> list[int]:
    """Model branches taken out by a constraint's contingency, or [] when it is unknown."""
    if not contingency:
        return []
    if market == "pjm":
        triples = []
        for element in contingency.split(";"):
            m = PJM_CTG_ELEMENT.match(element.strip())
            if m:
                triples.append((float(m.group(2)), m.group(3).strip()))
        if not triples:
            warn(f"Could not read any line or transformer from contingency '{contingency[:120]}'.")
            return []
        ids = []
        with engine.connect() as conn:
            for kv, b3 in triples:
                ids += [r[0] for r in conn.execute(text("""
                    SELECT id FROM pjm_outage_equipments WHERE kv = :kv AND TRIM(b3) = :b3
                """), {"kv": kv, "b3": b3}).fetchall()]
        branches = outage_branches(engine, market, version, ids)["pw_branch_id"].astype(int).tolist()
    else:
        table = f"{market}_netmap_contingency_elements"
        if table not in _table_names(engine):
            warn(f"No contingency mapping table for {market.upper()}.")
            return []
        with engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT pw_branch_id, pw_from_bus_num, pw_to_bus_num, pw_line_circuit, `defer`
                FROM {table} WHERE version_dt = :v AND contingency_element_name = :c
            """), {"v": version, "c": contingency}).fetchall()
        rows = [r for r in rows if r[4] != 1] or rows
        branches = [int(r[0]) for r in rows if r[0] is not None]
        branches += _branches_by_buses(engine, market, version,
                                       [(r[1], r[2], r[3]) for r in rows if r[0] is None and r[1] and r[2]])
    if not branches:
        warn(f"Contingency '{contingency[:120]}' is not mapped to the network model, so impacts are pre-contingency.")
    return sorted(set(branches))


# --- CONSTRAINT DEFINITION ---

def constraint_definition(engine, market: str, constraint_id: int, warn=_no_warn) -> dict:
    """Monitored element ID and contingency name for a constraint.

    New constraint IDs often have neither in ``official_constraints``; then another ID with
    the same name is used, and for MISO the contingency is the part of the name before the
    first underscore (e.g. GRE23101 in GRE23101_MCHENRY_TR1_TR12).
    """
    with engine.connect() as conn:
        row = conn.execute(text(f"""
            SELECT official_monitored_element_id, contingency_element_name,
                   COALESCE(rt_descriptive_name, da_descriptive_name)
            FROM {market}_official_constraints WHERE id = :c
        """), {"c": constraint_id}).fetchone()
        me_id, contingency, name = row if row else (None, None, None)
        if me_id is None or contingency is None:
            if name is None:
                for kind in ("rt", "da"):
                    name = conn.execute(text(f"""
                        SELECT MAX(constraint_name) FROM {market}_{kind}_constraint_shadow_prices
                        WHERE official_constraint_id = :c
                    """), {"c": constraint_id}).scalar() or name
            if name:
                # The same name can belong to both directions of an element (<+1> and <-1>), so
                # use the same-name constraint that most recently had shadow prices on a mapped
                # element: that is the element and direction the market actually binds.
                other, latest = None, None
                for kind in ("rt", "da"):
                    row = conn.execute(text(f"""
                        SELECT s.official_constraint_id, s.official_monitored_element_id, MAX(s.dt) AS last_dt
                        FROM {market}_{kind}_constraint_shadow_prices s
                        WHERE s.constraint_name = :n AND s.official_constraint_id <> :c
                          AND s.official_monitored_element_id IS NOT NULL
                        GROUP BY s.official_constraint_id, s.official_monitored_element_id
                        ORDER BY last_dt DESC LIMIT 1
                    """), {"n": name, "c": constraint_id}).fetchone()
                    if row and (latest is None or row[2] > latest):
                        latest = row[2]
                        ctg = conn.execute(text(f"SELECT contingency_element_name FROM {market}_official_constraints "
                                                "WHERE id = :i"), {"i": row[0]}).scalar()
                        other = (row[0], row[1], ctg)
                if other is None:
                    other = conn.execute(text(f"""
                        SELECT id, official_monitored_element_id, contingency_element_name FROM {market}_official_constraints
                        WHERE (rt_descriptive_name = :n OR da_descriptive_name = :n) AND id <> :c
                          AND official_monitored_element_id IS NOT NULL
                        ORDER BY last_used DESC LIMIT 1
                    """), {"n": name, "c": constraint_id}).fetchone()
                if other:
                    warn(f"Constraint {constraint_id} has no monitored element/contingency yet; "
                         f"using constraint {other[0]} with the same name.")
                    me_id = me_id or other[1]
                    contingency = contingency or other[2]
        if contingency is None and market == "miso" and name and "_" in name:
            contingency = name.split("_", 1)[0]
        me_name = conn.execute(text(f"""
            SELECT monitored_element_name FROM {market}_official_monitored_elements WHERE id = :m
        """), {"m": me_id}).scalar() if me_id is not None else None
    return {"me_id": me_id, "me_name": me_name, "contingency": contingency, "name": name,
            "direction": binding_direction(me_name)}


ME_DIRECTION = re.compile(r"<([+-])1>\s*$")


def binding_direction(me_name: str | None) -> int | None:
    """+1 or -1 from a monitored element name ending in <+1> / <-1> (MISO), else None.

    Checked on 2026-10-08 against desk notes for MISO Barton Lake and McHenry: the sign is
    the binding direction in the network model's branch orientation, so an outage loads the
    constraint when it moves model flow that way. (The netmap ``flow_likely_reversed`` flag
    did not agree with the notes and is not used.)
    """
    m = ME_DIRECTION.search(me_name or "")
    return (1 if m.group(1) == "+" else -1) if m else None


# --- FLOWS AND LODFS ---

def branch_info(engine, market: str, version: datetime.date, branch_ids) -> pd.DataFrame:
    """[branch_id, name, line_mw, line_limit_mw] from the case."""
    ids = sorted({int(b) for b in branch_ids})
    if not ids:
        return pd.DataFrame(columns=["branch_id", "name", "line_mw", "line_limit_mw"])
    placeholders, params = _in("b", ids)
    with engine.connect() as conn:
        df = pd.read_sql(text(f"""
            SELECT branch_id, CONCAT(bus1_name, ' - ', bus2_name, ' ', line_circuit) AS name, line_mw, line_limit_mw
            FROM {market}_pw_branches_{_suffix(version)} WHERE branch_id IN ({placeholders})
        """), conn, params=params)
    return df


def lodf_matrix(engine, market: str, version: datetime.date, monitored, outaged) -> pd.DataFrame:
    """LODFs as fractions, monitored branches as rows and outaged branches as columns.
    Pairs not stored in the case (effectively zero) are 0."""
    mons, outs = sorted({int(b) for b in monitored}), sorted({int(b) for b in outaged})
    mat = pd.DataFrame(0.0, index=mons, columns=outs)
    if not mons or not outs:
        return mat
    m_ph, params = _in("m", mons)
    for start in range(0, len(outs), 1000):
        o_ph, o_params = _in("o", outs[start:start + 1000])
        with engine.connect() as conn:
            rows = conn.execute(text(f"""
                SELECT monitored_branch_id, outage_branch_id, lodf FROM {market}_pw_lodfs_{_suffix(version)}
                WHERE monitored_branch_id IN ({m_ph}) AND outage_branch_id IN ({o_ph})
            """), {**params, **o_params}).fetchall()
        for m, o, v in rows:
            mat.at[int(m), int(o)] = float(v) / 100.0
    return mat


def _collapse_series(lodf: pd.DataFrame, branches: list[int]) -> list[int]:
    """Drops branches in series with one already kept: taking either out stops the other's
    flow (LODF of -100% both ways), so keeping both makes the outage equations singular."""
    kept = []
    for b in branches:
        if any(abs(lodf.at[a, b] + 1.0) < 0.03 and abs(lodf.at[b, a] + 1.0) < 0.03 for a in kept):
            continue
        kept.append(b)
    return kept


def flow_after_outages(flows: pd.Series, lodf: pd.DataFrame, monitored: int, outaged, warn=None) -> float:
    """DC flow on ``monitored`` with all ``outaged`` branches out (exact multi-outage formula).

    ``lodf`` must have rows for ``monitored`` and every outaged branch, and columns for every
    outaged branch; ``flows`` the base-case flow of each branch. If ``monitored`` is in series
    with an outaged branch its flow is 0. If the outages split the network (the equations
    are singular), single-outage superposition is used instead and ``warn`` is told.
    """
    monitored = int(monitored)
    s = [int(b) for b in dict.fromkeys(outaged) if int(b) != monitored]
    if not s:
        return float(flows.get(monitored, 0.0))
    if monitored in lodf.columns and any(abs(lodf.at[monitored, b] + 1.0) < 0.03
                                         and abs(lodf.at[b, monitored] + 1.0) < 0.03 for b in s if b in lodf.index):
        return 0.0
    s = _collapse_series(lodf, s)
    l_ss = lodf.loc[s, s].to_numpy(copy=True)
    np.fill_diagonal(l_ss, 0.0)
    f_s = flows.reindex(s).fillna(0.0).to_numpy()
    a = np.eye(len(s)) - l_ss
    if np.linalg.cond(a) > 1e6:
        if warn:
            warn("Some outage combinations split the network model; used single-outage superposition for them.")
        effective = f_s
    else:
        effective = np.linalg.solve(a, f_s)
    return float(flows.get(monitored, 0.0) + lodf.loc[monitored, s].to_numpy() @ effective)


# --- NEIGHBORING MARKETS (MISO) ---

def neighbor_outage_branches(engine, market: str, version: datetime.date, source_market: str, equipment_ids,
                             warn=None) -> pd.DataFrame:
    """Model branches for another market's outage equipment, for MISO constraints:
    ``miso_tiospf_outage_branch_mappings`` maps the outage to a MISO official branch, whose
    EMS name ``miso_netmodel_eqmap_lines`` / ``_transformers`` map to IDC buses; those are
    matched to case branches by bus number, then by bus name.
    Returns [outage_equipment_id, pw_branch_id]."""
    ids = sorted({int(i) for i in equipment_ids if pd.notna(i)})
    empty = pd.DataFrame(columns=["outage_equipment_id", "pw_branch_id"])
    if market != "miso" or not ids:
        return empty
    placeholders, params = _in("e", ids)
    params["src"] = source_market
    with engine.connect() as conn:
        maps = pd.read_sql(text(f"""
            SELECT t.outage_equipment_id, b.name AS branch_name
            FROM miso_tiospf_outage_branch_mappings t
            JOIN miso_official_branches b ON b.id = t.official_branch_id
            WHERE t.source_market = :src AND t.outage_equipment_id IN ({placeholders})
        """), conn, params=params)
        if maps.empty:
            return empty
        eq_version = conn.execute(text("SELECT MAX(version_dt) FROM miso_netmodel_eqmap_lines WHERE version_dt <= :v"),
                                  {"v": version}).scalar() or version
        lines = pd.read_sql(text("""
            SELECT CONCAT(ems_line_name, ' ', ems_segment_name) AS branch_name, idc_from_bus_number AS b1,
                   idc_to_bus_number AS b2, idc_ckt AS ckt, idc_from_bus_name AS n1, idc_to_bus_name AS n2
            FROM miso_netmodel_eqmap_lines WHERE version_dt = :v
        """), conn, params={"v": eq_version})
        xfmrs = pd.read_sql(text("""
            SELECT CONCAT(ems_station_name, ' ', ems_xfmr_name) AS branch_name, idc_from_bus_number AS b1,
                   idc_to_bus_number AS b2, idc_ckt AS ckt, idc_from_bus_name AS n1, idc_to_bus_name AS n2
            FROM miso_netmodel_eqmap_transformers WHERE version_dt = :v
        """), conn, params={"v": eq_version})
        branches = pd.read_sql(text(f"""
            SELECT branch_id, bus1_num, bus2_num, bus1_name, bus2_name, line_circuit FROM {market}_pw_branches_{_suffix(version)}
        """), conn)

    maps["key"] = maps["branch_name"].str.split().str.join(" ")
    eq = pd.concat([lines, xfmrs], ignore_index=True)
    eq["key"] = eq["branch_name"].str.split().str.join(" ")
    cand = maps.merge(eq, on="key")
    if cand.empty:
        return empty
    for col in ("b1", "b2", "ckt"):
        cand[col] = cand[col].astype(str).str.strip()
    branches["bus1_num"], branches["bus2_num"] = branches["bus1_num"].astype(str), branches["bus2_num"].astype(str)
    branches["ckt"] = branches["line_circuit"].astype(str).str.strip()

    def match(left_cols, right_cols):
        fwd = cand.merge(branches, left_on=left_cols, right_on=right_cols)
        rev = cand.merge(branches, left_on=left_cols[::-1], right_on=right_cols)
        both = pd.concat([fwd, rev])
        # Prefer the matching circuit when several branches join the same buses.
        both["ckt_match"] = both["ckt_x"] == both["ckt_y"] if "ckt_x" in both else True
        return both.sort_values("ckt_match", ascending=False).drop_duplicates(["outage_equipment_id", "b1", "b2"])

    by_num = match(["b1", "b2"], ["bus1_num", "bus2_num"])
    cand["n1"], cand["n2"] = cand["n1"].astype(str).str.strip(), cand["n2"].astype(str).str.strip()
    branches["bus1_name"] = branches["bus1_name"].astype(str).str.strip()
    branches["bus2_name"] = branches["bus2_name"].astype(str).str.strip()
    by_name = match(["n1", "n2"], ["bus1_name", "bus2_name"])
    found = pd.concat([by_num, by_name[~by_name["outage_equipment_id"].isin(by_num["outage_equipment_id"])]])

    # Last resort: one end matches by bus number and the other end is another bus at the same
    # station (same first 6 letters/digits of the name, e.g. GARRISN7 vs GARRISN48341). Only
    # used when it gives exactly one branch.
    def prefix(series):
        return series.astype(str).str.replace(r"[^A-Za-z0-9]", "", regex=True).str[:6].str.upper()

    rest = cand[~cand["outage_equipment_id"].isin(found["outage_equipment_id"])]
    if not rest.empty:
        br = branches.assign(p1=prefix(branches["bus1_name"]), p2=prefix(branches["bus2_name"]))
        rest = rest.assign(p1=prefix(rest["n1"]), p2=prefix(rest["n2"]))
        pairs = [(["b1", "p2"], ["bus1_num", "p2"]), (["b1", "p2"], ["bus2_num", "p1"]),
                 (["b2", "p1"], ["bus1_num", "p2"]), (["b2", "p1"], ["bus2_num", "p1"])]
        loose = pd.concat([rest.merge(br, left_on=lc, right_on=rc) for lc, rc in pairs])
        if not loose.empty:
            counts = loose.groupby("outage_equipment_id")["branch_id"].nunique()
            found = pd.concat([found, loose[loose["outage_equipment_id"].isin(counts[counts == 1].index)]])
    df = found[["outage_equipment_id", "branch_id"]].rename(columns={"branch_id": "pw_branch_id"}).drop_duplicates()
    missing = maps["outage_equipment_id"].nunique() - df["outage_equipment_id"].nunique()
    if missing and warn:
        warn(f"{missing} of {maps['outage_equipment_id'].nunique()} {source_market.upper()} outages mapped to MISO "
             "branches could not be found in the network model.")
    return df


# --- LIMITS ---

ME_NAME_SUFFIX = re.compile(r"\s+[A-Z]\s+\d+(\.\d+)?\s*KV\s*$", re.IGNORECASE)


def constraint_limit(engine, market: str, me_id: int | None, day: datetime.date, hr: int | None,
                     version: datetime.date | None, monitored_branch: int | None) -> dict:
    """The best limit available for a monitored element:

    1. PJM: today's branch rating from ``pjm_branch_hourly_ratings``, found by matching the
       monitored element name to ``pjm_official_branches`` (normal and emergency ratings;
       post-contingency constraints are normally held to the emergency rating)
    2. the limit in the PowerWorld case
    Returns {limit_mw, normal_mw, emergency_mw, source}, with None values when unknown.
    """
    out = {"limit_mw": None, "normal_mw": None, "emergency_mw": None, "source": None}
    if market == "pjm" and me_id is not None:
        with engine.connect() as conn:
            name = conn.execute(text("SELECT monitored_element_name FROM pjm_official_monitored_elements WHERE id = :m"),
                                {"m": me_id}).scalar()
            if name:
                normalized = ME_NAME_SUFFIX.sub("", name.strip()).strip()
                branch = conn.execute(text("""
                    SELECT id FROM pjm_official_branches WHERE name_normalized = :n ORDER BY last_used DESC LIMIT 1
                """), {"n": normalized}).scalar()
                if branch:
                    rating = conn.execute(text("""
                        SELECT normal_rating, emergency_rating FROM pjm_branch_hourly_ratings
                        WHERE official_branch_id = :b AND dt = :d ORDER BY ABS(hr - :h) LIMIT 1
                    """), {"b": branch, "d": day, "h": hr or 12}).fetchone()
                    if rating:
                        out.update(normal_mw=rating[0], emergency_mw=rating[1], limit_mw=rating[1] or rating[0],
                                   source=f"PJM branch rating ({normalized}, emergency)")
                        return out
    if version is not None and monitored_branch is not None:
        info = branch_info(engine, market, version, [monitored_branch])
        if not info.empty and info["line_limit_mw"].iloc[0]:
            out.update(limit_mw=float(info["line_limit_mw"].iloc[0]),
                       source=f"PowerWorld case {version} limit")
    return out
