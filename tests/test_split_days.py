"""Same-day return reopens the entry; merge_split_days joins existing splits."""
import importlib.util, os, sys, tempfile, unittest, types
from datetime import datetime, timedelta

HERE = os.path.dirname(__file__)
spec = importlib.util.spec_from_file_location(
    "tt_store", os.path.join(HERE, "..", "custom_components", "timetrack", "store.py"))
store_mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(store_mod)
Store = [v for k, v in vars(store_mod).items() if isinstance(v, type) and k.endswith("Store")][0]


class SplitDays(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db"); os.close(fd); os.remove(self.db)
        self.s = Store(self.db)

    def tearDown(self):
        os.remove(self.db)

    def _ins(self, client, ci, co, status="pending", desc=""):
        c = self.s._connect()
        cur = c.execute(
            "INSERT INTO time_entries (client_name, zone_name, clock_in, clock_out, raw_hours,"
            " rounded_hours, push_status, description) VALUES (?,?,?,?,?,?,?,?)",
            (client, "z", ci, co, 1, 1, status, desc))
        c.commit(); i = cur.lastrowid; c.close(); return i

    def _rows(self):
        c = self.s._connect(); r = [dict(x) for x in c.execute("SELECT * FROM time_entries ORDER BY id")]; c.close(); return r

    def test_return_same_day_reopens(self):
        a = self.s.clock_in("LATU", "z"); self.s.clock_out(a.id)
        b = self.s.clock_in("LATU", "z")
        self.assertEqual(a.id, b.id)
        self.assertIsNone(self._rows()[0]["clock_out"])

    def test_other_client_or_pushed_gets_new_entry(self):
        a = self.s.clock_in("LATU", "z"); self.s.clock_out(a.id)
        self.assertNotEqual(self.s.clock_in("OTHER", "z").id, a.id)
        c = self.s._connect(); c.execute("UPDATE time_entries SET clock_out=?, push_status='pushed' WHERE id=?",
                                         (datetime.now().isoformat(), a.id + 1)); c.commit(); c.close()
        p = self.s.clock_in("OTHER", "z")
        self.assertNotEqual(p.id, a.id + 1)

    def test_merge_oct30(self):
        k = self._ins("LATU", "2026-10-30T11:20:19", "2026-10-30T14:08:12", desc="a")
        self._ins("LATU", "2026-10-30T14:45:43", "2026-10-30T16:40:34", desc="b")
        self._ins("LATU", "2026-10-29T13:52:21", "2026-10-29T16:58:30")
        pushed = self._ins("LATU", "2026-10-28T09:00:00", "2026-10-28T10:00:00", status="pushed")
        self._ins("LATU", "2026-10-28T11:00:00", "2026-10-28T12:00:00")
        r = self.s.merge_split_days("2026-10-01", "2026-10-31", 15)
        self.assertEqual(r, {"merged_days": 1, "removed_entries": 1})
        e = {x["id"]: x for x in self._rows()}
        self.assertEqual(e[k]["clock_out"], "2026-10-30T16:40:34")
        self.assertEqual(e[k]["rounded_hours"], 5.5)
        self.assertEqual(e[k]["description"], "a; b")
        self.assertIn(pushed, e)
        self.assertEqual(len(e), 4)

    def test_merge_bad_range_is_noop(self):
        self._ins("LATU", "2026-10-30T11:00:00", "2026-10-30T12:00:00")
        self._ins("LATU", "2026-10-30T13:00:00", "2026-10-30T14:00:00")
        self.assertEqual(self.s.merge_split_days("2026-11-01", "2026-10-01")["merged_days"], 0)
        self.assertEqual(self.s.merge_split_days("garbage", "x")["merged_days"], 0)
        self.assertEqual(len(self._rows()), 2)


if __name__ == "__main__":
    unittest.main()
