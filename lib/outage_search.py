"""Outage & shadow price search logic, shared by the Streamlit page and the MCP server.

``pages/outage_and_shadow_search.py`` renders these results in the browser, and
``mcp_server.py`` hands them to an AI client. Both must produce the same search
fields, matches, and clipboard text, so the logic lives here once.

Nothing in this module imports Streamlit. Functions that can hit a recoverable
database error take a ``warn`` callable: the page passes ``st.warning`` so the
user sees the problem, and the MCP server collects the messages and returns them
with the result. Either way a failed query is reported, never silently dropped.
"""

import re

import pandas as pd
from sqlalchemy import text

MARKETS = ["miso", "spp", "pjm", "ercot", "caiso"]
SEARCH_TYPES = ["Equipment ID", "Group ID", "Manual string"]
CONTEXT_OPTIONS = ["All", "rt_shadow", "rt_shadow_forecast", "da_shadow"]
LOOKBACK_OPTIONS = ["3 Years", "1 Month", "6 Months", "1 Year", "5 Years", "All Time"]


def _no_warn(_message):
    pass


# --- TEXT HELPERS ---

def clean_id(raw_input: str) -> str:
    """Strips leading letters and colons, keeping only the trailing numbers."""
    if not raw_input:
        return ""
    parts = raw_input.strip().split(":")
    last_part = parts[-1]
    return re.sub(r"\D", "", last_part)


def format_regex_pattern(field: str) -> str:
    """Regex for a search term: allows flexible spacing, and the term must not be part of a
    longer word or number.

    That is the same as wrapping the term in \b when it starts and ends with a letter or
    digit. Unlike \b it also works for terms that start or end with punctuation, such as
    SPP names ending in ")".
    """
    field_clean = re.sub(r'\s+', ' ', field.strip())
    escaped_field = re.sub(r'([.\\+*?^$()\[\]{}|])', r'\\\1', field_clean)
    flex_space_pattern = re.sub(r' ', r'\\s+', escaped_field)
    return rf"(?<!\w){flex_space_pattern}(?!\w)"


def clean_body_text_for_raw(text_val: str) -> str:
    """Cleans text into a single flat line for exports/clipboard."""
    if not text_val:
        return ""
    s = str(text_val)
    s = re.sub(r'<br\s*/?>', ' ', s, flags=re.IGNORECASE)
    s = s.replace("\\t", " ").replace("\t", " ")
    s = s.replace("\\r\\n", " ").replace("\\n", " ").replace("\\r", " ")
    s = s.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    return re.sub(r'\s+', ' ', s).strip()


def clean_annotation_notes(text_val: str) -> str:
    """Cleans annotation notes formatting."""
    if not text_val:
        return ""
    s = str(text_val)
    s = s.replace("\\t", " ").replace("\t", " ")
    s = s.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r'<br\s*/?>', '\n', s, flags=re.IGNORECASE)
    return s.strip()


def extract_matching_sentence(body_text: str, search_fields: list[str]):
    """Extracts ONLY the sentence containing a matching search term with flexible spacing."""
    if not body_text or not search_fields:
        return None

    text_clean = str(body_text)
    text_clean = re.sub(r"\*+", "", text_clean)
    text_clean = re.sub(r'\[([^\]]+)\]\s*\(([^)]+)\)', r'\1', text_clean)

    raw_sentences = re.split(r'(?<=[.!?|])\s+|\n|\r', text_clean)

    for sentence in raw_sentences:
        s = sentence.strip()
        if not s:
            continue
        for term in search_fields:
            if re.search(format_regex_pattern(term), s, re.IGNORECASE):
                return re.sub(r'\s+', ' ', s).strip()

    return None


def format_timestamp(value) -> str:
    """Formats a DB timestamp as YYYY-MM-DD HH:MM, or '' when missing."""
    if value is None:
        return ""
    try:
        return pd.to_datetime(value).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(value)


def lookback_start_date(lookback_option: str, now: pd.Timestamp):
    """Start of the note search window for a lookback option, or None for "All Time"."""
    if lookback_option == "1 Month":
        return now - pd.DateOffset(months=1)
    elif lookback_option == "6 Months":
        return now - pd.DateOffset(months=6)
    elif lookback_option == "1 Year":
        return now - pd.DateOffset(years=1)
    elif lookback_option == "3 Years":
        return now - pd.DateOffset(years=3)
    elif lookback_option == "5 Years":
        return now - pd.DateOffset(years=5)
    return None


