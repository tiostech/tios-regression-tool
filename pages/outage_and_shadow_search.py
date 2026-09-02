import re
import html
import datetime
import streamlit as st
import pandas as pd
from sqlalchemy import create_engine, text
from lib import db



st.set_page_config(page_title="Market Outage Search", layout="wide")
st.title("⚡ Power Market Outage & Shadow Price Search")


if not db.gate("1. Database"):
    st.stop()

engine = db.engine()

# --- HELPER FUNCTIONS ---

def clean_id(raw_input: str) -> str:
    """Strips leading letters and colons, keeping only the trailing numbers."""
    if not raw_input:
        return ""
    parts = raw_input.strip().split(":")
    last_part = parts[-1]
    return re.sub(r"\D", "", last_part)


def format_regex_pattern(field: str) -> str:
    """Safely wraps search terms in regex word boundaries (\b) and allows flexible spacing."""
    field_clean = re.sub(r'\s+', ' ', field.strip())
    escaped_field = re.sub(r'([.\\+*?^$()\[\]{}|])', r'\\\1', field_clean)
    flex_space_pattern = re.sub(r' ', r'\\s+', escaped_field)
    return rf"\b{flex_space_pattern}\b"


def clean_body_text_for_display(text_val: str) -> str:
    """Cleans line breaks and tabs for HTML display while preserving paragraph breaks."""
    if not text_val:
        return ""
    s = str(text_val)
    s = s.replace("\\t", " ").replace("\t", " ")
    s = re.sub(r'<br\s*/?>', '<br>', s, flags=re.IGNORECASE)
    s = s.replace("\\r\\n", "<br>").replace("\\n", "<br>").replace("\\r", "<br>")
    s = s.replace("\r\n", "<br>").replace("\n", "<br>").replace("\r", "<br>")
    return s


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
            term_clean = re.sub(r'\s+', ' ', term.strip())
            escaped_term = re.sub(r'([.\\+*?^$()\[\]{}|])', r'\\\1', term_clean)
            pattern_str = re.sub(r' ', r'\\s+', escaped_term)

            if re.search(rf"\b{pattern_str}\b", s, re.IGNORECASE):
                return re.sub(r'\s+', ' ', s).strip()

    return None


def render_links_only(text_val: str) -> str:
    """Converts [anchor text](url) or [anchor text] (url) into HTML hyperlinks."""
    if not text_val:
        return ""
    s = str(text_val)

    def link_replacer(match):
        label = match.group(1)
        url = match.group(2).strip()
        href = url if url.startswith(("http://", "https://")) else f"https://{url}"
        return f'<a href="{href}" target="_blank" style="color: #0066cc; text-decoration: underline; font-weight: 500;">{label}</a>'

    return re.sub(r'\[([^\]]+)\]\s*\(([^)]+)\)', link_replacer, s)


def highlight_matches(text_str: str, terms: list[str]) -> str:
    """Highlights matched search terms in red, supporting variable whitespace."""
    if not text_str or not terms:
        return text_str
    highlighted = str(text_str)
    for term in terms:
        term_clean = re.sub(r'\s+', ' ', term.strip())
        escaped_term = re.sub(r'([.\\+*?^$()\[\]{}|])', r'\\\1', term_clean)
        pattern_str = re.sub(r' ', r'\\s+', escaped_term)

        pattern = re.compile(rf"\b({pattern_str})\b", re.IGNORECASE)
        highlighted = pattern.sub(
            r'<span style="color: #d9534f; font-weight: bold; background-color: #fdf2f2; padding: 2px 4px; border-radius: 3px;">\1</span>',
            highlighted
        )
    return highlighted


def get_flag_icon_svg(flag_type: str) -> str:
    """Generates an inline SVG flag icon with a fill color determined by flag_type."""
    ft = (flag_type or "").lower()
    if "alert" in ft:
        color = "#d9534f"  # Red
    elif "watch" in ft:
        color = "#ff9800"  # Orange
    elif "safe" in ft:
        color = "#4caf50"  # Green
    else:
        color = "#d9534f"  # Default Red

    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20" viewBox="0 0 24 24" '
        f'fill="{color}" style="vertical-align: -3px; margin-right: 6px;">'
        f'<path d="M14.4 6L14 4H5v17h2v-7h5.6l.4 2h7V6h-5.6z"/></svg>'
    )


