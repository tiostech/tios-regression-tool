"""FID (forward impact decomposition) assessment runs: the run index in MySQL and the result
parquets on S3, read the way EnergyCore's FID Assessment Results page reads them.

Used by ``mcp_server.py``. Nothing here imports Streamlit.

Where the data is (tios-core ``Fid::Assessment``):

- ``{market}_fid_runs``: one row per run (ercot, miso, pjm, spp). Scheduled runs start three
  times a day (about 00:00, 10:00 and 14:30 Central) and assess the curated list plus elements
  picked by RT/DA/forecast shadow thresholds and annotated outages starting
  (``Fid::Assessment::Selection``), over a window of a few days back and forward. ``started_at``
  is UTC; ``market_date`` is the market day the run started on. A run still ``running`` more
  than ``STALE_AFTER`` after it started died without recording its outcome (the page calls it
  "incomplete").
- ``{market}_fid_run_monelems``: one row per monitored element a run wrote, with the SE case
  it was assessed against and why it was picked.
- ``s3://tios-fid-production/fid-assessment/{market}/{run_id}/fid_result/
  constraint_fid_monelem_{me_id}.parquet``: the results (``Fid::Assessment::Catalog``). One row
  per (constraint x contingency x outage facility x day): the facility's impact on the
  constraint (``impact_mw``/``impact_percentage``, fixed per facility), whether it is out that
  day (``active``), and the day's total impact of every outage and post-outage flow against
  ``rating_b``. Read with the default AWS credential chain; the bucket is not readable by most
  personal IAM users.
"""

import concurrent.futures
import datetime
import io

import numpy as np
import pandas as pd
from sqlalchemy import text

FID_MARKETS = ["ercot", "miso", "pjm", "spp"]
BUCKET = "tios-fid-production"
PREFIX = "fid-assessment"

# Fid::Assessment::Runner::DEFAULT_TIME_LIMIT (4 hours) + 1 hour, as AnyMarketFidRunManager.
STALE_AFTER = datetime.timedelta(hours=5)

# Fid::Visualization: a facility earns a row on a constraint with an impact of at least this
# (percent of rating, either way).
SHOW_IMPACT_PCT = 1.0

REASON_LABELS = {"curated": "Always Run list", "rt_shadow": "RT threshold", "worried_excited": "Annotated outage starting",
                 "forecast_shadow": "Forecast threshold", "da_shadow": "DA threshold"}

NUMERIC_COLUMNS = ["total_impact_mw", "total_impact_percentage", "post_outage_flow_mw", "impact_mw",
                   "impact_percentage", "branch_flow_mw"]

S3_WORKERS = 8


class S3Unavailable(Exception):
    """The result parquets could not be read: no AWS credentials, or no access to the bucket."""


def _check_market(market: str) -> None:
    if market not in FID_MARKETS:
        raise ValueError(f"FID runs exist for {', '.join(FID_MARKETS)}, not '{market}'.")


def _in(prefix: str, values) -> tuple[str, dict]:
    values = list(values)
    return ", ".join(f":{prefix}{i}" for i in range(len(values))), {f"{prefix}{i}": v for i, v in enumerate(values)}


# --- Run index (MySQL) -----------------------------------------------------------------------

def status_label(status: str, started_at, now: datetime.datetime | None = None) -> str:
    """The page's label: complete, failed, in progress, or incomplete (still 'running' but past
    STALE_AFTER, so the process died)."""
    now = now or datetime.datetime.utcnow()
    if status == "failed":
        return "failed"
    if status == "running":
        return "incomplete" if started_at < now - STALE_AFTER else "in progress"
    return "complete"