def format_outage_name(outage_mkt: str, row: dict) -> str:
    """Human-readable outage name from one transmission outage row, or '' if empty.

    ``row`` needs the market's name columns: PJM b1/b3/kv, MISO kv/from_station/
    to_station, SPP ems_equipment_name, ERCOT from_station/to_station, CAISO
    kv/substation_cim_name.
    """
    if outage_mkt == "pjm":
        b1 = str(row["b1"]).strip() if row["b1"] is not None else ""
        b3 = str(row["b3"]).strip() if row["b3"] is not None else ""
        kv_str = ""
        if row["kv"] is not None and str(row["kv"]).strip() != "":
            try:
                kv_num = int(float(str(row["kv"]).strip()))
                kv_str = f"{kv_num}kv"
            except (ValueError, TypeError):
                kv_str = f"{row['kv']}kv"
        return " ".join(p for p in [b1, b3, kv_str] if p)

    elif outage_mkt == "miso":
        cols = [row["kv"], row["from_station"], row["to_station"]]
        return " ".join(str(col).strip() for col in cols if col is not None and str(col).strip() != "")

    elif outage_mkt == "spp":
        if row["ems_equipment_name"] is not None and str(row["ems_equipment_name"]).strip():
            return str(row["ems_equipment_name"]).strip()
        return ""

    elif outage_mkt == "ercot":
        fs = str(row["from_station"]).strip() if row["from_station"] is not None else ""
        ts = str(row["to_station"]).strip() if row["to_station"] is not None else ""
        return f"{fs}-{ts}" if (fs or ts) else ""

    elif outage_mkt == "caiso":
        kv = str(row["kv"]).strip() if row["kv"] is not None else ""
        sub = str(row["substation_cim_name"]).strip() if row["substation_cim_name"] is not None else ""
        return " ".join(p for p in [kv, sub] if p)

    return ""


# Columns format_outage_name reads, per market.
OUTAGE_NAME_COLUMNS = {
    "pjm": ["b1", "b3", "kv"],
    "miso": ["kv", "from_station", "to_station"],
    "spp": ["ems_equipment_name"],
    "ercot": ["from_station", "to_station"],
    "caiso": ["kv", "substation_cim_name"],
}


# --- DIRECT SQL DATABASE QUERY HELPERS ---

def fetch_transmission_outage_flags(engine, current_mkt: str, eq_ids, warn=_no_warn) -> list[dict]:
    """Fetches the most recent transmission outage flag notes directly from the database for non-PJM markets."""
    c_mkt = current_mkt.lower()
    if not eq_ids or c_mkt == "pjm":
        return []

    eq_ids = list(eq_ids)
    try:
        with engine.connect() as conn:
            placeholders = ", ".join([f":e{i}" for i in range(len(eq_ids))])
            params = {f"e{i}": eq for i, eq in enumerate(eq_ids)}

            flag_query = text(f"""
                SELECT outage_id, notes, updated_at, flag_type
                FROM {c_mkt}_transmission_outage_flags
                WHERE outage_id IN ({placeholders})
                ORDER BY updated_at DESC, id DESC
            """)
            res = conn.execute(flag_query, params).fetchall()

            flags_by_eq = {}
            for row in res:
                eq_id_str = str(row[0])
                if eq_id_str not in flags_by_eq:
                    flags_by_eq[eq_id_str] = {
                        "outage_id": eq_id_str,
                        "notes": row[1] if row[1] is not None else "",
                        "updated_at": format_timestamp(row[2]),
                        "flag_type": str(row[3]) if len(row) > 3 and row[3] is not None else ""
                    }
            return list(flags_by_eq.values())
    except Exception as e:
        warn(f"Could not fetch transmission outage flags from {c_mkt}_transmission_outage_flags: {e}")
        return []


def dedupe_flag_notes(raw_flag_notes: list[dict]) -> list[dict]:
    """Drops flag notes whose cleaned text body repeats an earlier one."""
    flag_notes = []
    seen_flag_texts = set()
    for flag in raw_flag_notes:
        note_key = clean_body_text_for_raw(flag.get('notes', ''))
        if note_key and note_key not in seen_flag_texts:
            seen_flag_texts.add(note_key)
            flag_notes.append(flag)
    return flag_notes