# --- DIRECT SQL DATABASE QUERY HELPERS ---

@st.cache_data(ttl=3600)
def fetch_transmission_outage_flags(current_mkt: str, eq_ids_tuple: tuple) -> list[dict]:
    """Fetches the most recent transmission outage flag notes directly from the database for non-PJM markets."""
    c_mkt = current_mkt.lower()
    if not eq_ids_tuple or c_mkt == "pjm":
        return []

    eq_ids = list(eq_ids_tuple)
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
                    updated_dt = row[2]
                    updated_str = ""
                    if updated_dt is not None:
                        try:
                            updated_str = pd.to_datetime(updated_dt).strftime("%Y-%m-%d %H:%M")
                        except Exception:
                            updated_str = str(updated_dt)

                    flags_by_eq[eq_id_str] = {
                        "outage_id": eq_id_str,
                        "notes": row[1] if row[1] is not None else "",
                        "updated_at": updated_str,
                        "flag_type": str(row[3]) if len(row) > 3 and row[3] is not None else ""
                    }
            return list(flags_by_eq.values())
    except Exception as e:
        st.warning(f"Could not fetch transmission outage flags from {c_mkt}_transmission_outage_flags: {e}")
        return []


@st.cache_data(ttl=3600)
def fetch_outage_annotations(eq_ids_tuple: tuple, outage_mkt: str, current_mkt: str) -> list[dict]:
    """Fetches outage annotations directly from the database."""
    if not eq_ids_tuple:
        return []

    eq_ids = list(eq_ids_tuple)
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
                updated_dt = row[3]
                updated_str = ""
                if updated_dt is not None:
                    try:
                        updated_str = pd.to_datetime(updated_dt).strftime("%Y-%m-%d %H:%M")
                    except Exception:
                        updated_str = str(updated_dt)

                annotations.append({
                    "annotation_id": row[0],
                    "name": row[1] if row[1] is not None else "",
                    "notes": row[2] if row[2] is not None else "",
                    "updated_at": updated_str,
                    "official_monitored_element_id": str(row[4]) if row[4] is not None else ""
                })
            return annotations
    except Exception as e:
        st.warning(f"Could not fetch outage annotations from {c_mkt}_outage_annotations: {e}")
        return []