def runs(engine, market: str, start_date: datetime.date, end_date: datetime.date) -> pd.DataFrame:
    """Every run whose market_date is in the range, newest first, with ``label`` and the number
    of elements it has written so far (``monelems``; a run in progress writes as it goes)."""
    _check_market(market)
    with engine.connect() as conn:
        df = pd.read_sql(text(f"""
            SELECT r.run_id, r.started_at, r.finished_at, r.market_date, r.kind, r.status, r.start_dt, r.end_dt,
                   r.se_case, r.requested_count, r.written_count, r.error_count, r.timed_out, r.failure,
                   r.created_by, (SELECT COUNT(*) FROM {market}_fid_run_monelems m WHERE m.run_id = r.run_id) AS monelems
            FROM {market}_fid_runs r
            WHERE r.market_date BETWEEN :s AND :e
            ORDER BY r.started_at DESC
        """), conn, params={"s": start_date, "e": end_date})
    now = datetime.datetime.utcnow()
    df["label"] = [status_label(s, t, now) for s, t in zip(df["status"], df["started_at"])]
    return df


def run(engine, market: str, run_id: str) -> dict | None:
    _check_market(market)
    with engine.connect() as conn:
        row = conn.execute(text(f"SELECT * FROM {market}_fid_runs WHERE run_id = :r"), {"r": run_id}).mappings().first()
    if row is None:
        return None
    row = dict(row)
    row["label"] = status_label(row["status"], row["started_at"])
    return row


def latest_run(engine, market: str, kind: str | None = "scheduled", before=None) -> dict | None:
    """The newest complete run (of ``kind``, unless None), optionally started before ``before``."""
    _check_market(market)
    where, params = ["status = 'complete'"], {}
    if kind:
        where.append("kind = :k")
        params["k"] = kind
    if before is not None:
        where.append("started_at < :b")
        params["b"] = before
    with engine.connect() as conn:
        row = conn.execute(text(f"""
            SELECT * FROM {market}_fid_runs WHERE {" AND ".join(where)} ORDER BY started_at DESC LIMIT 1
        """), params).mappings().first()
    if row is None:
        return None
    row = dict(row)
    row["label"] = status_label(row["status"], row["started_at"])
    return row


def run_monelems(engine, market: str, run_id: str) -> pd.DataFrame:
    """Elements a run wrote: me_id, se_case, se_case_source, reasons (list)."""
    _check_market(market)
    with engine.connect() as conn:
        df = pd.read_sql(text(f"""
            SELECT official_monitored_element_id AS me_id, se_case, se_case_source, selection_reasons
            FROM {market}_fid_run_monelems WHERE run_id = :r ORDER BY official_monitored_element_id
        """), conn, params={"r": run_id})
    df["reasons"] = [str(r or "").split() for r in df["selection_reasons"]]
    return df


def previous_runs_for(engine, market: str, me_ids, before) -> pd.DataFrame:
    """For each element, the newest complete run that wrote it and started before ``before``:
    me_id, run_id, started_at, se_case, kind. Elements no earlier run wrote are missing."""
    _check_market(market)
    ids = sorted({int(i) for i in me_ids})
    if not ids:
        return pd.DataFrame(columns=["me_id", "run_id", "started_at", "se_case", "kind"])
    placeholders, params = _in("m", ids)
    params["b"] = before
    # Two weeks back is plenty (runs are thrice daily) and keeps the scan on the (me, started_at) index.
    params["since"] = before - datetime.timedelta(days=14)
    with engine.connect() as conn:
        df = pd.read_sql(text(f"""
            SELECT m.official_monitored_element_id AS me_id, m.run_id, m.started_at, m.se_case, r.kind
            FROM {market}_fid_run_monelems m
            JOIN {market}_fid_runs r ON r.run_id = m.run_id AND r.status = 'complete'
            WHERE m.official_monitored_element_id IN ({placeholders})
              AND m.started_at < :b AND m.started_at >= :since
            ORDER BY m.started_at DESC
        """), conn, params=params)
    return df.drop_duplicates("me_id")


def rt_shadow_totals(engine, market: str, me_ids, end: datetime.date, days: int = 14) -> dict[int, float]:
    """{me_id: RT shadow price total} over the ``days`` market days ending on ``end``, the
    lookback the page orders constraint tabs by."""
    ids = sorted({int(i) for i in me_ids})
    if not ids:
        return {}
    placeholders, params = _in("m", ids)
    params.update({"s": end - datetime.timedelta(days=days - 1), "e": end})
    with engine.connect() as conn:
        rows = conn.execute(text(f"""
            SELECT official_monitored_element_id, SUM(shadow_price)
            FROM {market}_rt_constraint_shadow_prices
            WHERE official_monitored_element_id IN ({placeholders}) AND dt BETWEEN :s AND :e
            GROUP BY official_monitored_element_id
        """), params).fetchall()
    return {int(m): float(t or 0) for m, t in rows}