def fetch_outage_annotations(engine, eq_ids, outage_mkt: str, current_mkt: str, warn=_no_warn) -> list[dict]:
    """Fetches outage annotations directly from the database."""
    if not eq_ids:
        return []

    eq_ids = list(eq_ids)
    c_mkt = current_mkt.lower()

    try:
        with engine.connect() as conn:
            monelem_table_name = f"{c_mkt}_outage_annotation_monelems"
            col_res = conn.execute(text(f"SHOW COLUMNS FROM {monelem_table_name}")).fetchall()
            cols = [r[0] for r in col_res]

            monelem_col = "official_monitored_element_id"
            if "official_monitored_element_id" in cols:
                monelem_col = "official_monitored_element_id"
            elif "monitored_element_id" in cols:
                monelem_col = "monitored_element_id"
            elif f"{c_mkt}_official_monitored_element_id" in cols:
                monelem_col = f"{c_mkt}_official_monitored_element_id"
            elif "miso_official_monitored_element_id" in cols:
                monelem_col = "miso_official_monitored_element_id"

            placeholders = ", ".join([f":e{i}" for i in range(len(eq_ids))])
            params = {f"e{i}": eq_id for i, eq_id in enumerate(eq_ids)}

            outage_type_exact = f"{outage_mkt.capitalize()}OutageEquipment"
            outage_type_like = f"%{outage_mkt.lower()}%"
            params["outage_type_exact"] = outage_type_exact
            params["outage_type_like"] = outage_type_like

            ann_query = text(f"""
                SELECT DISTINCT
                    a.id AS annotation_id,
                    a.name AS name,
                    a.notes AS notes,
                    a.updated_at AS updated_at,
                    m.{monelem_col} AS official_monitored_element_id
                FROM {c_mkt}_outage_annotations a
                JOIN {c_mkt}_outage_annotation_outages o
                    ON a.id = o.annotation_id
                JOIN {c_mkt}_outage_annotation_monelems m
                    ON a.id = m.annotation_id
                WHERE a.archived = 0
                  AND o.outage_id IN ({placeholders})
                  AND (o.outage_type = :outage_type_exact OR LOWER(o.outage_type) LIKE :outage_type_like)
            """)

            res = conn.execute(ann_query, params).fetchall()

            annotations = []
            for row in res:
                annotations.append({
                    "annotation_id": row[0],
                    "name": row[1] if row[1] is not None else "",
                    "notes": row[2] if row[2] is not None else "",
                    "updated_at": format_timestamp(row[3]),
                    "official_monitored_element_id": str(row[4]) if row[4] is not None else ""
                })
            return annotations
    except Exception as e:
        warn(f"Could not fetch outage annotations from {c_mkt}_outage_annotations: {e}")
        return []