@st.cache_data(ttl=3600)
def fetch_outage_names(search_type: str, raw_input_id: str, outage_mkt: str, raw_eq_ids_tuple: tuple) -> list[str]:
    """Fetches human-readable outage names directly from the database."""
    outage_names = []
    target_eq_ids = []

    if search_type == "Equipment ID":
        target_eq_ids = list(raw_eq_ids_tuple)

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
                st.warning(f"Could not fetch basis_outage_id for groups: {e}")

    if not target_eq_ids:
        return []

    target_eq_ids = list(dict.fromkeys(target_eq_ids))
    placeholders = ", ".join([f":e{i}" for i in range(len(target_eq_ids))])
    params = {f"e{i}": eq for i, eq in enumerate(target_eq_ids)}

    try:
        with engine.connect() as conn:
            if outage_mkt == "pjm":
                q = text(f"""
                    SELECT b1, b3, kv
                    FROM pjm_transmission_outages
                    WHERE outage_equipment_id IN ({placeholders})
                """)
                res = conn.execute(q, params).fetchall()
                for row in res:
                    b1 = str(row[0]).strip() if row[0] is not None else ""
                    b3 = str(row[1]).strip() if row[1] is not None else ""
                    kv_str = ""
                    if row[2] is not None and str(row[2]).strip() != "":
                        try:
                            kv_num = int(float(str(row[2]).strip()))
                            kv_str = f"{kv_num}kv"
                        except (ValueError, TypeError):
                            kv_str = f"{row[2]}kv"

                    parts = [p for p in [b1, b3, kv_str] if p]
                    if parts:
                        outage_names.append(" ".join(parts))

            elif outage_mkt == "miso":
                q = text(f"""
                    SELECT outage_equipment_id, kv, from_station, to_station
                    FROM miso_transmission_outages
                    WHERE outage_equipment_id IN ({placeholders})
                """)
                res = conn.execute(q, params).fetchall()
                found_eqs = set()
                for row in res:
                    found_eqs.add(str(row[0]))
                    parts = [str(col).strip() for col in row[1:] if col is not None and str(col).strip() != ""]
                    if parts:
                        outage_names.append(" ".join(parts))

                missing_eqs = [eq for eq in target_eq_ids if eq not in found_eqs]
                if missing_eqs:
                    m_placeholders = ", ".join([f":me{i}" for i in range(len(missing_eqs))])
                    m_params = {f"me{i}": eq for i, eq in enumerate(missing_eqs)}
                    q_active = text(f"""
                        SELECT kv, from_station, to_station
                        FROM miso_active_outages
                        WHERE outage_equipment_id IN ({m_placeholders})
                    """)
                    res_act = conn.execute(q_active, m_params).fetchall()
                    for row in res_act:
                        parts = [str(col).strip() for col in row if col is not None and str(col).strip() != ""]
                        if parts:
                            outage_names.append(" ".join(parts))

            elif outage_mkt == "spp":
                q = text(f"""
                    SELECT ems_equipment_name
                    FROM spp_transmission_outages
                    WHERE outage_equipment_id IN ({placeholders})
                """)
                res = conn.execute(q, params).fetchall()
                for row in res:
                    if row[0] is not None and str(row[0]).strip():
                        outage_names.append(str(row[0]).strip())

            elif outage_mkt == "ercot":
                q = text(f"""
                    SELECT from_station, to_station
                    FROM ercot_transmission_outages
                    WHERE outage_equipment_id IN ({placeholders})
                """)
                res = conn.execute(q, params).fetchall()
                for row in res:
                    fs = str(row[0]).strip() if row[0] is not None else ""
                    ts = str(row[1]).strip() if row[1] is not None else ""
                    if fs or ts:
                        outage_names.append(f"{fs}-{ts}")

            elif outage_mkt == "caiso":
                q = text(f"""
                    SELECT kv, substation_cim_name
                    FROM caiso_transmission_outages
                    WHERE outage_equipment_id IN ({placeholders})
                """)
                res = conn.execute(q, params).fetchall()
                for row in res:
                    kv = str(row[0]).strip() if row[0] is not None else ""
                    sub = str(row[1]).strip() if row[1] is not None else ""
                    parts = [p for p in [kv, sub] if p]
                    if parts:
                        outage_names.append(" ".join(parts))

    except Exception as e:
        st.warning(f"Could not fetch human-readable outage names for {outage_mkt.upper()}: {e}")

    return list(dict.fromkeys(outage_names))


@st.cache_data(ttl=3600)
def fetch_monelem_meta_descriptions(current_mkt: str, me_ids_tuple: tuple) -> dict[str, str]:
    """Fetches monitored element meta descriptions directly from the database."""
    if not me_ids_tuple:
        return {}

    me_ids = list(me_ids_tuple)
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


@st.cache_data(ttl=14400,
               show_spinner="📥 Initializing cache: Downloading market notes into memory (this may take a minute)...")
def load_market_notes(current_market: str) -> pd.DataFrame:
    """Pre-loads all notes and element names for the market into server RAM."""
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


@st.cache_data(ttl=3600)
def execute_main_search_query(current_market: str, search_fields_tuple: tuple, context_option: str,
                              start_date_str: str, outage_market: str = "miso", result_limit: int = 500) -> \