def compare_run_index(current: pd.DataFrame, previous: pd.DataFrame) -> dict:
    """Elements added to and dropped from a run compared with an earlier one, and SE case
    changes for the elements both assessed."""
    cur = current.set_index("me_id")
    prev = previous.set_index("me_id")
    both = cur.index.intersection(prev.index)
    changed = [(int(m), prev.at[m, "se_case"], cur.at[m, "se_case"]) for m in both
               if (prev.at[m, "se_case"] or "") != (cur.at[m, "se_case"] or "")]
    return {"added": sorted(int(m) for m in cur.index.difference(prev.index)),
            "dropped": sorted(int(m) for m in prev.index.difference(cur.index)),
            "case_changed": changed}


# --- Result parquets (S3) --------------------------------------------------------------------

def monelem_key(market: str, run_id: str, me_id: int) -> str:
    return f"{PREFIX}/{market}/{run_id}/fid_result/constraint_fid_monelem_{int(me_id)}.parquet"


def s3_client():
    """A boto3 S3 client from the default credential chain (env vars, ~/.aws, SSO, instance role)."""
    try:
        import boto3
        import botocore
    except ImportError as e:
        raise S3Unavailable("boto3 is not installed, so FID results on S3 cannot be read.") from e
    session = boto3.session.Session()
    if session.get_credentials() is None:
        raise S3Unavailable(f"No AWS credentials found, so FID results in s3://{BUCKET} cannot be read. "
                            "Configure AWS credentials with read access to that bucket (aws configure).")
    return session.client("s3", region_name=session.region_name or "us-east-1",
                          config=botocore.config.Config(max_pool_connections=S3_WORKERS * 2))


def read_monelem_frame(client, market: str, run_id: str, me_id: int) -> pd.DataFrame | None:
    """One element's result frame, cleaned as Fid::Reader does; None when the run has no file
    for it. Raises S3Unavailable on an access problem."""
    import botocore.exceptions
    key = monelem_key(market, run_id, me_id)
    try:
        body = client.get_object(Bucket=BUCKET, Key=key)["Body"].read()
    except botocore.exceptions.ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404", "NotFound"):
            return None
        if code in ("AccessDenied", "403", "Forbidden", "InvalidAccessKeyId", "ExpiredToken", "SignatureDoesNotMatch"):
            raise S3Unavailable(f"AWS refused to read s3://{BUCKET}/{key} ({code}). The AWS credentials on this "
                                f"machine need s3:GetObject on {BUCKET}.") from e
        raise
    except botocore.exceptions.NoCredentialsError as e:
        raise S3Unavailable("No AWS credentials found, so FID results on S3 cannot be read.") from e
    return clean(pd.read_parquet(io.BytesIO(body)))


def read_frames(client, market: str, pairs) -> dict:
    """{(run_id, me_id): frame or None} for (run_id, me_id) pairs, read in parallel. Raises
    S3Unavailable on the first access problem."""
    pairs = list(dict.fromkeys(pairs))
    out = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=S3_WORKERS) as pool:
        futures = {pool.submit(read_monelem_frame, client, market, r, m): (r, m) for r, m in pairs}
        for f in concurrent.futures.as_completed(futures):
            out[futures[f]] = f.result()
    return out


def clean(raw: pd.DataFrame) -> pd.DataFrame:
    """Fid::Reader.clean: sort by day, numeric columns as floats, branch flow to 2 places, and
    drop any row with a null in any column (R's na.omit, kept by the page on purpose)."""
    df = raw.sort_values("dt", kind="stable").copy()
    for c in NUMERIC_COLUMNS:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
    if "branch_flow_mw" in df.columns:
        df["branch_flow_mw"] = df["branch_flow_mw"].round(2)
    df = df.dropna().reset_index(drop=True)
    df["dt"] = pd.to_datetime(df["dt"]).dt.date
    return df


