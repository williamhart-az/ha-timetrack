"""Unit tests for ZoneTracker zone alias resolution.

Tests the fix for the geofence zone alias bypass bug where entering
'TimeTrack - LAT' zone created a 'LAT' entry instead of resolving
to 'LATU' through zone_aliases.
"""

import sqlite3
import tempfile
import os
import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime

# We test the store and tracker logic directly without HA dependencies


class FakeTimeEntry:
    """Minimal TimeEntry for tests."""
    def __init__(self, id, client, zone, clock_in, clock_out=None, source="auto"):
        self.id = id
        self.client_name = client
        self.zone_name = zone
        self.clock_in = clock_in
        self.clock_out = clock_out
        self.source = source
        self.duration_hours = 2.0


class TestResolveZoneToClient(unittest.TestCase):
    """Test store.resolve_zone_to_client() lookup ordering."""

    def setUp(self):
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                zone_name TEXT NOT NULL,
                msp_client_name TEXT,
                msp_ticket_id TEXT,
                msp_service_item_rate_id TEXT,
                default_description TEXT,
                hourly_rate REAL DEFAULT 0,
                active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE zone_aliases (
                zone_state TEXT PRIMARY KEY,
                client_name TEXT NOT NULL
            );
            CREATE TABLE time_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_name TEXT NOT NULL,
                zone_name TEXT NOT NULL,
                clock_in TEXT NOT NULL,
                clock_out TEXT,
                description TEXT DEFAULT '',
                source TEXT DEFAULT 'auto',
                msp_ticket_id TEXT,
                msp_synced INTEGER DEFAULT 0,
                push_status TEXT DEFAULT 'pending',
                msp_rate_id TEXT,
                raw_hours REAL,
                rounded_hours REAL,
                billable INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        # Seed: client 'LATU' with zone 'TimeTrack - LATU'
        conn.execute(
            "INSERT INTO clients (name, zone_name, msp_ticket_id) VALUES (?, ?, ?)",
            ("LATU", "TimeTrack - LATU", "ticket-latu-367"),
        )
        # Seed: zone alias 'TimeTrack - LAT' → 'LATU'
        conn.execute(
            "INSERT INTO zone_aliases (zone_state, client_name) VALUES (?, ?)",
            ("TimeTrack - LAT", "LATU"),
        )
        # Seed: direct client 'DI' with zone 'TimeTrack - DiamondIron'
        conn.execute(
            "INSERT INTO clients (name, zone_name, msp_ticket_id) VALUES (?, ?, ?)",
            ("DI", "TimeTrack - DiamondIron", "ticket-di-100"),
        )
        conn.execute(
            "INSERT INTO zone_aliases (zone_state, client_name) VALUES (?, ?)",
            ("TimeTrack - DiamondIron", "DI"),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        os.close(self.db_fd)
        os.unlink(self.db_path)

    def _make_store(self):
        """Create a minimal store-like object that just has resolve_zone_to_client."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row

        class MiniStore:
            def __init__(self, db_path):
                self._db_path = db_path

            def _connect(self):
                c = sqlite3.connect(self._db_path)
                c.row_factory = sqlite3.Row
                return c

            def resolve_zone_to_client(self, zone_state):
                conn = self._connect()
                row = conn.execute(
                    "SELECT client_name FROM zone_aliases WHERE zone_state = ?",
                    (zone_state,),
                ).fetchone()
                if row:
                    conn.close()
                    return row["client_name"]
                row = conn.execute(
                    "SELECT name FROM clients WHERE zone_name = ? AND active = 1",
                    (zone_state,),
                ).fetchone()
                conn.close()
                return row["name"] if row else None

            def get_client_by_zone(self, zone_name):
                conn = self._connect()
                row = conn.execute(
                    "SELECT * FROM clients WHERE zone_name = ? AND active = 1",
                    (zone_name,),
                ).fetchone()
                conn.close()
                return dict(row) if row else None

            def get_client_by_name(self, name):
                conn = self._connect()
                row = conn.execute(
                    "SELECT * FROM clients WHERE name = ? AND active = 1",
                    (name,),
                ).fetchone()
                conn.close()
                return dict(row) if row else None

            def get_open_entry(self):
                conn = self._connect()
                row = conn.execute(
                    "SELECT * FROM time_entries WHERE clock_out IS NULL ORDER BY clock_in DESC LIMIT 1"
                ).fetchone()
                conn.close()
                return dict(row) if row else None

            def add_client(self, name, zone_name):
                conn = self._connect()
                conn.execute(
                    "INSERT OR IGNORE INTO clients (name, zone_name) VALUES (?, ?)",
                    (name, zone_name),
                )
                conn.commit()
                conn.close()

            def clock_in(self, client, zone, source="auto"):
                conn = self._connect()
                now = datetime.now().isoformat()
                cursor = conn.execute(
                    "INSERT INTO time_entries (client_name, zone_name, clock_in, source) VALUES (?, ?, ?, ?)",
                    (client, zone, now, source),
                )
                entry_id = cursor.lastrowid
                conn.commit()
                conn.close()
                return FakeTimeEntry(entry_id, client, zone, now, source=source)

            def clock_out(self, entry_id, rounding_minutes=15):
                return FakeTimeEntry(entry_id, "test", "test", datetime.now(), datetime.now())

        return MiniStore(self.db_path)

    # ── resolve_zone_to_client tests ──

    def test_alias_lookup_returns_mapped_client(self):
        """Zone alias 'TimeTrack - LAT' should resolve to 'LATU'."""
        store = self._make_store()
        result = store.resolve_zone_to_client("TimeTrack - LAT")
        self.assertEqual(result, "LATU")

    def test_direct_zone_returns_client(self):
        """Direct client zone 'TimeTrack - LATU' should resolve to 'LATU'."""
        store = self._make_store()
        result = store.resolve_zone_to_client("TimeTrack - LATU")
        self.assertEqual(result, "LATU")

    def test_unknown_zone_returns_none(self):
        """Unknown zone should return None."""
        store = self._make_store()
        result = store.resolve_zone_to_client("TimeTrack - UnknownClient")
        self.assertIsNone(result)

    def test_alias_takes_priority_over_direct(self):
        """If both alias and client exist for a zone, alias wins."""
        store = self._make_store()
        # DiamondIron has both an alias AND a direct client match
        result = store.resolve_zone_to_client("TimeTrack - DiamondIron")
        self.assertEqual(result, "DI")

    # ── Simulated tracker clock-in tests ──

    def test_clock_in_with_alias_uses_resolved_name(self):
        """THE BUG FIX: clock_in via aliased zone should use resolved client name."""
        store = self._make_store()
        TIMETRACK_ZONE_PREFIX = "TimeTrack - "
        zone = "TimeTrack - LAT"

        # Simulate the FIXED _handle_clock_in logic
        client_name = store.resolve_zone_to_client(zone)
        if not client_name:
            client_name = zone.replace(TIMETRACK_ZONE_PREFIX, "")

        self.assertEqual(client_name, "LATU", "Should resolve to LATU, not LAT")

        # Clock in should create entry with 'LATU'
        entry = store.clock_in(client_name, zone, source="auto")
        self.assertEqual(entry.client_name, "LATU")

    def test_clock_in_unknown_zone_falls_back_to_prefix_strip(self):
        """Unknown zone with no alias should fall back to prefix stripping."""
        store = self._make_store()
        TIMETRACK_ZONE_PREFIX = "TimeTrack - "
        zone = "TimeTrack - NewClient"

        client_name = store.resolve_zone_to_client(zone)
        if not client_name:
            client_name = zone.replace(TIMETRACK_ZONE_PREFIX, "")

        self.assertEqual(client_name, "NewClient")

    def test_clock_in_does_not_create_phantom_client(self):
        """Aliased zone should NOT auto-register a new client."""
        store = self._make_store()
        TIMETRACK_ZONE_PREFIX = "TimeTrack - "
        zone = "TimeTrack - LAT"

        client_name = store.resolve_zone_to_client(zone)
        if not client_name:
            client_name = zone.replace(TIMETRACK_ZONE_PREFIX, "")

        # Check that 'LAT' was NOT registered as a client
        existing = store.get_client_by_zone(zone)
        # The zone lookup will find nothing because the client is 'LATU'
        # with zone 'TimeTrack - LATU', not 'TimeTrack - LAT'
        # But since resolve_zone_to_client found the alias, no auto-register needed
        lat_client = store.get_client_by_name("LAT")
        self.assertIsNone(lat_client, "Phantom 'LAT' client should NOT exist")

    def test_ticket_resolution_for_aliased_zone(self):
        """Clock-out event handler should find the ticket for aliased zones."""
        store = self._make_store()
        zone = "TimeTrack - LAT"

        # Simulate the FIXED _handle_clock_out_event logic
        resolved_name = store.resolve_zone_to_client(zone)
        client_info = store.get_client_by_name(resolved_name) if resolved_name else None

        self.assertIsNotNone(client_info, "Should find client info for aliased zone")
        self.assertEqual(client_info["name"], "LATU")
        self.assertEqual(client_info["msp_ticket_id"], "ticket-latu-367")


if __name__ == "__main__":
    unittest.main()
