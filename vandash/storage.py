"""SQLite persistence for history buckets.

One row per time bucket. Writes are one tiny upsert per bucket rollover
(~30s), so synchronous sqlite3 on the event loop is fine.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

COLUMNS = ("t", "w", "v", "a", "temp", "rh", "soc", "solar_wh", "alt_w", "ac_w")

SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    t    INTEGER PRIMARY KEY,  -- bucket start, unix seconds
    w    REAL,                 -- solar power, W
    v    REAL,                 -- battery voltage, V
    a    REAL,                 -- battery charging current, A
    temp REAL,                 -- cabin temperature, C
    rh   REAL,                 -- relative humidity, %
    soc  REAL,                 -- battery state of charge, %
    solar_wh REAL,             -- MPPT's yield-since-midnight counter, Wh
    alt_w REAL,                -- alternator (Orion) charge estimate, W
    ac_w REAL                  -- shore charger output, W
)
"""


class HistoryStore:
    def __init__(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute("PRAGMA journal_mode=WAL")
        # NORMAL is durable enough under WAL (recovers to the last checkpoint on
        # crash) and skips an fsync per commit — gentler on the SD card and safe
        # across a van ignition-off power cut.
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute(SCHEMA)
        # Additive migration for databases created before SoC history existed.
        columns = {row[1] for row in self._db.execute("PRAGMA table_info(history)")}
        if "soc" not in columns:
            self._db.execute("ALTER TABLE history ADD COLUMN soc REAL")
        if "solar_wh" not in columns:
            self._db.execute("ALTER TABLE history ADD COLUMN solar_wh REAL")
        if "alt_w" not in columns:
            self._db.execute("ALTER TABLE history ADD COLUMN alt_w REAL")
        if "ac_w" not in columns:
            self._db.execute("ALTER TABLE history ADD COLUMN ac_w REAL")
        self._db.commit()

    def load_since(self, ts: float) -> list[dict]:
        cursor = self._db.execute(
            f"SELECT {', '.join(COLUMNS)} FROM history WHERE t >= ? ORDER BY t",
            (int(ts),),
        )
        return [dict(zip(COLUMNS, row)) for row in cursor.fetchall()]

    def max_t(self) -> float | None:
        row = self._db.execute("SELECT MAX(t) FROM history").fetchone()
        return None if row is None or row[0] is None else float(row[0])

    def upsert(self, row: dict) -> None:
        self._db.execute(
            f"INSERT OR REPLACE INTO history ({', '.join(COLUMNS)}) "
            f"VALUES ({', '.join('?' * len(COLUMNS))})",
            tuple(row.get(c) for c in COLUMNS),
        )
        self._db.commit()

    def day_series(
        self, start_ts: float, end_ts: float, bucket: int = 300
    ) -> list[dict]:
        cursor = self._db.execute(
            "SELECT (t / ?) * ? AS tb, AVG(w), AVG(v), AVG(a), AVG(temp), AVG(rh), "
            "AVG(soc), MAX(solar_wh), AVG(alt_w), AVG(ac_w) "
            "FROM history WHERE t >= ? AND t < ? GROUP BY tb ORDER BY tb",
            (bucket, bucket, int(start_ts), int(end_ts)),
        )
        digits = {"w": 1, "v": 2, "a": 1, "temp": 1, "rh": 0, "soc": 1,
                  "solar_wh": 0, "alt_w": 1, "ac_w": 1}
        rows = []
        for raw in cursor.fetchall():
            row = dict(zip(COLUMNS, raw))
            for col, nd in digits.items():
                if row[col] is not None:
                    row[col] = round(row[col], nd)
            rows.append(row)
        return rows

    def daily_solar_max(self, start_ts: float, end_ts: float) -> dict[str, float]:
        """Maximum solar watts by local calendar date within a time range."""
        cursor = self._db.execute(
            "SELECT date(t, 'unixepoch', 'localtime') AS day, MAX(w) "
            "FROM history WHERE t >= ? AND t < ? AND w IS NOT NULL "
            "GROUP BY day ORDER BY day",
            (int(start_ts), int(end_ts)),
        )
        return {day: round(watts, 1) for day, watts in cursor.fetchall()}

    def daily_solar_stats(
        self, start_ts: float, end_ts: float, bucket_seconds: int
    ) -> dict[str, dict[str, float]]:
        """Peak watts and integrated kWh by local calendar date."""
        cursor = self._db.execute(
            "WITH samples AS ("
            " SELECT t, w, solar_wh, date(t, 'unixepoch', 'localtime') AS day,"
            " LEAD(t) OVER (ORDER BY t) AS next_t"
            " FROM history WHERE t >= ? AND t < ?"
            ") SELECT day, MAX(w),"
            " COALESCE(MAX(solar_wh) / 1000.0,"
            " COALESCE(SUM(CASE WHEN w IS NOT NULL THEN w * MIN(MAX(next_t - t, 0), ?) ELSE 0 END), 0)"
            " / 3600000.0)"
            " FROM samples GROUP BY day ORDER BY day",
            (int(start_ts), int(end_ts), bucket_seconds * 3),
        )
        return {
            day: {
                "w": round(max_watts, 1),
                "kwh": round(kwh, 3),
            }
            for day, max_watts, kwh in cursor.fetchall()
        }

    def close(self) -> None:
        self._db.close()