# --- Analysis --------------------------------------------------------------------------------

def facility_impacts(frame: pd.DataFrame) -> pd.DataFrame:
    """One row per (constraint, iso, facility): the facility's impact picked as
    Fid::OutageAnalysis.impact_source picks it (largest |impact_percentage|, loading ahead of
    shielding on a tie, then the ids) and the days it is out."""
    if frame.empty:
        return pd.DataFrame(columns=["official_constraint_id", "iso", "facility", "impact_pct", "impact_mw", "out_days"])
    f = frame.assign(_abs=frame["impact_percentage"].abs())
    f = f.sort_values(["official_constraint_id", "iso", "facility", "_abs", "impact_percentage", "contingency_uid"],
                      ascending=[True, True, True, False, False, True], kind="stable")
    picked = f.drop_duplicates(["official_constraint_id", "iso", "facility"])[
        ["official_constraint_id", "iso", "facility", "impact_percentage", "impact_mw"]]
    keys = ["official_constraint_id", "iso", "facility"]
    out = (frame.loc[frame["active"] == 1, keys + ["dt"]].drop_duplicates().sort_values("dt")
           .groupby(keys)["dt"].agg(list).rename("out_days").reset_index())
    picked = picked.merge(out, on=["official_constraint_id", "iso", "facility"], how="left")
    picked["out_days"] = [d if isinstance(d, list) else [] for d in picked["out_days"]]
    return picked.rename(columns={"impact_percentage": "impact_pct"}).reset_index(drop=True)


def constraint_days(frame: pd.DataFrame) -> pd.DataFrame:
    """One row per (constraint, day): total outage impact and post-outage loading as % of the
    element's rating (the Constraint Impact Over Time chart), plus the base-case flow."""
    if frame.empty:
        return pd.DataFrame(columns=["official_constraint_id", "dt", "total_impact_pct", "total_impact_mw",
                                     "post_outage_flow_mw", "rating_b", "loading_pct", "base_pct"])
    rating = frame.groupby("dt")["rating_b"].max().rename("rating_b")
    days = (frame.groupby(["official_constraint_id", "dt"])
            .agg(total_impact_pct=("total_impact_percentage", "max"), total_impact_mw=("total_impact_mw", "max"),
                 post_outage_flow_mw=("post_outage_flow_mw", "max"), base_mw=("branch_flow_mw", "max"))
            .reset_index().merge(rating, on="dt", how="left"))
    ok = days["rating_b"] > 0
    days["loading_pct"] = np.where(ok, days["post_outage_flow_mw"] / days["rating_b"].where(ok) * 100, np.nan)
    days["base_pct"] = np.where(ok, days["base_mw"] / days["rating_b"].where(ok) * 100, np.nan)
    return days


def summarize(frame: pd.DataFrame, from_day: datetime.date) -> dict | None:
    """The element's forward picture from ``from_day`` on: per constraint the peak post-outage
    loading and its day, and the outages (|impact| >= SHOW_IMPACT_PCT) out on those days."""
    if frame is None or frame.empty:
        return None
    days = constraint_days(frame)
    fwd = days[days["dt"] >= from_day]
    if fwd.empty:
        return {"constraints": pd.DataFrame(), "facilities": facility_impacts(frame), "days": days, "forward_days": []}
    idx = fwd.groupby("official_constraint_id")["loading_pct"].idxmax().dropna()
    peaks = fwd.loc[idx, ["official_constraint_id", "dt", "loading_pct", "total_impact_pct", "base_pct", "rating_b"]]
    peaks = peaks.rename(columns={"dt": "peak_day", "loading_pct": "peak_loading_pct",
                                  "total_impact_pct": "impact_on_peak_day_pct"})
    peaks["max_total_impact_pct"] = peaks["official_constraint_id"].map(
        fwd.groupby("official_constraint_id")["total_impact_pct"].max())
    peaks = peaks.sort_values("peak_loading_pct", ascending=False).reset_index(drop=True)
    fac = facility_impacts(frame)
    fac["fwd_out_days"] = [[d for d in o if d >= from_day] for o in fac["out_days"]]
    return {"constraints": peaks, "facilities": fac, "days": days,
            "forward_days": sorted(set(fwd["dt"]))}