def fetch_outage_names(engine, search_type: str, raw_input_id: str, outage_mkt: str, raw_eq_ids,
                       warn=_no_warn) -> list[str]:
    """Fetches human-readable outage names directly from the database."""
    outage_names = []
    target_eq_ids = []

    if search_type == "Equipment ID":
        target_eq_ids = list(raw_eq_ids)

    elif search_type == "Group ID":
        cleaned_num = clean_id(raw_input_id)
        if cleaned_num:
            try:
                with engine.connect() as conn:
                    assoc_query = text(f"""
                        SELECT associated_group_id
                        FROM {outage_mkt}_outage_group_associations
                        WHERE group_id = :grp_id
                    """)
                    assoc_res = conn.execute(assoc_query, {"grp_id": cleaned_num}).fetchall()
                    associated_group_ids = [str(row[0]) for row in assoc_res if row[0] is not None]
                    all_group_ids = list(dict.fromkeys([cleaned_num] + associated_group_ids))

                    if all_group_ids:
                        placeholders = ", ".join([f":g{i}" for i in range(len(all_group_ids))])
                        params = {f"g{i}": gid for i, gid in enumerate(all_group_ids)}

                        col_res = conn.execute(
                            text(f"SHOW COLUMNS FROM {outage_mkt}_outage_equipment_groups")).fetchall()
                        cols = [r[0] for r in col_res]
                        id_col = "id" if "id" in cols else (
                            "group_id" if "group_id" in cols else "outage_equipment_group_id")

                        grp_query = text(f"""
                            SELECT basis_outage_id
                            FROM {outage_mkt}_outage_equipment_groups
                            WHERE {id_col} IN ({placeholders})
                        """)
                        grp_res = conn.execute(grp_query, params).fetchall()
                        target_eq_ids = [str(row[0]) for row in grp_res if row[0] is not None]
            except Exception as e:
                warn(f"Could not fetch basis_outage_id for groups: {e}")

    if not target_eq_ids or outage_mkt not in OUTAGE_NAME_COLUMNS:
        return []

    target_eq_ids = list(dict.fromkeys(target_eq_ids))
    placeholders = ", ".join([f":e{i}" for i in range(len(target_eq_ids))])
    params = {f"e{i}": eq for i, eq in enumerate(target_eq_ids)}
    name_cols = OUTAGE_NAME_COLUMNS[outage_mkt]

    try:
        with engine.connect() as conn:
            q = text(f"""
                SELECT outage_equipment_id, {", ".join(name_cols)}
                FROM {outage_mkt}_transmission_outages
                WHERE outage_equipment_id IN ({placeholders})
            """)
            res = conn.execute(q, params).mappings().fetchall()
            found_eqs = set()
            for row in res:
                found_eqs.add(str(row["outage_equipment_id"]))
                name = format_outage_name(outage_mkt, row)
                if name:
                    outage_names.append(name)

            # MISO outages that have dropped off the transmission outage report are
            # still in miso_active_outages.
            if outage_mkt == "miso":
                missing_eqs = [eq for eq in target_eq_ids if eq not in found_eqs]
                if missing_eqs:
                    m_placeholders = ", ".join([f":me{i}" for i in range(len(missing_eqs))])
                    m_params = {f"me{i}": eq for i, eq in enumerate(missing_eqs)}
                    q_active = text(f"""
                        SELECT kv, from_station, to_station
                        FROM miso_active_outages
                        WHERE outage_equipment_id IN ({m_placeholders})
                    """)
                    res_act = conn.execute(q_active, m_params).mappings().fetchall()
                    for row in res_act:
                        name = format_outage_name("miso", row)
                        if name:
                            outage_names.append(name)

    except Exception as e:
        warn(f"Could not fetch human-readable outage names for {outage_mkt.upper()}: {e}")

    return list(dict.fromkeys(outage_names))


def fetch_monelem_meta_descriptions(engine, current_mkt: str, me_ids) -> dict[str, str]:
    """Fetches monitored element meta descriptions directly from the database."""
    if not me_ids:
        return {}

    me_ids = list(me_ids)
    c_mkt = current_mkt.lower()
    meta_descriptions = {}

    try:
        with engine.connect() as conn:
            placeholders = ", ".join([f":m{i}" for i in range(len(me_ids))])
            params = {f"m{i}": mid for i, mid in enumerate(me_ids)}

            meta_query = text(f"""
                SELECT official_monitored_element_id, description
                FROM {c_mkt}_monelem_meta
                WHERE official_monitored_element_id IN ({placeholders})
            """)
            res = conn.execute(meta_query, params).fetchall()
            for row in res:
                if row[0] is not None:
                    desc_raw = row[1] if row[1] is not None else ""
                    meta_descriptions[str(row[0])] = clean_body_text_for_raw(desc_raw)
    except Exception:
        pass

    return meta_descriptions


def load_market_notes(engine, current_market: str) -> pd.DataFrame:
    """Loads all notes and element names for the market. Slow (100k+ rows): callers should cache it."""
    query = f"""
        SELECT
            qn.official_monitored_element_id,
            ome.monitored_element_name,
            qn.dt,
            qn.context,
            qn.body
        FROM {current_market}_monelem_quick_notes qn
        LEFT JOIN {current_market}_official_monitored_elements ome
            ON qn.official_monitored_element_id = ome.id
    """
    with engine.connect() as conn:
        df = pd.read_sql(text(query), conn)
        df['dt'] = pd.to_datetime(df['dt'])
        return df


def filter_notes(notes_df: pd.DataFrame, search_fields, context_option: str, start_date_str) -> pd.DataFrame:
    """Applies the date, context, and text filters to the pre-loaded notes."""
    df = notes_df
    if df.empty:
        return df

    if start_date_str:
        start_dt = pd.to_datetime(start_date_str)
        df = df[df['dt'] >= start_dt]

    if context_option != "All":
        df = df[df['context'] == context_option]

    if df.empty:
        return df.copy()

    mask = pd.Series(False, index=df.index)

    for term in search_fields:
        term_clean = term.strip()
        if not term_clean:
            continue
        mask |= df['body'].str.contains(format_regex_pattern(term_clean), case=False, na=False, regex=True)

    return df[mask].copy()


