import re
import html
import streamlit as st
import pandas as pd
from lib import db
from lib import outage_search as osearch
from lib.outage_search import clean_id, clean_body_text_for_raw, clean_annotation_notes

st.set_page_config(page_title="Market Outage Search", layout="wide")
st.title("⚡ Power Market Outage & Shadow Price Search")

if not db.gate("1. Database"):
    st.stop()

engine = db.engine()

# --- DISPLAY HELPERS ---
# The search and formatting logic lives in lib/outage_search.py so the MCP server
# (mcp_server.py) produces exactly the same results as this page.

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
        pattern = re.compile(f"({osearch.format_regex_pattern(term)})", re.IGNORECASE)
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


# --- CACHED DATABASE QUERIES ---

@st.cache_data(ttl=3600)
def fetch_transmission_outage_flags(current_mkt: str, eq_ids_tuple: tuple) -> list[dict]:
    """Fetches the most recent transmission outage flag notes for non-PJM markets."""
    return osearch.fetch_transmission_outage_flags(engine, current_mkt, eq_ids_tuple, warn=st.warning)


@st.cache_data(ttl=3600)
def fetch_outage_annotations(eq_ids_tuple: tuple, outage_mkt: str, current_mkt: str) -> list[dict]:
    """Fetches outage annotations."""
    return osearch.fetch_outage_annotations(engine, eq_ids_tuple, outage_mkt, current_mkt, warn=st.warning)


@st.cache_data(ttl=3600)
def fetch_outage_names(search_type: str, raw_input_id: str, outage_mkt: str, raw_eq_ids_tuple: tuple) -> list[str]:
    """Fetches human-readable outage names."""
    return osearch.fetch_outage_names(engine, search_type, raw_input_id, outage_mkt, raw_eq_ids_tuple,
                                      warn=st.warning)


@st.cache_data(ttl=3600)
def fetch_monelem_meta_descriptions(current_mkt: str, me_ids_tuple: tuple) -> dict[str, str]:
    """Fetches monitored element meta descriptions."""
    return osearch.fetch_monelem_meta_descriptions(engine, current_mkt, me_ids_tuple)


@st.cache_data(ttl=14400,
               show_spinner="📥 Initializing cache: Downloading market notes into memory (this may take a minute)...")
def load_market_notes(current_market: str) -> pd.DataFrame:
    """Pre-loads all notes and element names for the market into server RAM."""
    return osearch.load_market_notes(engine, current_market)


@st.cache_data(ttl=3600)
def execute_main_search_query(current_market: str, search_fields_tuple: tuple, context_option: str,
                              start_date_str: str, result_limit: int = 500) -> tuple[pd.DataFrame, bool]:
    """Executes an in-memory Pandas search, then fetches SQL shadow prices only for matched rows."""
    return osearch.execute_main_search_query(engine, load_market_notes(current_market), current_market,
                                             search_fields_tuple, context_option, start_date_str,
                                             result_limit=result_limit)


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
start_date = osearch.lookback_start_date(lookback_option, now)

# --- BUILD SEARCH FIELDS ---

search_fields, raw_eq_ids, raw_group_ids = osearch.build_search_fields(
    engine, search_type, raw_input_id, current_market, outage_market, warn=st.warning
)

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
                flag_notes = osearch.dedupe_flag_notes(raw_flag_notes)

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
                me_groups = osearch.group_by_monitored_element(engine, df, annotations, current_market)

                result_msg = f"Found {len(df)} matching quick note record(s) across {len(me_groups)} monitored element(s) in {current_market.upper()} via Direct MySQL 🛢️."
                if was_limited:
                    result_msg += " ⚠️ Results limited to 2000 rows for performance. Refine your search to see more results."
                st.success(result_msg)

                if me_groups:
                    matched_me_ids = [g['me_id'] for g in me_groups if g['me_id']]
                    meta_descriptions = fetch_monelem_meta_descriptions(current_market, tuple(matched_me_ids))

                    # --- SIDEBAR: CONSOLIDATED COPY LATEST SHADOW NOTES (RT -> FC -> DA) ---
                    all_snippets = osearch.shadow_snippets(me_groups, search_fields, current_market)

                    with st.sidebar:
                        st.markdown("---")
                        st.subheader("📋 Shadow Snippets")
                        if all_snippets:
                            st.caption("Click top-right icon below to copy matching notes (RT / FC / DA):")
                            st.code("\n\n".join(all_snippets), language="markdown")
                        else:
                            st.info("No matching Shadow notes found.")

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

                    tsv_data = osearch.clipboard_text(current_market, search_fields, outage_names, flag_notes,
                                                      me_groups, df, meta_descriptions)
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