tuple[pd.DataFrame, bool]:
    """Executes an in-memory Pandas search, then fetches SQL shadow prices only for matched rows."""
    search_fields = list(search_fields_tuple)
    if not search_fields:
        return pd.DataFrame(), False

    # 1. Load the pre-cached notes dataset
    df = load_market_notes(current_market)
    if df.empty:
        return df, False

    # 2. Apply Date and Context Filters In-Memory
    if start_date_str:
        start_dt = pd.to_datetime(start_date_str)
        df = df[df['dt'] >= start_dt]

    if context_option != "All":
        df = df[df['context'] == context_option]

    # 3. Apply Text Search (Vectorized Pandas)
    if not df.empty:
        is_spp_outage = (outage_market.lower() == "spp")
        mask = pd.Series(False, index=df.index)

        for term in search_fields:
            term_clean = term.strip()
            if not term_clean:
                continue

            if is_spp_outage:
                # Fast literal substring matching (no regex overhead)
                mask |= df['body'].str.contains(term_clean, case=False, na=False, regex=False)
            else:
                # Regex boundary matching
                regex_pattern = format_regex_pattern(term_clean)
                mask |= df['body'].str.contains(regex_pattern, case=False, na=False, regex=True)

        df_matches = df[mask].copy()
    else:
        df_matches = df.copy()

    was_limited = len(df_matches) > result_limit

    # Sort and apply limit
    df_matches = df_matches.sort_values(by='dt', ascending=False)
    if was_limited:
        df_matches = df_matches.head(result_limit)

    # 4. Fetch Targeted Shadow Prices via SQL
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


# --- SIDEBAR INPUTS ---

st.sidebar.header("Search Parameters")

current_market = st.sidebar.selectbox(
    "Current Market (Search & Shadows)",
    options=["miso", "spp", "pjm", "ercot", "caiso"],
    format_func=lambda x: x.upper(),
    help="Market where notes (qn.body), RT/DA shadow prices, and outage annotations are queried."
)

use_different_outage_market = st.sidebar.checkbox(
    "Outage Market differs from Current Market",
    value=False,
    help="Check this if the outage originated from a different market than the notes database being searched."
)

if use_different_outage_market:
    outage_market = st.sidebar.selectbox(
        "Outage Market (ID Construction)",
        options=["miso", "spp", "pjm", "ercot", "caiso"],
        index=["miso", "spp", "pjm", "ercot", "caiso"].index(current_market),
        format_func=lambda x: x.upper(),
        help="Market used to derive group associations, equipment IDs, and notation rules."
    )
else:
    outage_market = current_market

search_type = st.sidebar.radio(
    "Search Input Type",
    options=["Equipment ID", "Group ID", "Manual string"]
)

raw_input_id = st.sidebar.text_input(
    f"Enter {search_type}",
    value=""
)

st.sidebar.markdown("---")
st.sidebar.header("Filter Parameters")

context_option = st.sidebar.selectbox(
    "Context Type",
    options=["All", "rt_shadow", "rt_shadow_forecast", "da_shadow"],
    index=0,
    help="Filter notes by specific shadow price context."
)

lookback_option = st.sidebar.selectbox(
    "Date Lookback Period",
    options=["3 Years", "1 Month", "6 Months", "1 Year", "5 Years", "All Time"],
    index=0,
    help="Filter notes by date."
)

now = pd.Timestamp.now()
start_date = None

if lookback_option == "1 Month":
    start_date = now - pd.DateOffset(months=1)
elif lookback_option == "6 Months":
    start_date = now - pd.DateOffset(months=6)
elif lookback_option == "1 Year":
    start_date = now - pd.DateOffset(years=1)
elif lookback_option == "3 Years":
    start_date = now - pd.DateOffset(years=3)
# --- BUILD SEARCH FIELDS ---

search_fields = []
raw_eq_ids = []
raw_group_ids = []

if search_type == "Manual string":
    if raw_input_id:
        for term in raw_input_id.split(","):
            cleaned_term = re.sub(r'\s+', ' ', term.replace("\t", " ")).strip()
            if cleaned_term:
                search_fields.append(cleaned_term)

else:
    cleaned_num = clean_id(raw_input_id)

    if cleaned_num:
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
                st.warning(f"Could not fetch group associations or equipment members: {e}")
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
                    st.warning(f"Could not fetch MISO transmission/active outage details for SPP search: {e}")

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
                    st.warning(f"Could not fetch MISO IDC equipment names for PJM search: {e}")

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
                    try:
                        with engine.connect() as conn:
                            placeholders = ", ".join([f":e{i}" for i in range(len(raw_eq_ids))])
                            spp_query = text(f"""
                                SELECT ems_equipment_name
                                FROM spp_transmission_outages
                                WHERE outage_equipment_id IN ({placeholders})
                            """)
                            spp_params = {f"e{i}": eq for i, eq in enumerate(raw_eq_ids)}
                            spp_res = conn.execute(spp_query, spp_params).fetchall()

                            for row in spp_res:
                                if row[0] is not None and str(row[0]).strip():
                                    search_fields.append(str(row[0]).strip())
                    except Exception as e:
                        st.warning(f"Could not fetch SPP EMS equipment names: {e}")