def execute_main_search_query(engine, notes_df: pd.DataFrame, current_market: str, search_fields,
                              context_option: str, start_date_str: str,
                              result_limit: int = 500) -> tuple[pd.DataFrame, bool]:
    """Executes an in-memory Pandas search, then fetches SQL shadow prices only for matched rows."""
    search_fields = list(search_fields)
    if not search_fields:
        return pd.DataFrame(), False

    if notes_df.empty:
        return notes_df, False

    df_matches = filter_notes(notes_df, search_fields, context_option, start_date_str)

    was_limited = len(df_matches) > result_limit

    # Sort and apply limit
    df_matches = df_matches.sort_values(by='dt', ascending=False)
    if was_limited:
        df_matches = df_matches.head(result_limit)

    # Fetch Targeted Shadow Prices via SQL
    if not df_matches.empty:
        unique_ids = df_matches['official_monitored_element_id'].unique().tolist()
        min_dt = df_matches['dt'].min().strftime('%Y-%m-%d %H:%M:%S')
        max_dt = df_matches['dt'].max().strftime('%Y-%m-%d %H:%M:%S')

        placeholders = ", ".join([f":id{i}" for i in range(len(unique_ids))])
        params = {f"id{i}": me_id for i, me_id in enumerate(unique_ids)}
        params.update({"min_dt": min_dt, "max_dt": max_dt})

        all_prices = []

        # Fetch RT Prices if present in matches
        if "rt_shadow" in df_matches['context'].values:
            rt_query = text(f"""
                SELECT official_monitored_element_id, dt, ROUND(SUM(COALESCE(shadow_price, 0))) as shadow_price
                FROM {current_market}_rt_constraint_shadow_prices
                WHERE official_monitored_element_id IN ({placeholders}) AND dt BETWEEN :min_dt AND :max_dt
                GROUP BY official_monitored_element_id, dt
            """)
            with engine.connect() as conn:
                rt_df = pd.read_sql(rt_query, conn, params=params)
                rt_df['context'] = 'rt_shadow'
                all_prices.append(rt_df)

        # Fetch RT Forecasts if present in matches
        if "rt_shadow_forecast" in df_matches['context'].values:
            rtf_query = text(f"""
                SELECT official_monitored_element_id, dt, ROUND(SUM(COALESCE(shadow, 0))) as shadow_price
                FROM {current_market}_rt_constraint_shadow_price_forecasts
                WHERE official_monitored_element_id IN ({placeholders}) AND dt BETWEEN :min_dt AND :max_dt
                GROUP BY official_monitored_element_id, dt
            """)
            with engine.connect() as conn:
                rtf_df = pd.read_sql(rtf_query, conn, params=params)
                rtf_df['context'] = 'rt_shadow_forecast'
                all_prices.append(rtf_df)

        # Fetch DA Prices if present in matches
        if "da_shadow" in df_matches['context'].values:
            da_query = text(f"""
                SELECT official_monitored_element_id, dt, ROUND(SUM(COALESCE(shadow_price, 0))) as shadow_price
                FROM {current_market}_da_constraint_shadow_prices
                WHERE official_monitored_element_id IN ({placeholders}) AND dt BETWEEN :min_dt AND :max_dt
                GROUP BY official_monitored_element_id, dt
            """)
            with engine.connect() as conn:
                da_df = pd.read_sql(da_query, conn, params=params)
                da_df['context'] = 'da_shadow'
                all_prices.append(da_df)

        # Merge prices back into the matched notes
        if all_prices:
            combined_prices = pd.concat(all_prices, ignore_index=True)
            combined_prices['dt'] = pd.to_datetime(combined_prices['dt'])
            combined_prices.rename(columns={'shadow_price': 'total_shadow_price'}, inplace=True)

            df_matches = pd.merge(
                df_matches,
                combined_prices,
                on=['official_monitored_element_id', 'dt', 'context'],
                how='left'
            )
        else:
            df_matches['total_shadow_price'] = 0.0

        df_matches['total_shadow_price'] = df_matches['total_shadow_price'].fillna(0.0)
    else:
        df_matches['total_shadow_price'] = pd.Series(dtype=float)

    return df_matches, was_limited


# --- SEARCH FIELD CONSTRUCTION ---

