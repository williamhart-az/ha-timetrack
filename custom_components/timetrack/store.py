"""SQLite data store for TimeTrack time entries."""

import sqlite3
import logging
import math
from datetime import datetime, date
from pathlib import Path
from typing import Optional

_LOGGER = logging.getLogger(__name__)


class TimeEntry:
    """Represents a single time entry."""

    def __init__(
        self,
        id: Optional[int],
        client: str,
        zone: str,
        clock_in: datetime,
        clock_out: Optional[datetime] = None,
        source: str = "auto",
        msp_ticket_id: Optional[str] = None,
        msp_synced: bool = False,
    ):
        self.id = id
        self.client = client
        self.zone = zone
        self.clock_in = clock_in
        self.clock_out = clock_out
        self.source = source
        self.msp_ticket_id = msp_ticket_id
        self.msp_synced = msp_synced

    @property
    def duration_hours(self) -> float:
        """Raw duration in hours."""
        if not self.clock_out:
            delta = datetime.now() - self.clock_in
        else:
            delta = self.clock_out - self.clock_in
        return delta.total_seconds() / 3600

    @property
    def is_open(self) -> bool:
        return self.clock_out is None


class TimeTrackStore:
    """SQLite-backed store for time entries."""

    def __init__(self, db_path: str, recorder_db_path: str = None):
        self._db_path = db_path
        self._recorder_db_path = recorder_db_path
        self._init_db()

    # All rates, customers, and tickets are populated from the MSP Manager API
    # at startup. No hardcoded data — clean slate for every install.
    KNOWN_RATES = []
    KNOWN_CUSTOMERS = []
    KNOWN_TICKETS = []
    ALERT_SERVICE_ITEM_IDS = set()

    def get_service_item_for_customer(self, customer_short: str) -> str | None:
        """Get the ServiceItemId for a customer short name (from DB)."""
        conn = self._connect()
        row = conn.execute(
            "SELECT id FROM msp_customers WHERE short_name = ? AND is_active = 1 LIMIT 1",
            (customer_short,),
        ).fetchone()
        conn.close()
        return row["id"] if row else None

    def _get_svc_to_customer_map(self) -> dict:
        """Build ServiceItemId → customer short map from DB."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT id, short_name FROM msp_customers WHERE is_active = 1"
        ).fetchall()
        conn.close()
        return {r["id"]: r["short_name"] for r in rows}

    def _init_db(self):
        """Create tables if they don't exist."""
        conn = self._connect()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS clients (
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

            CREATE TABLE IF NOT EXISTS service_item_rates (
                id TEXT PRIMARY KEY,
                service_item_id TEXT,
                name TEXT NOT NULL,
                rate REAL NOT NULL,
                is_default INTEGER DEFAULT 0,
                is_active INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS msp_customers (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                short_name TEXT NOT NULL,
                is_active INTEGER DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS msp_tickets (
                id TEXT PRIMARY KEY,
                ticket_number INTEGER,
                title TEXT NOT NULL,
                customer_short TEXT,
                service_item_id TEXT,
                status TEXT DEFAULT 'open',
                created_date TEXT,
                completed_date TEXT
            );

            CREATE TABLE IF NOT EXISTS time_entries (
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
                raw_hours REAL,
                rounded_hours REAL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (client_name) REFERENCES clients(name)
            );

            CREATE TABLE IF NOT EXISTS zone_aliases (
                zone_state TEXT PRIMARY KEY,
                client_name TEXT NOT NULL,
                FOREIGN KEY (client_name) REFERENCES clients(name)
            );

            CREATE INDEX IF NOT EXISTS idx_entries_client
                ON time_entries(client_name);
            CREATE INDEX IF NOT EXISTS idx_entries_clock_in
                ON time_entries(clock_in);
            CREATE INDEX IF NOT EXISTS idx_entries_open
                ON time_entries(clock_out);
        """)
        # Migrate: add columns if they don't exist (safe for existing DBs)
        for col, typ, default in [
            ("msp_client_name", "TEXT", None),
            ("msp_service_item_rate_id", "TEXT", None),
            ("default_description", "TEXT", None),
        ]:
            try:
                conn.execute(f"ALTER TABLE clients ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass
        # Migrate time_entries: add new columns
        for col, typ, default in [
            ("description", "TEXT", "''"),
            ("push_status", "TEXT", "'pending'"),
        ]:
            try:
                stmt = f"ALTER TABLE time_entries ADD COLUMN {col} {typ}"
                if default:
                    stmt += f" DEFAULT {default}"
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass
        # Migrate msp_tickets: add is_alert column
        try:
            conn.execute("ALTER TABLE msp_tickets ADD COLUMN is_alert INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        # Migrate service_item_rates: add service_item_id column
        try:
            conn.execute("ALTER TABLE service_item_rates ADD COLUMN service_item_id TEXT")
        except sqlite3.OperationalError:
            pass
        # Migrate msp_tickets: add service_item_id column
        try:
            conn.execute("ALTER TABLE msp_tickets ADD COLUMN service_item_id TEXT")
        except sqlite3.OperationalError:
            pass
        # Migrate time_entries: add billable column
        try:
            conn.execute("ALTER TABLE time_entries ADD COLUMN billable INTEGER DEFAULT 1")
        except sqlite3.OperationalError:
            pass
        # No hardcoded seed data — rates, customers, and tickets are
        # populated from the MSP Manager API at startup.
        conn.commit()
        conn.close()
        _LOGGER.info("TimeTrack database initialized at %s", self._db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ── Client Management ──

    def add_client(
        self,
        name: str,
        zone_name: str,
        msp_ticket_id: str = None,
        msp_service_item_rate_id: str = None,
        msp_client_name: str = None,
        default_description: str = None,
    ) -> None:
        """Add or update a client with MSP Manager mapping.

        If no service_item_rate_id is provided, uses the default rate from the DB.
        """
        if not msp_service_item_rate_id:
            msp_service_item_rate_id = self.get_default_rate_id()
        # Preserve existing zone_name if not provided
        if not zone_name:
            zone_name = f"TimeTrack - {name}"
        conn = self._connect()
        conn.execute(
            """INSERT INTO clients (name, zone_name, msp_ticket_id, msp_service_item_rate_id, msp_client_name, default_description)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                   zone_name = excluded.zone_name,
                   msp_ticket_id = COALESCE(excluded.msp_ticket_id, clients.msp_ticket_id),
                   msp_service_item_rate_id = COALESCE(excluded.msp_service_item_rate_id, clients.msp_service_item_rate_id),
                   msp_client_name = COALESCE(excluded.msp_client_name, clients.msp_client_name),
                   default_description = excluded.default_description""",
            (name, zone_name, msp_ticket_id, msp_service_item_rate_id, msp_client_name, default_description),
        )
        conn.commit()
        conn.close()

    def get_service_item_rates(self) -> list[dict]:
        """Get active service item rates."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT * FROM service_item_rates WHERE is_active = 1 ORDER BY is_default DESC"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def sync_service_item_rates(self, api_rates: list[dict]) -> int:
        """Sync service item rates from MSP Manager API.

        Replaces all existing rates with fresh data from /serviceitemratesview.
        Each rate is tied to a specific customer ServiceItem.
        """
        conn = self._connect()
        conn.execute("DELETE FROM service_item_rates")
        count = 0
        for r in api_rates:
            conn.execute(
                """INSERT OR REPLACE INTO service_item_rates
                   (id, service_item_id, name, rate, is_default, is_active)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    r.get("serviceItemRateId"),
                    r.get("serviceItemId"),
                    r.get("rateName", "Unknown"),
                    r.get("rate", 0),
                    1 if r.get("isDefault") else 0,
                    1 if r.get("isActive", True) else 0,
                ),
            )
            count += 1
        conn.commit()
        conn.close()
        _LOGGER.info("Synced %d service item rates", count)
        return count

    def get_default_rate_for_service_item(self, service_item_id: str) -> Optional[str]:
        """Get the default rate ID for a given ServiceItem."""
        conn = self._connect()
        row = conn.execute(
            """SELECT id FROM service_item_rates
               WHERE service_item_id = ? AND is_default = 1 AND is_active = 1
               LIMIT 1""",
            (service_item_id,),
        ).fetchone()
        conn.close()
        return row["id"] if row else None

    def get_default_rate_id(self) -> str | None:
        """Get the default service item rate ID from DB."""
        conn = self._connect()
        row = conn.execute(
            "SELECT id FROM service_item_rates WHERE is_default = 1 AND is_active = 1 LIMIT 1"
        ).fetchone()
        conn.close()
        return row["id"] if row else None

    def get_client_by_zone(self, zone_name: str) -> Optional[dict]:
        """Look up a client by their TimeTrack zone name."""
        conn = self._connect()
        row = conn.execute(
            "SELECT * FROM clients WHERE zone_name = ? AND active = 1",
            (zone_name,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def get_client_by_name(self, name: str) -> Optional[dict]:
        """Look up a client by their short name."""
        conn = self._connect()
        row = conn.execute(
            "SELECT * FROM clients WHERE name = ? AND active = 1",
            (name,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    # ── Zone Alias CRUD ──

    def add_zone_alias(self, zone_state: str, client_name: str) -> None:
        """Map a HA zone state string to a client name."""
        conn = self._connect()
        conn.execute(
            "INSERT OR REPLACE INTO zone_aliases (zone_state, client_name) VALUES (?, ?)",
            (zone_state, client_name),
        )
        conn.commit()
        conn.close()

    def remove_zone_alias(self, zone_state: str) -> None:
        conn = self._connect()
        conn.execute("DELETE FROM zone_aliases WHERE zone_state = ?", (zone_state,))
        conn.commit()
        conn.close()

    def get_zone_aliases(self) -> list[dict]:
        conn = self._connect()
        rows = conn.execute(
            "SELECT zone_state, client_name FROM zone_aliases ORDER BY client_name"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def resolve_zone_to_client(self, zone_state: str) -> Optional[str]:
        """Resolve a HA zone state to a client name.

        Lookup order:
        1. zone_aliases table (exact match)
        2. clients.zone_name (exact match)
        3. None (skip)
        """
        conn = self._connect()
        # 1. Check aliases
        row = conn.execute(
            "SELECT client_name FROM zone_aliases WHERE zone_state = ?",
            (zone_state,),
        ).fetchone()
        if row:
            conn.close()
            return row["client_name"]
        # 2. Check clients.zone_name
        row = conn.execute(
            "SELECT name FROM clients WHERE zone_name = ? AND active = 1",
            (zone_state,),
        ).fetchone()
        conn.close()
        return row["name"] if row else None

    # ── History-Based Entry Generation ──

    def generate_entries_from_history(
        self,
        start_date: str,
        end_date: str,
        person_metadata_id: int,
        rounding_minutes: int = 15,
    ) -> dict:
        """Generate time entries from HA recorder zone history.

        Args:
            start_date: ISO date string (YYYY-MM-DD)
            end_date: ISO date string (YYYY-MM-DD)
            person_metadata_id: metadata_id for person entity in HA recorder
            rounding_minutes: rounding increment (default 15)

        Returns:
            dict with 'generated', 'skipped', 'errors' counts
        """
        import math
        from datetime import datetime, timezone, timedelta
        from zoneinfo import ZoneInfo

        if not self._recorder_db_path:
            return {"generated": 0, "skipped": 0, "errors": 1,
                    "message": "Recorder DB path not configured"}

        local_tz = ZoneInfo("America/Denver")
        start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=local_tz)
        end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=local_tz
        )

        start_ts = start_dt.timestamp()
        end_ts = end_dt.timestamp()

        # Query recorder for all state changes in range
        rec_conn = sqlite3.connect(self._recorder_db_path)
        rec_conn.row_factory = sqlite3.Row
        rows = rec_conn.execute(
            """SELECT s.state, s.last_changed_ts
               FROM states s
               WHERE s.metadata_id = ?
               AND s.last_changed_ts BETWEEN ? AND ?
               ORDER BY s.last_changed_ts""",
            (person_metadata_id, start_ts, end_ts),
        ).fetchall()
        rec_conn.close()

        # Build zone sessions: (zone_state, arrive_ts, depart_ts)
        sessions = []
        current_zone = None
        arrive_ts = None
        prev_state = None

        for r in rows:
            state = r["state"]
            ts = r["last_changed_ts"]
            if state == prev_state:
                continue  # Skip duplicate states
            prev_state = state

            client = self.resolve_zone_to_client(state)
            if client:
                # Entering a work zone
                if current_zone != state:
                    # Close previous session if switching zones
                    if current_zone and arrive_ts:
                        sessions.append((current_zone, arrive_ts, ts))
                    current_zone = state
                    arrive_ts = ts
            else:
                # Leaving work zone
                if current_zone and arrive_ts:
                    sessions.append((current_zone, arrive_ts, ts))
                    current_zone = None
                    arrive_ts = None

        # Group sessions by zone+date: first arrival, last departure
        from collections import defaultdict
        day_entries = defaultdict(lambda: {"arrive": None, "depart": None, "zone": None})

        for zone_state, arr_ts, dep_ts in sessions:
            client = self.resolve_zone_to_client(zone_state)
            if not client:
                continue

            arr_dt = datetime.fromtimestamp(arr_ts, tz=timezone.utc).astimezone(local_tz)
            dep_dt = datetime.fromtimestamp(dep_ts, tz=timezone.utc).astimezone(local_tz)

            # Split at midnight if needed
            if arr_dt.date() != dep_dt.date():
                # Part 1: arrival date → 23:59:59
                midnight = arr_dt.replace(
                    hour=23, minute=59, second=59
                )
                key1 = (client, arr_dt.date().isoformat())
                e1 = day_entries[key1]
                if e1["arrive"] is None or arr_dt < e1["arrive"]:
                    e1["arrive"] = arr_dt
                    e1["zone"] = zone_state
                e1["depart"] = midnight

                # Part 2: next day 00:00 → departure
                next_day = (arr_dt + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0
                )
                key2 = (client, dep_dt.date().isoformat())
                e2 = day_entries[key2]
                if e2["arrive"] is None or next_day < e2["arrive"]:
                    e2["arrive"] = next_day
                    e2["zone"] = zone_state
                if e2["depart"] is None or dep_dt > e2["depart"]:
                    e2["depart"] = dep_dt
            else:
                key = (client, arr_dt.date().isoformat())
                e = day_entries[key]
                if e["arrive"] is None or arr_dt < e["arrive"]:
                    e["arrive"] = arr_dt
                    e["zone"] = zone_state
                if e["depart"] is None or dep_dt > e["depart"]:
                    e["depart"] = dep_dt

        # Generate entries
        generated = 0
        skipped = 0
        conn = self._connect()

        for (client_name, date_str), data in sorted(day_entries.items()):
            if not data["arrive"] or not data["depart"]:
                continue

            arrive = data["arrive"]
            depart = data["depart"]
            zone = data["zone"] or ""

            # Dedup: skip if entry exists for this client+date
            existing = conn.execute(
                """SELECT id FROM time_entries
                   WHERE client_name = ? AND DATE(clock_in) = ?""",
                (client_name, date_str),
            ).fetchone()
            if existing:
                skipped += 1
                continue

            raw_hours = (depart - arrive).total_seconds() / 3600
            if raw_hours <= 0:
                continue

            increments = 60 / rounding_minutes
            rounded_hours = math.ceil(raw_hours * increments) / increments

            clock_in_str = arrive.isoformat()
            clock_out_str = depart.isoformat()

            # Get default ticket from client
            client_info = self.get_client_by_name(client_name)
            ticket_id = client_info.get("msp_ticket_id") if client_info else None

            conn.execute(
                """INSERT INTO time_entries
                   (client_name, zone_name, clock_in, clock_out,
                    raw_hours, rounded_hours, source, msp_ticket_id,
                    push_status, billable)
                   VALUES (?, ?, ?, ?, ?, ?, 'history', ?, 'pending', 1)""",
                (client_name, zone, clock_in_str, clock_out_str,
                 round(raw_hours, 2), rounded_hours, ticket_id),
            )
            generated += 1

        conn.commit()
        conn.close()
        return {"generated": generated, "skipped": skipped, "errors": 0}

    def get_all_clients(self) -> list[dict]:
        conn = self._connect()
        rows = conn.execute("SELECT * FROM clients WHERE active = 1").fetchall()
        conn.close()
        return [dict(r) for r in rows]

    # ── Time Entry Management ──

    def clock_in(self, client: str, zone: str, source: str = "auto") -> TimeEntry:
        """Create a new open time entry."""
        now = datetime.now()
        conn = self._connect()
        cursor = conn.execute(
            """INSERT INTO time_entries (client_name, zone_name, clock_in, source)
               VALUES (?, ?, ?, ?)""",
            (client, zone, now.isoformat(), source),
        )
        entry_id = cursor.lastrowid
        conn.commit()
        conn.close()
        _LOGGER.info("Clocked in: %s at %s (source: %s)", client, zone, source)
        return TimeEntry(entry_id, client, zone, now, source=source)

    def clock_out(
        self, entry_id: int, rounding_minutes: int = 15
    ) -> Optional[TimeEntry]:
        """Close an open time entry and calculate hours."""
        now = datetime.now()
        conn = self._connect()
        row = conn.execute(
            "SELECT * FROM time_entries WHERE id = ? AND clock_out IS NULL",
            (entry_id,),
        ).fetchone()
        if not row:
            conn.close()
            return None

        clock_in = datetime.fromisoformat(row["clock_in"])
        raw_hours = (now - clock_in).total_seconds() / 3600
        rounded_hours = self._round_hours(raw_hours, rounding_minutes)

        conn.execute(
            """UPDATE time_entries
               SET clock_out = ?, raw_hours = ?, rounded_hours = ?
               WHERE id = ?""",
            (now.isoformat(), raw_hours, rounded_hours, entry_id),
        )
        conn.commit()
        conn.close()

        _LOGGER.info(
            "Clocked out: %s — %.2fh raw → %.2fh rounded",
            row["client_name"],
            raw_hours,
            rounded_hours,
        )

        entry = TimeEntry(
            entry_id,
            row["client_name"],
            row["zone_name"],
            clock_in,
            now,
            row["source"],
        )
        return entry

    def get_open_entry(self) -> Optional[dict]:
        """Get the currently open time entry, if any."""
        conn = self._connect()
        row = conn.execute(
            "SELECT * FROM time_entries WHERE clock_out IS NULL ORDER BY clock_in DESC LIMIT 1"
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def get_entries_for_date(self, target_date: date) -> list[dict]:
        """Get all entries for a specific date."""
        conn = self._connect()
        rows = conn.execute(
            """SELECT * FROM time_entries
               WHERE date(clock_in) = ?
               ORDER BY clock_in""",
            (target_date.isoformat(),),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_entries_for_month(
        self, year: int, month: int
    ) -> list[dict]:
        """Get all entries for a specific month."""
        conn = self._connect()
        rows = conn.execute(
            """SELECT * FROM time_entries
               WHERE strftime('%Y', clock_in) = ?
               AND strftime('%m', clock_in) = ?
               AND clock_out IS NOT NULL
               ORDER BY clock_in""",
            (str(year), f"{month:02d}"),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_daily_totals(
        self, year: int, month: int, rounding_minutes: int = 15
    ) -> list[dict]:
        """Get daily totals per client using span billing.

        Span billing: first arrival → last departure per client/day.
        """
        entries = self.get_entries_for_month(year, month)

        # Group by (date, client)
        spans: dict[tuple[str, str], dict] = {}
        for entry in entries:
            clock_in = datetime.fromisoformat(entry["clock_in"])
            day_key = clock_in.date().isoformat()
            client = entry["client_name"]
            key = (day_key, client)

            if key not in spans:
                spans[key] = {
                    "date": day_key,
                    "client": client,
                    "first_in": clock_in,
                    "last_out": datetime.fromisoformat(entry["clock_out"]),
                }
            else:
                out = datetime.fromisoformat(entry["clock_out"])
                if clock_in < spans[key]["first_in"]:
                    spans[key]["first_in"] = clock_in
                if out > spans[key]["last_out"]:
                    spans[key]["last_out"] = out

        # Calculate span hours with rounding
        result = []
        for key, span in sorted(spans.items()):
            raw_hours = (
                span["last_out"] - span["first_in"]
            ).total_seconds() / 3600
            rounded = self._round_hours(raw_hours, rounding_minutes)
            result.append(
                {
                    "date": span["date"],
                    "client": span["client"],
                    "clock_in": span["first_in"].strftime("%-I:%M %p"),
                    "clock_out": span["last_out"].strftime("%-I:%M %p"),
                    "raw_hours": raw_hours,
                    "rounded_hours": rounded,
                }
            )
        return result

    def get_hours_today(self) -> float:
        """Get total rounded hours for today."""
        today = date.today()
        entries = self.get_entries_for_date(today)
        total = 0.0
        for e in entries:
            if e["rounded_hours"]:
                total += e["rounded_hours"]
            elif e["clock_out"] is None:
                # Open entry — calculate running time
                clock_in = datetime.fromisoformat(e["clock_in"])
                total += (datetime.now() - clock_in).total_seconds() / 3600
        return total

    def get_hours_this_week(self) -> float:
        """Get total rounded hours for the current week (Mon-Sun)."""
        today = date.today()
        monday = today - __import__("datetime").timedelta(days=today.weekday())
        conn = self._connect()
        row = conn.execute(
            """SELECT COALESCE(SUM(rounded_hours), 0) as total
               FROM time_entries
               WHERE date(clock_in) >= ?
               AND clock_out IS NOT NULL""",
            (monday.isoformat(),),
        ).fetchone()
        conn.close()
        return row["total"] if row else 0.0

    def mark_msp_synced(self, entry_id: int) -> None:
        """Mark an entry as synced to MSP Manager."""
        conn = self._connect()
        conn.execute(
            "UPDATE time_entries SET msp_synced = 1, push_status = 'pushed' WHERE id = ?",
            (entry_id,),
        )
        conn.commit()
        conn.close()

    # ── Lookups ──

    def get_ticket_by_id(self, ticket_id: str) -> dict | None:
        """Get a single ticket by its ID."""
        conn = self._connect()
        row = conn.execute(
            "SELECT * FROM msp_tickets WHERE id = ?", (ticket_id,)
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def get_entry_by_id(self, entry_id: int) -> dict | None:
        """Get a single time entry by its ID."""
        conn = self._connect()
        row = conn.execute(
            "SELECT * FROM time_entries WHERE id = ?", (entry_id,)
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    # ── Batch Push Workflow ──

    def get_pending_entries(self) -> list[dict]:
        """Get all completed entries that haven't been pushed yet."""
        conn = self._connect()
        rows = conn.execute(
            """SELECT te.*,
                      COALESCE(te.msp_ticket_id, c.msp_ticket_id) AS resolved_ticket_id,
                      CASE WHEN te.msp_ticket_id IS NULL AND c.msp_ticket_id IS NOT NULL
                           THEN 1 ELSE 0 END AS ticket_from_default,
                      c.msp_service_item_rate_id,
                      c.msp_client_name,
                      t.service_item_id AS ticket_service_item_id,
                      sir.id AS resolved_rate_id
               FROM time_entries te
               LEFT JOIN clients c ON te.client_name = c.name
               LEFT JOIN msp_tickets t
                    ON COALESCE(te.msp_ticket_id, c.msp_ticket_id) = t.id
               LEFT JOIN service_item_rates sir
                    ON sir.service_item_id = t.service_item_id
                   AND sir.is_default = 1
                   AND sir.is_active = 1
               WHERE te.clock_out IS NOT NULL
               AND te.push_status IN ('pending', 'failed')
               ORDER BY te.clock_in ASC"""
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_recent_entries(self, limit: int = 50) -> list[dict]:
        """Get recent completed entries regardless of push status.

        Used by the Status tab to show activity with push status badges.
        Returns more entries so the card can filter by date range client-side.
        """
        conn = self._connect()
        rows = conn.execute(
            """SELECT te.*,
                      COALESCE(te.msp_ticket_id, c.msp_ticket_id) AS resolved_ticket_id,
                      c.msp_client_name,
                      t.ticket_number
               FROM time_entries te
               LEFT JOIN clients c ON te.client_name = c.name
               LEFT JOIN msp_tickets t
                    ON COALESCE(te.msp_ticket_id, c.msp_ticket_id) = t.id
               WHERE te.clock_out IS NOT NULL
               ORDER BY te.clock_in DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def update_entry(
        self,
        entry_id: int,
        description: str = None,
        msp_ticket_id: str = None,
        billable: bool = None,
    ) -> bool:
        """Update an entry's description, ticket, or billable flag before push."""
        conn = self._connect()
        updates = []
        params = []
        if description is not None:
            updates.append("description = ?")
            params.append(description)
        if msp_ticket_id is not None:
            updates.append("msp_ticket_id = ?")
            params.append(msp_ticket_id)
        if billable is not None:
            updates.append("billable = ?")
            params.append(1 if billable else 0)
        if not updates:
            conn.close()
            return False
        params.append(entry_id)
        conn.execute(
            f"UPDATE time_entries SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        conn.commit()
        conn.close()
        return True

    def mark_pushed(self, entry_id: int) -> None:
        """Mark an entry as successfully pushed."""
        conn = self._connect()
        conn.execute(
            "UPDATE time_entries SET push_status = 'pushed', msp_synced = 1 WHERE id = ?",
            (entry_id,),
        )
        conn.commit()
        conn.close()

    def mark_push_failed(self, entry_id: int) -> None:
        """Mark an entry push as failed."""
        conn = self._connect()
        conn.execute(
            "UPDATE time_entries SET push_status = 'failed' WHERE id = ?",
            (entry_id,),
        )
        conn.commit()
        conn.close()

    def get_msp_customers(self) -> list[dict]:
        """Get all MSP Manager customers."""
        conn = self._connect()
        rows = conn.execute(
            "SELECT * FROM msp_customers WHERE is_active = 1 ORDER BY short_name"
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_tickets(self, open_only: bool = False, exclude_alerts: bool = True) -> list[dict]:
        """Get tickets from local DB, ordered by status then date.

        Args:
            open_only: If True, only return open tickets.
            exclude_alerts: If True (default), exclude auto-generated alert tickets.
        """
        conn = self._connect()
        conditions = ["status != 'deleted'"]
        if open_only:
            conditions.append("status = 'open'")
        if exclude_alerts:
            conditions.append("COALESCE(is_alert, 0) = 0")
        query = "SELECT * FROM msp_tickets"
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, ticket_number DESC"
        rows = conn.execute(query).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def upsert_tickets(self, tickets: list[dict]) -> int:
        """Insert or update tickets from MSP Manager API response.

        Also marks any local tickets NOT in the API results as 'deleted'.

        Args:
            tickets: Raw ticket dicts from MSP Manager OData API.
        Returns:
            Number of tickets upserted.
        """
        conn = self._connect()
        count = 0
        synced_ids = set()
        for t in tickets:
            ticket_id = t.get("TicketId")
            if not ticket_id:
                continue
            synced_ids.add(ticket_id)
            svc_id = t.get("ServiceItemId", "")
            customer_short = self._get_svc_to_customer_map().get(svc_id, "")
            completed = t.get("CompletedDate")
            status = "closed" if completed else "open"
            is_alert = 1 if svc_id in self.ALERT_SERVICE_ITEM_IDS else 0
            conn.execute(
                """INSERT OR REPLACE INTO msp_tickets
                   (id, ticket_number, title, customer_short, service_item_id,
                    status, created_date, completed_date, is_alert)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ticket_id,
                    t.get("TicketNumber"),
                    t.get("Title", "Untitled"),
                    customer_short,
                    svc_id,
                    status,
                    t.get("CreatedDate"),
                    completed,
                    is_alert,
                ),
            )
            count += 1

        # Mark tickets missing from API as deleted
        if synced_ids:
            placeholders = ",".join("?" for _ in synced_ids)
            deleted = conn.execute(
                f"""UPDATE msp_tickets SET status = 'deleted'
                    WHERE id NOT IN ({placeholders})
                    AND status != 'deleted'""",
                list(synced_ids),
            ).rowcount
            if deleted:
                _LOGGER.info("Marked %d tickets as deleted (missing from API)", deleted)
                # Reassign pending/failed entries on deleted tickets to client defaults
                orphaned = conn.execute(
                    """SELECT te.id, te.client_name, te.msp_ticket_id
                       FROM time_entries te
                       JOIN msp_tickets mt ON te.msp_ticket_id = mt.id
                       WHERE mt.status = 'deleted'
                       AND te.push_status IN ('pending', 'failed')"""
                ).fetchall()
                for row in orphaned:
                    client = conn.execute(
                        "SELECT msp_ticket_id FROM clients WHERE name = ? AND active = 1",
                        (row["client_name"],),
                    ).fetchone()
                    default_tid = client["msp_ticket_id"] if client else None
                    conn.execute(
                        """UPDATE time_entries
                           SET msp_ticket_id = ?, push_status = 'pending', msp_synced = 0
                           WHERE id = ?""",
                        (default_tid, row["id"]),
                    )
                    _LOGGER.info(
                        "Entry #%d: ticket deleted, reassigned to %s (reset to pending)",
                        row["id"], default_tid or "none",
                    )

        conn.commit()
        conn.close()
        return count

    # ── Helpers ──

    @staticmethod
    def _round_hours(hours: float, rounding_minutes: int = 15) -> float:
        """Round hours UP to nearest increment.

        Matches MSP Manager behavior: always rounds up to the next
        15-minute increment (e.g. 2.30h → 2.50h, not 2.25h).
        """
        if rounding_minutes <= 0:
            return hours
        import math
        increments_per_hour = 60 / rounding_minutes
        return math.ceil(hours * increments_per_hour) / increments_per_hour

    def generate_report(
        self, year: int, month: int, rounding_minutes: int = 15
    ) -> str:
        """Generate a markdown report matching the OpenClaw agent format."""
        import calendar

        month_name = calendar.month_name[month]
        dailies = self.get_daily_totals(year, month, rounding_minutes)

        if not dailies:
            return f"No time entries for {month_name} {year}."

        # Group by client
        by_client: dict[str, list] = {}
        for d in dailies:
            by_client.setdefault(d["client"], []).append(d)

        lines = [f"**{month_name} {year} Time Report**\n"]

        # Summary table
        lines.append("| Location | Days | Hours |")
        lines.append("| --- | --- | --- |")
        total_days = 0
        total_hours = 0.0
        for client, entries in by_client.items():
            days = len(entries)
            hours = sum(e["rounded_hours"] for e in entries)
            total_days += days
            total_hours += hours
            lines.append(
                f"| {client} | {days} | {self._format_hours(hours)} |"
            )
        lines.append(
            f"| **Total** | **{total_days}** | **{self._format_hours(total_hours)}** |"
        )
        lines.append("")

        # Detailed breakdown per client
        for client, entries in by_client.items():
            client_hours = sum(e["rounded_hours"] for e in entries)
            lines.append(f"**{client}** — {self._format_hours(client_hours)}\n")
            lines.append("| Date | In | Out | Hours |")
            lines.append("| --- | --- | --- | --- |")
            for e in entries:
                dt = datetime.fromisoformat(e["date"])
                date_str = dt.strftime("%b %-d")
                lines.append(
                    f"| {date_str} | {e['clock_in']} | {e['clock_out']} | {self._format_hours(e['rounded_hours'])} |"
                )
            lines.append("")

        lines.append(f"**Grand Total: {self._format_hours(total_hours)}**")
        return "\n".join(lines)

    @staticmethod
    def _format_hours(hours: float) -> str:
        """Format hours as 'Xh YYm'."""
        h = int(hours)
        m = int((hours - h) * 60)
        if h == 0:
            return f"{m}m"
        return f"{h}h {m:02d}m"