# Deduplicate search fields
search_fields = list(dict.fromkeys(search_fields))

# Fetch Human Readable Outage Name(s)
outage_names = fetch_outage_names(search_type, raw_input_id, outage_market, tuple(raw_eq_ids))

# Outage Header
if outage_names:
    st.header(f"⚡ Outage: {', '.join(outage_names)}")

# Display Search Fields Section
st.subheader("📋 Search Fields")
if search_fields:
    csv_search_str = ", ".join(search_fields)
    st.info(
        f"**Searching in {current_market.upper()} for fields constructed via {outage_market.upper()} rules:** `{csv_search_str}` | "
        f"**Context:** `{context_option}` | **Lookback:** `{lookback_option}`"
    )
else:
    st.warning("Please enter a valid search value in the sidebar to generate search fields.")

# --- SEARCH EXECUTION & DIRECT MYSQL QUERY ---

if st.sidebar.button("Run Search", type="primary"):
    if not search_fields:
        st.error("No valid search fields generated. Please check your input.")
    else:
        # Prime the cache OUTSIDE the main spinner so the custom indicator displays
        load_market_notes(current_market)

        with st.spinner(
                f"Scanning {current_market.upper()} notes for {len(search_fields)} term(s) and fetching shadow prices..."):
            try:
                # 1. Fetch Outage Annotations & Transmission Outage Flags (cached)
                annotations = fetch_outage_annotations(tuple(raw_eq_ids), outage_market, current_market)
                raw_flag_notes = fetch_transmission_outage_flags(current_market, tuple(raw_eq_ids))

                # Deduplicate flag notes by their exact text body
                flag_notes = []
                seen_flag_texts = set()
                for flag in raw_flag_notes:
                    note_key = clean_body_text_for_raw(flag.get('notes', ''))
                    if note_key and note_key not in seen_flag_texts:
                        seen_flag_texts.add(note_key)
                        flag_notes.append(flag)

                annotations_by_me = {}
                for ann in annotations:
                    me_id_str = ann["official_monitored_element_id"]
                    if me_id_str not in annotations_by_me:
                        annotations_by_me[me_id_str] = []
                    annotations_by_me[me_id_str].append(ann)

                # Render Transmission Outage Flag Notes box if present (Non-PJM)
                if current_market.lower() != "pjm" and flag_notes:
                    st.subheader("Equipment Flag Notes")
                    for flag in flag_notes:
                        eq_id = flag['outage_id']
                        f_notes = clean_annotation_notes(flag['notes'])
                        f_notes_escaped = html.escape(f_notes)
                        f_notes_rendered = render_links_only(f_notes_escaped)
                        f_updated = flag.get('updated_at', '')

                        # Generate dynamic SVG flag icon based on flag_type (alert=red, watch=orange, safe=green)
                        flag_svg_icon = get_flag_icon_svg(flag.get('flag_type', ''))

                        last_update_badge = f'<span style="font-size: 0.88em; font-weight: normal; color: #856404; margin-left: 12px;">(Last Update: {f_updated})</span>' if f_updated else ''

                        st.markdown(
                            f"""
                            <div style="background-color: #fff3cd; border: 1px solid #ffeeba; border-left: 5px solid #ffc107; border-radius: 6px; padding: 14px 18px; margin-bottom: 14px;">
                                <div style="color: #856404; font-weight: bold; font-size: 1.05em; margin-bottom: 8px;">
                                    {flag_svg_icon}FLAG NOTE: Equipment ID {eq_id} {last_update_badge}
                                </div>
                                <div style="color: #212529; white-space: pre-wrap; line-height: 1.5;">{f_notes_rendered}</div>
                            </div>
                            """,
                            unsafe_allow_html=True
                        )

                # 2. Execute main search query with caching and result limiting
                start_date_str = start_date.strftime("%Y-%m-%d") if start_date is not None else None
                df, was_limited = execute_main_search_query(
                    current_market,
                    tuple(search_fields),
                    context_option,
                    start_date_str,
                    result_limit=2000
                )

                # 3. Process and group monitored elements
                me_groups = []
                me_ids_in_df = set()

                if not df.empty:
                    grouped = df.groupby(['official_monitored_element_id', 'monitored_element_name'], sort=False)

                    for (me_id, me_name), group_df in grouped:
                        me_id_str = str(me_id)
                        me_ids_in_df.add(me_id_str)
                        sorted_group_df = group_df.sort_values(by='dt', ascending=False)
                        max_dt = sorted_group_df['dt'].max()

                        sum_shadow = pd.to_numeric(sorted_group_df['total_shadow_price'], errors='coerce').fillna(
                            0).sum()

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

                result_msg = f"Found {len(df)} matching quick note record(s) across {len(me_groups)} monitored element(s) in {current_market.upper()} via Direct MySQL 🛢️."
                if was_limited:
                    result_msg += " ⚠️ Results limited to 2000 rows for performance. Refine your search to see more results."
                st.success(result_msg)

                if me_groups:
                    me_groups.sort(key=lambda x: (x['sum_shadow'], str(x['max_dt'])), reverse=True)

                    matched_me_ids = [g['me_id'] for g in me_groups if g['me_id']]
                    meta_descriptions = fetch_monelem_meta_descriptions(current_market, tuple(matched_me_ids))

                    # --- SIDEBAR: CONSOLIDATED COPY ALL LATEST RT SHADOW NOTES ---
                    all_rt_snippets = []
                    for g in me_groups:
                        rt_shadow_df = g['group_df'][g['group_df']['context'] == 'rt_shadow']
                        if not rt_shadow_df.empty:
                            latest_row = rt_shadow_df.iloc[0]
                            raw_body = latest_row['body']
                            note_dt = latest_row['dt']

                            matching_sentence = extract_matching_sentence(raw_body, search_fields)

                            if matching_sentence:
                                me_id = str(g['me_id'])
                                me_name = str(g['me_name'])
                                me_url = f"https://energycore.tioscapital.com/{current_market.lower()}/monitored_elements/{me_id}"
                                header_label = f"{current_market.upper()} ME {me_id} {me_name}"
                                markdown_link = f"[{header_label}]({me_url})"

                                date_str = pd.to_datetime(note_dt).strftime("%Y-%m-%d") if pd.notna(note_dt) else ""

                                snippet_line = f"*({date_str})* | **{markdown_link}**: {matching_sentence}"
                                all_rt_snippets.append(snippet_line)

                    with st.sidebar:
                        st.markdown("---")
                        st.subheader("📋 RT Shadow Snippets")
                        if all_rt_snippets:
                            st.caption("Click top-right icon below to copy matching RT notes:")
                            st.code("\n\n".join(all_rt_snippets), language="markdown")
                        else:
                            st.info("No matching RT Shadow notes found.")

                    # --- MAIN PANEL DATA TABS ---
                    tab_titles = [f"ME {g['me_id']} - {g['me_name'][:30]}..." if len(
                        str(g['me_name'])) > 30 else f"ME {g['me_id']} - {g['me_name']}" for g in me_groups]
                    tabs = st.tabs(tab_titles)

                    # Build URL parameters for annotation search links based on search context & cross-market rules
                    ann_group_param = ""
                    ann_outage_param = ""
                    cleaned_search_id = clean_id(raw_input_id)
                    is_cross_market = current_market.lower() != outage_market.lower()

                    if search_type == "Group ID" and cleaned_search_id:
                        if is_cross_market:
                            ann_group_param = f"{outage_market.lower()}:g:{cleaned_search_id}"
                        else:
                            ann_group_param = f"g:{cleaned_search_id}"

                    elif search_type == "Equipment ID" and cleaned_search_id:
                        if is_cross_market:
                            ann_outage_param = f"{outage_market.lower()}:{cleaned_search_id}"
                        else:
                            ann_outage_param = cleaned_search_id

                    for tab, g in zip(tabs, me_groups):
                        with tab:
                            me_id = str(g['me_id'])
                            me_name = str(g['me_name'])
                            me_url = f"https://energycore.tioscapital.com/{current_market.lower()}/monitored_elements/{me_id}"
                            header_label = f"{current_market.upper()} ME {me_id} {me_name}"

                            st.markdown(f"### [{header_label}]({me_url})")

                            if g.get('annotations'):
                                for ann in g['annotations']:
                                    ann_name = html.escape(ann.get('name', ''))
                                    ann_notes_cleaned = clean_annotation_notes(ann.get('notes', ''))
                                    ann_notes_escaped = html.escape(ann_notes_cleaned)
                                    ann_notes_rendered = render_links_only(ann_notes_escaped)
                                    ann_updated = ann.get('updated_at', '')

                                    # Build hyperlink for annotation title pointing to current market
                                    ann_search_url = (
                                        f"https://energycore.tioscapital.com/{current_market.lower()}/outage_annotations?"
                                        f"annotation_name=&annotation_notes=&monelem_name=&monelem_id={me_id}&"
                                        f"outage_group_id={ann_group_param}&outage_id={ann_outage_param}&"
                                        f"outage_request_id=&commit=Search%21"
                                    )
                                    ann_title_link = f'<a href="{ann_search_url}" target="_blank" style="color: #721c24; text-decoration: underline;">OUTAGE ANNOTATION: {ann_name}</a>'

                                    last_update_badge = f'<span style="font-size: 0.88em; font-weight: normal; color: #842029; margin-left: 12px;">(Last Update: {ann_updated})</span>' if ann_updated else ''

                                    st.markdown(
                                        f"""
                                        <div style="background-color: #fdf2f2; border: 1px solid #f5c6cb; border-left: 5px solid #d9534f; border-radius: 6px; padding: 14px 18px; margin-bottom: 18px;">
                                            <div style="color: #721c24; font-weight: bold; font-size: 1.05em; margin-bottom: 8px;">
                                                🚨 {ann_title_link} {last_update_badge}
                                            </div>
                                            <div style="color: #212529; white-space: pre-wrap; line-height: 1.5;"><b>Notes:</b> {ann_notes_rendered}</div>
                                        </div>
                                        """,
                                        unsafe_allow_html=True
                                    )

                            if not g['group_df'].empty:
                                sub_df = g['group_df'][['dt', 'context', 'body', 'total_shadow_price']].copy()

                                sub_df['body'] = sub_df['body'].apply(clean_body_text_for_display)
                                sub_df['body'] = sub_df['body'].apply(lambda x: highlight_matches(x, search_fields))
                                sub_df['body'] = sub_df['body'].apply(render_links_only)

                                html_table = sub_df.to_html(escape=False, index=False)
                                st.markdown(
                                    f'<div style="overflow-x: auto; max-height: 600px; border: 1px solid #e6e6e6; border-radius: 5px; padding: 10px;">{html_table}</div>',
                                    unsafe_allow_html=True
                                )
                            else:
                                st.caption("No matching quick notes found for this monitored element.")

                    # --- COPY FULL OUTPUT TO CLIPBOARD SECTION ---
                    st.subheader("📋 Copy Full Output to Clipboard")
                    st.caption(
                        "Click the copy icon at the top-right of the box below to copy all rows across all tabs directly (formatted for Excel / Sequel Ace):")

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

                    tsv_data = "\n".join(clipboard_parts)
                    st.code(tsv_data, language="text")

                # Prepare CSV download copy
                df_download = df.copy() if not df.empty else pd.DataFrame()
                if not df_download.empty:
                    df_download['body'] = df_download['body'].apply(clean_body_text_for_raw)
                csv = df_download.to_csv(index=False).encode('utf-8')

                st.download_button(
                    label="Download CSV File",
                    data=csv,
                    file_name=f"{current_market}_outage_results.csv",
                    mime="text/csv"
                )

            except Exception as e:
                st.error(f"Error executing query: {e}")