def build_search_fields(engine, search_type: str, raw_input_id: str, current_market: str, outage_market: str,
                        warn=_no_warn) -> tuple[list[str], list[str], list[str]]:
    """Builds the note search terms for an outage, using the outage market's notation rules.

    Returns (search_fields, raw_eq_ids, raw_group_ids).
    """
    search_fields = []
    raw_eq_ids = []
    raw_group_ids = []

    if search_type == "Manual string":
        if raw_input_id:
            for term in raw_input_id.split(","):
                cleaned_term = re.sub(r'\s+', ' ', term.replace("\t", " ")).strip()
                if cleaned_term:
                    search_fields.append(cleaned_term)

        return list(dict.fromkeys(search_fields)), raw_eq_ids, raw_group_ids

    cleaned_num = clean_id(raw_input_id)
    if not cleaned_num:
        return search_fields, raw_eq_ids, raw_group_ids

    if search_type == "Equipment ID":
        raw_eq_ids.append(cleaned_num)

    elif search_type == "Group ID":
        try:
            with engine.connect() as conn:
                assoc_query = text(f"""
                    SELECT associated_group_id
                    FROM {outage_market}_outage_group_associations
                    WHERE group_id = :grp_id
                """)
                assoc_res = conn.execute(assoc_query, {"grp_id": cleaned_num}).fetchall()
                associated_group_ids = [str(row[0]) for row in assoc_res if row[0] is not None]

                all_group_ids = list(dict.fromkeys([cleaned_num] + associated_group_ids))

                if outage_market not in ["spp", "pjm"]:
                    for gid in all_group_ids:
                        raw_group_ids.append(f"GID {gid}")

                if all_group_ids:
                    placeholders = ", ".join([f":g{i}" for i in range(len(all_group_ids))])
                    eq_query = text(f"""
                        SELECT outage_equipment_id
                        FROM {outage_market}_outage_equipment_group_members
                        WHERE outage_equipment_group_id IN ({placeholders})
                    """)
                    params = {f"g{i}": gid for i, gid in enumerate(all_group_ids)}
                    eq_res = conn.execute(eq_query, params).fetchall()

                    for row in eq_res:
                        if row[0] is not None:
                            raw_eq_ids.append(str(row[0]))

        except Exception as e:
            warn(f"Could not fetch group associations or equipment members: {e}")
            if outage_market not in ["spp", "pjm"]:
                raw_group_ids.append(f"GID {cleaned_num}")

    # SPECIAL CASE 1: Current Market = SPP and Outage Market = MISO
    if current_market == "spp" and outage_market == "miso":
        if raw_eq_ids:
            try:
                with engine.connect() as conn:
                    placeholders = ", ".join([f":e{i}" for i in range(len(raw_eq_ids))])
                    spp_miso_params = {f"e{i}": eq for i, eq in enumerate(raw_eq_ids)}

                    miso_trans_query = text(f"""
                        SELECT kv, from_station, to_station
                        FROM miso_transmission_outages
                        WHERE outage_equipment_id IN ({placeholders})
                    """)
                    res_trans = conn.execute(miso_trans_query, spp_miso_params).fetchall()

                    miso_active_query = text(f"""
                        SELECT kv, from_station, to_station
                        FROM miso_active_outages
                        WHERE outage_equipment_id IN ({placeholders})
                    """)
                    res_active = conn.execute(miso_active_query, spp_miso_params).fetchall()

                    all_rows = res_trans + res_active

                    for row in all_rows:
                        parts = [str(col).strip() for col in row if col is not None and str(col).strip() != ""]
                        if parts:
                            joined_str = " ".join(parts)
                            cleaned_str = re.sub(r"\s+", " ", joined_str).strip()
                            if cleaned_str:
                                search_fields.append(cleaned_str)

            except Exception as e:
                warn(f"Could not fetch MISO transmission/active outage details for SPP search: {e}")

    # SPECIAL CASE 2: Current Market = PJM and Outage Market = MISO
    elif current_market == "pjm" and outage_market == "miso":
        search_fields.extend(raw_group_ids)

        if raw_eq_ids:
            for eq in raw_eq_ids:
                search_fields.append(f"equip {eq}")

            try:
                with engine.connect() as conn:
                    placeholders = ", ".join([f":e{i}" for i in range(len(raw_eq_ids))])
                    pjm_miso_params = {f"e{i}": eq for i, eq in enumerate(raw_eq_ids)}

                    pjm_miso_query = text(f"""
                        SELECT idc_equipment_name
                        FROM miso_transmission_outages
                        WHERE outage_equipment_id IN ({placeholders})
                    """)
                    res = conn.execute(pjm_miso_query, pjm_miso_params).fetchall()

                    for row in res:
                        if row[0] is not None and str(row[0]).strip():
                            cleaned_idc = re.sub(r"\s+", " ", str(row[0])).strip()
                            if cleaned_idc:
                                search_fields.append(cleaned_idc)

            except Exception as e:
                warn(f"Could not fetch MISO IDC equipment names for PJM search: {e}")

    else:
        if outage_market not in ["spp", "pjm"]:
            search_fields.extend(raw_group_ids)

        if raw_eq_ids:
            if outage_market in ["caiso", "miso"]:
                for eq in raw_eq_ids:
                    search_fields.append(f"equip {eq}")

            elif outage_market == "pjm":
                for eq in raw_eq_ids:
                    search_fields.append(f"e:{eq}")

            elif outage_market == "ercot":
                for eq in raw_eq_ids:
                    search_fields.append(eq)

            elif outage_market == "spp":
                # SPP notes name outages as "<equipment id> <EMS name>", but the name in the
                # note often differs a little from the EMS name ("138kV", a dropped " W",
                # a shortened name). The ID plus the first word of the name finds those too.
                try:
                    with engine.connect() as conn:
                        placeholders = ", ".join([f":e{i}" for i in range(len(raw_eq_ids))])
                        spp_query = text(f"""
                            SELECT outage_equipment_id, ems_equipment_name
                            FROM spp_transmission_outages
                            WHERE outage_equipment_id IN ({placeholders})
                        """)
                        spp_params = {f"e{i}": eq for i, eq in enumerate(raw_eq_ids)}
                        spp_res = conn.execute(spp_query, spp_params).fetchall()

                        for row in spp_res:
                            if row[1] is not None and str(row[1]).strip():
                                ems_name = str(row[1]).strip()
                                search_fields.append(ems_name)
                                search_fields.append(f"{row[0]} {ems_name.split()[0]}")
                except Exception as e:
                    warn(f"Could not fetch SPP EMS equipment names: {e}")

    return list(dict.fromkeys(search_fields)), raw_eq_ids, raw_group_ids


