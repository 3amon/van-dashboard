import os
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, time as datetime_time, timedelta

from vandash.storage import HistoryStore


class DailySolarMaxTest(unittest.TestCase):
    def test_returns_local_daily_maxima_and_skips_nulls(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(os.path.join(directory, "history.db"))
            first = date.today() - timedelta(days=1)
            start = datetime.combine(first, datetime_time.min).timestamp()
            store.upsert({"t": start + 60, "w": 25})
            store.upsert({"t": start + 120, "w": 90})
            store.upsert({"t": start + 180, "w": None})
            store.upsert({"t": start + 86400 + 60, "w": 40})

            result = store.daily_solar_max(start, start + 2 * 86400)

            self.assertEqual(result[first.isoformat()], 90)
            self.assertEqual(result[(first + timedelta(days=1)).isoformat()], 40)

            stats = store.daily_solar_stats(start, start + 2 * 86400, 30)
            self.assertEqual(stats[first.isoformat()]["w"], 90)
            self.assertEqual(stats[first.isoformat()]["kwh"], 0.002)
            self.assertEqual(stats[(first + timedelta(days=1)).isoformat()]["kwh"], 0.0)
            store.close()

    def test_adds_soc_column_to_existing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "history.db")
            db = sqlite3.connect(path)
            db.execute(
                "CREATE TABLE history (t INTEGER PRIMARY KEY, w REAL, v REAL, "
                "a REAL, temp REAL, rh REAL)"
            )
            db.execute("INSERT INTO history VALUES (1, 2, 3, 4, 5, 6)")
            db.commit()
            db.close()

            store = HistoryStore(path)
            self.assertIn("soc", store.load_since(0)[0])
            self.assertIn("solar_wh", store.load_since(0)[0])
            store.upsert({"t": 2, "soc": 88.5})
            self.assertEqual(store.load_since(2)[0]["soc"], 88.5)
            store.close()

    def test_prefers_victron_daily_yield_counter(self):
        with tempfile.TemporaryDirectory() as directory:
            store = HistoryStore(os.path.join(directory, "history.db"))
            start = datetime.combine(date.today(), datetime_time.min).timestamp()
            store.upsert({"t": start + 60, "w": 500, "solar_wh": 1200})
            store.upsert({"t": start + 90, "w": 600, "solar_wh": 2560})
            stats = store.daily_solar_stats(start, start + 86400, 30)
            self.assertEqual(stats[date.today().isoformat()]["kwh"], 2.56)
            store.close()


if __name__ == "__main__":
    unittest.main()