def top_outages(summary: dict, constraint_id, n: int = 5, forward_only: bool = True) -> pd.DataFrame:
    """The constraint's largest outages by |impact|, shown ones only, out in the forward window
    when ``forward_only``."""
    fac = summary["facilities"]
    f = fac[(fac["official_constraint_id"] == constraint_id) & (fac["impact_pct"].abs() >= SHOW_IMPACT_PCT)]
    if forward_only:
        f = f[f["fwd_out_days"].map(bool)]
    return f.reindex(f["impact_pct"].abs().sort_values(ascending=False).index).head(n)


def compare(new: dict | None, old: dict | None, change_pct: float = 2.0) -> dict:
    """What changed for one element between two runs, over the forward days both cover.

    - constraints: per constraint, peak loading in each run over the common days and the change
      (new or gone constraints included).
    - facilities: outages that started or stopped mattering (|impact| crossing
      SHOW_IMPACT_PCT), whose impact moved by at least ``change_pct`` points, or whose out days
      in the common window changed (rescheduled, newly out, back in service).
    """
    empty = {"constraints": pd.DataFrame(), "facilities": pd.DataFrame(), "common_days": []}
    if new is None or old is None:
        return empty
    common = sorted(set(new["forward_days"]) & set(old["forward_days"]))
    if not common:
        return empty

    def peaks(s):
        d = s["days"][s["days"]["dt"].isin(common)]
        return d.groupby("official_constraint_id")["loading_pct"].max()

    pn, po = peaks(new), peaks(old)
    cons = pd.DataFrame({"new_peak_pct": pn, "old_peak_pct": po}).reset_index().rename(
        columns={"index": "official_constraint_id"})
    cons["change_pct"] = cons["new_peak_pct"] - cons["old_peak_pct"]
    cons = cons.reindex(cons["change_pct"].abs().fillna(np.inf).sort_values(ascending=False).index).reset_index(drop=True)

    key = ["official_constraint_id", "iso", "facility"]
    fn = new["facilities"].copy()
    fo = old["facilities"].copy()
    common_set = set(common)
    for f in (fn, fo):
        f["common_out"] = [tuple(d for d in o if d in common_set) for o in f["out_days"]]
    m = fn.merge(fo, on=key, how="outer", suffixes=("_new", "_old"))
    rows = []
    for r in m.itertuples(index=False):
        ni, oi = r.impact_pct_new, r.impact_pct_old
        n_show = pd.notna(ni) and abs(ni) >= SHOW_IMPACT_PCT
        o_show = pd.notna(oi) and abs(oi) >= SHOW_IMPACT_PCT
        if not (n_show or o_show):
            continue
        n_out = r.common_out_new if isinstance(r.common_out_new, tuple) else ()
        o_out = r.common_out_old if isinstance(r.common_out_old, tuple) else ()
        what = []
        if n_show and not o_show:
            what.append("new" if pd.isna(oi) else "now matters")
        elif o_show and not n_show:
            what.append("gone" if pd.isna(ni) else "no longer matters")
        elif abs(ni - oi) >= change_pct:
            what.append("impact changed")
        if n_out != o_out and (n_show or o_show):
            if n_out and not o_out:
                what.append("now out " + _days(n_out))
            elif o_out and not n_out:
                what.append("no longer out " + _days(o_out))
            else:
                what.append(f"out days {_days(o_out)} -> {_days(n_out)}")
        if what:
            rows.append({"official_constraint_id": r.official_constraint_id, "iso": r.iso, "facility": r.facility,
                         "old_impact_pct": oi, "new_impact_pct": ni, "change": "; ".join(what),
                         "_rank": max(abs(ni) if pd.notna(ni) else 0, abs(oi) if pd.notna(oi) else 0)})
    fac = pd.DataFrame(rows)
    if not fac.empty:
        fac = fac.sort_values("_rank", ascending=False).drop(columns="_rank").reset_index(drop=True)
    return {"constraints": cons, "facilities": fac, "common_days": common}


def _days(days) -> str:
    return ",".join(d.strftime("%m-%d") for d in days) if days else "-"