# --- RESULT ASSEMBLY ---

def group_by_monitored_element(engine, df: pd.DataFrame, annotations: list[dict], current_market: str) -> list[dict]:
    """Groups matched notes by monitored element, adds annotated elements that had no
    note matches, and sorts by total shadow price then latest note date (both descending)."""
    annotations_by_me = {}
    for ann in annotations:
        me_id_str = ann["official_monitored_element_id"]
        if me_id_str not in annotations_by_me:
            annotations_by_me[me_id_str] = []
        annotations_by_me[me_id_str].append(ann)

    me_groups = []
    me_ids_in_df = set()

    if not df.empty:
        grouped = df.groupby(['official_monitored_element_id', 'monitored_element_name'], sort=False)

        for (me_id, me_name), group_df in grouped:
            me_id_str = str(me_id)
            me_ids_in_df.add(me_id_str)
            sorted_group_df = group_df.sort_values(by='dt', ascending=False)
            max_dt = sorted_group_df['dt'].max()

            sum_shadow = pd.to_numeric(sorted_group_df['total_shadow_price'], errors='coerce').fillna(0).sum()

            me_groups.append({
                'me_id': me_id_str,
                'me_name': me_name if me_name is not None else "",
                'max_dt': max_dt,
                'sum_shadow': sum_shadow,
                'group_df': sorted_group_df,
                'annotations': annotations_by_me.get(me_id_str, [])
            })

    # Include annotated Monitored Elements even if they had no quick note matches
    for me_id_str, ann_list in annotations_by_me.items():
        if me_id_str and me_id_str not in me_ids_in_df:
            fetched_me_name = ""
            try:
                with engine.connect() as conn:
                    me_name_query = text(f"""
                        SELECT monitored_element_name
                        FROM {current_market}_official_monitored_elements
                        WHERE id = :me_id
                    """)
                    name_res = conn.execute(me_name_query, {"me_id": me_id_str}).fetchone()
                    if name_res and name_res[0]:
                        fetched_me_name = name_res[0]
            except Exception:
                pass

            me_groups.append({
                'me_id': me_id_str,
                'me_name': fetched_me_name,
                'max_dt': pd.NaT,
                'sum_shadow': 0,
                'group_df': pd.DataFrame(
                    columns=['official_monitored_element_id', 'monitored_element_name', 'dt', 'context',
                             'body', 'total_shadow_price']),
                'annotations': ann_list
            })

    me_groups.sort(key=lambda x: (x['sum_shadow'], str(x['max_dt'])), reverse=True)
    return me_groups


