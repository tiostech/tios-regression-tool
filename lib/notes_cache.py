"""A market's desk notes, kept on disk and brought up to date with only what changed.

``outage_search.load_market_notes`` reads every note of a market (MISO: 200k, about 20
seconds over the SSH tunnel). The MCP server used to do that for every market each time it
started -- every new Claude Desktop session -- and again every 4 hours. This keeps the raw
rows in ``<cache dir>/notes_<market>.parquet`` and, on each refresh, reads only the notes
whose ``updated_at`` moved since the newest one already held (about 0.2 seconds).

That is safe because EnergyCore only changes notes through ActiveRecord (create, update, and
"delete", which sets ``deleted = 1``), and each of those sets ``updated_at``. Against
anything else (a script deleting rows): the row count is compared with the database on
every refresh and vanished IDs are dropped, and the whole market is reloaded once a day.

``frame()`` returns exactly what ``load_market_notes`` returns (deleted notes left out,
element names joined), with rows in ID order.
"""

import json
import logging
import os
import tempfile
import time

import pandas as pd
from sqlalchemy import text

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
FULL_RELOAD_SECONDS = 24 * 60 * 60
# Re-read notes updated this long before the newest one held, in case rows committed out of
# updated_at order (a long transaction) were missed by the last read.
OVERLAP = pd.Timedelta(minutes=30)
COLUMNS = ["id", "official_monitored_element_id", "dt", "context", "body", "deleted", "updated_at"]


def cache_dir() -> str:
    path = os.environ.get("ENERGYCORE_MCP_CACHE_DIR") or os.path.join(os.path.expanduser("~"), ".cache",
                                                                       "energycore-outages")
    os.makedirs(path, exist_ok=True)
    return path


class MarketNotes:
    def __init__(self, engine, market: str, directory: str | None = None):
        self.engine = engine
        self.market = market
        self.directory = directory
        self.raw: pd.DataFrame | None = None
        self.full_loaded_at = 0.0  # epoch seconds of the last full read

    # --- storage ---

    def _paths(self) -> tuple[str, str]:
        d = self.directory or cache_dir()
        return os.path.join(d, f"notes_{self.market}.parquet"), os.path.join(d, f"notes_{self.market}.json")

    def _read_disk(self) -> bool:
        data_path, meta_path = self._paths()
        try:
            with open(meta_path) as f:
                meta = json.load(f)
            if meta.get("schema") != SCHEMA_VERSION or meta.get("market") != self.market:
                return False
            raw = pd.read_parquet(data_path)
            if list(raw.columns) != COLUMNS:
                return False
        except (OSError, ValueError, KeyError):
            return False
        except Exception:
            log.warning("Unreadable notes cache for %s; reloading", self.market, exc_info=True)
            return False
        self.raw, self.full_loaded_at = raw, float(meta.get("full_loaded_at", 0))
        return True

    def _write_disk(self) -> None:
        data_path, meta_path = self._paths()
        try:
            d = os.path.dirname(data_path)
            fd, tmp = tempfile.mkstemp(dir=d, suffix=".parquet.tmp")
            os.close(fd)
            self.raw.to_parquet(tmp, index=False)
            os.replace(tmp, data_path)
            fd, tmp = tempfile.mkstemp(dir=d, suffix=".json.tmp")
            with os.fdopen(fd, "w") as f:
                json.dump({"schema": SCHEMA_VERSION, "market": self.market, "full_loaded_at": self.full_loaded_at}, f)
            os.replace(tmp, meta_path)
        except Exception:
            log.warning("Could not write the notes cache for %s", self.market, exc_info=True)

    # --- database ---

    def _select(self, where: str = "", params: dict | None = None) -> pd.DataFrame:
        with self.engine.connect() as conn:
            df = pd.read_sql(text(f"""
                SELECT {", ".join(COLUMNS)} FROM {self.market}_monelem_quick_notes {where}
            """), conn, params=params or {})
        return self._typed(df)

    @staticmethod
    def _typed(df: pd.DataFrame) -> pd.DataFrame:
        df["dt"] = pd.to_datetime(df["dt"])
        df["updated_at"] = pd.to_datetime(df["updated_at"])
        return df

    def _full(self) -> None:
        self.raw = self._select().sort_values("id").reset_index(drop=True)
        self.full_loaded_at = time.time()

    def _delta(self) -> bool:
        """Merges notes changed since the newest held. True when anything changed."""
        since = self.raw["updated_at"].max() - OVERLAP if not self.raw.empty else pd.Timestamp("1970-01-01")
        changed = self._select("WHERE updated_at >= :since", {"since": since.to_pydatetime()})
        dirty = False
        if not changed.empty:
            held = self.raw.set_index("id")
            new = changed.set_index("id")
            same = new.index.isin(held.index)
            stamps_moved = (held.loc[new.index[same], "updated_at"].values != new.loc[same, "updated_at"].values).any()
            if (~same).any() or stamps_moved:
                held = pd.concat([held.drop(new.index[same]), new])
                self.raw = held.sort_index().reset_index()
                dirty = True
        with self.engine.connect() as conn:
            count = conn.execute(text(f"SELECT COUNT(*) FROM {self.market}_monelem_quick_notes")).scalar()
            if count != len(self.raw):
                ids = {r[0] for r in conn.execute(text(f"SELECT id FROM {self.market}_monelem_quick_notes"))}
                gone = ~self.raw["id"].isin(ids)
                if gone.any():
                    log.info("%s: %d notes no longer in the database", self.market, int(gone.sum()))
                    self.raw = self.raw[~gone].reset_index(drop=True)
                    dirty = True
                if len(self.raw) != len(ids):
                    log.info("%s: note count still differs after the update; reloading", self.market)
                    self._full()
                    dirty = True
        return dirty

    def _names(self) -> pd.DataFrame:
        with self.engine.connect() as conn:
            return pd.read_sql(text(f"""
                SELECT id AS official_monitored_element_id, monitored_element_name
                FROM {self.market}_official_monitored_elements
                WHERE id IN (SELECT DISTINCT official_monitored_element_id FROM {self.market}_monelem_quick_notes)
            """), conn)

    # --- public ---

    def refresh(self) -> pd.DataFrame:
        """Brings the notes up to date (reading the disk cache the first time) and returns frame()."""
        if self.raw is None and not self._read_disk():
            self._full()
            dirty = True
        elif time.time() - self.full_loaded_at > FULL_RELOAD_SECONDS:
            self._full()
            dirty = True
        else:
            dirty = self._delta()
        if dirty:
            self._write_disk()
        # Rebuilt every time: element names can change without any note changing, and they
        # are cheap to re-read.
        self._frame = self._build()
        return self._frame

    def _build(self) -> pd.DataFrame:
        live = self.raw[(self.raw["deleted"] == 0) | self.raw["deleted"].isna()]
        df = live.merge(self._names(), on="official_monitored_element_id", how="left")
        # A SQL LEFT JOIN gives None for elements without a name; pandas gives NaN.
        names = df["monitored_element_name"].astype(object)
        df["monitored_element_name"] = names.where(names.notna(), None)
        return df[["official_monitored_element_id", "monitored_element_name", "dt", "context", "body"]] \
            .reset_index(drop=True)