def monitored_element_url(current_market: str, me_id: str) -> str:
    return f"https://energycore.tioscapital.com/{current_market.lower()}/monitored_elements/{me_id}"


def shadow_snippets(me_groups: list[dict], search_fields: list[str], current_market: str) -> list[str]:
    """One markdown line per monitored element: the newest note sentence that mentions the
    outage, looking at RT notes first, then RT forecast, then DA."""
    all_snippets = []
    for g in me_groups:
        selected_row = None
        matching_sentence = None

        # Priority fallback: RT ('rt_shadow') -> FC ('rt_shadow_forecast') -> DA ('da_shadow')
        for ctx in ['rt_shadow', 'rt_shadow_forecast', 'da_shadow']:
            ctx_df = g['group_df'][g['group_df']['context'] == ctx]
            if not ctx_df.empty:
                for row in ctx_df.to_dict('records'):
                    sent = extract_matching_sentence(row['body'], search_fields)
                    if sent:
                        selected_row = row
                        matching_sentence = sent
                        break
                if selected_row is not None:
                    break

        if selected_row is not None and matching_sentence:
            me_id = str(g['me_id'])
            me_name = str(g['me_name'])
            header_label = f"{current_market.upper()} ME {me_id} {me_name}"
            markdown_link = f"[{header_label}]({monitored_element_url(current_market, me_id)})"

            note_dt = selected_row['dt']
            date_str = pd.to_datetime(note_dt).strftime("%Y-%m-%d") if pd.notna(note_dt) else ""

            all_snippets.append(f"*({date_str})* | **{markdown_link}**: {matching_sentence}")
    return all_snippets


def clipboard_text(current_market: str, search_fields: list[str], outage_names: list[str], flag_notes: list[dict],
                   me_groups: list[dict], df: pd.DataFrame, meta_descriptions: dict[str, str]) -> str:
    """The tab-separated "Copy Full Output to Clipboard" text block."""
    clipboard_parts = [
        f"Current Market: {current_market.upper()}",
        f"Match Fields: {', '.join(search_fields)}"
    ]
    if outage_names:
        clipboard_parts.append(f"Outage Name(s): {', '.join(outage_names)}")

    if current_market.lower() != "pjm" and flag_notes:
        clipboard_parts.append("")
        clipboard_parts.append("=== FLAG NOTES ===")
        clipboard_parts.append("Equipment ID\tNotes\tLast Update")
        for flag in flag_notes:
            raw_flag_note = clean_body_text_for_raw(flag['notes'])
            clipboard_parts.append(f"{flag['outage_id']}\t{raw_flag_note}\t{flag['updated_at']}")

    annotated_entries = []
    for g in me_groups:
        if g.get('annotations'):
            for ann in g['annotations']:
                raw_ann_note = clean_body_text_for_raw(ann.get('notes', ''))
                annotated_entries.append({
                    'me_id': g['me_id'],
                    'me_name': g['me_name'],
                    'ann_name': ann.get('name', ''),
                    'ann_notes': raw_ann_note,
                    'updated_at': ann.get('updated_at', '')
                })

    if annotated_entries:
        clipboard_parts.append("")
        clipboard_parts.append("=== OUTAGE ANNOTATIONS ===")
        clipboard_parts.append(
            "Monitored Element ID\tMonitored Element Name\tAnnotation Name\tAnnotation Notes\tLast Update")
        for entry in annotated_entries:
            clipboard_parts.append(
                f"{entry['me_id']}\t{entry['me_name']}\t{entry['ann_name']}\t{entry['ann_notes']}\t{entry['updated_at']}")

    clipboard_parts.append("")
    clipboard_parts.append("=== NOTE SEARCH RESULTS ===")
    if not df.empty:
        df_export = df.copy()
        df_export['body'] = df_export['body'].apply(clean_body_text_for_raw)
        clipboard_parts.append(df_export.to_csv(sep="\t", index=False))
    else:
        clipboard_parts.append("No note matches found.")

    clipboard_parts.append("")
    clipboard_parts.append("=== MONITORED ELEMENT DESCRIPTIONS ===")
    clipboard_parts.append("Monitored Element ID\tMonitored Element Name\tDescription")
    for g in me_groups:
        me_desc = meta_descriptions.get(g['me_id'], "")
        clipboard_parts.append(f"{g['me_id']}\t{g['me_name']}\t{me_desc}")

    return "\n".join(clipboard_parts)
