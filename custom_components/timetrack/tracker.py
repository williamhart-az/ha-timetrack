"""Zone tracker — listens to person entity state changes and auto clock in/out."""

import logging
from datetime import datetime

from homeassistant.core import HomeAssistant, Event, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED

from .const import TIMETRACK_ZONE_PREFIX, CONF_MIN_SESSION_MINUTES
from .store import TimeTrackStore

_LOGGER = logging.getLogger(__name__)


class ZoneTracker:
    """Track person entity zone changes and manage time entries."""

    def __init__(
        self,
        hass: HomeAssistant,
        store: TimeTrackStore,
        person_entity: str,
        min_session_minutes: int = 15,
        rounding_minutes: int = 15,
    ):
        self.hass = hass
        self.store = store
        self.person_entity = person_entity
        self.min_session_minutes = min_session_minutes
        self.rounding_minutes = rounding_minutes
        self._unsub = None
        self._current_entry_id: int | None = None
        self._listeners: list[callable] = []

        # Check for any open entry from a previous session
        open_entry = store.get_open_entry()
        if open_entry:
            self._current_entry_id = open_entry["id"]
            _LOGGER.info(
                "Resuming open entry #%d for %s",
                open_entry["id"],
                open_entry["client_name"],
            )

    def add_listener(self, callback_fn: callable) -> None:
        """Add a listener that gets called on clock in/out events."""
        self._listeners.append(callback_fn)

    def _notify_listeners(self) -> None:
        """Notify all listeners of a state change."""
        for listener in self._listeners:
            try:
                listener()
            except Exception:
                _LOGGER.exception("Error notifying listener")

    async def async_start(self) -> None:
        """Start tracking zone changes."""
        self._unsub = async_track_state_change_event(
            self.hass,
            [self.person_entity],
            self._handle_zone_change,
        )
        _LOGGER.info(
            "TimeTrack zone tracker started — watching %s for '%s' zones",
            self.person_entity,
            TIMETRACK_ZONE_PREFIX,
        )

    async def async_stop(self) -> None:
        """Stop tracking."""
        if self._unsub:
            self._unsub()
            self._unsub = None

    @callback
    def _handle_zone_change(self, event: Event) -> None:
        """Handle a person entity state change."""
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")

        if not new_state or not old_state:
            return

        new_zone = new_state.state
        old_zone = old_state.state

        if new_zone == old_zone:
            return

        _LOGGER.debug(
            "Zone change: %s → %s",
            old_zone,
            new_zone,
        )

        # Left a TimeTrack zone → clock out
        if old_zone.startswith(TIMETRACK_ZONE_PREFIX):
            self._handle_clock_out(old_zone)

        # Entered a TimeTrack zone → clock in
        if new_zone.startswith(TIMETRACK_ZONE_PREFIX):
            self._handle_clock_in(new_zone)

    def _handle_clock_in(self, zone: str) -> None:
        """Clock in to a TimeTrack zone."""
        client_name = zone.replace(TIMETRACK_ZONE_PREFIX, "")

        # Auto-register client if not known
        existing = self.store.get_client_by_zone(zone)
        if not existing:
            self.store.add_client(client_name, zone)
            _LOGGER.info("Auto-registered new client: %s (zone: %s)", client_name, zone)

        # Close any existing open entry first
        open_entry = self.store.get_open_entry()
        if open_entry:
            _LOGGER.warning(
                "Closing still-open entry #%d for %s before clocking into %s",
                open_entry["id"],
                open_entry["client_name"],
                client_name,
            )
            self.store.clock_out(open_entry["id"], self.rounding_minutes)

        entry = self.store.clock_in(client_name, zone, source="auto")
        self._current_entry_id = entry.id
        _LOGGER.info("⏱️ Clocked IN: %s", client_name)
        self._notify_listeners()

    def _handle_clock_out(self, zone: str) -> None:
        """Clock out of a TimeTrack zone."""
        client_name = zone.replace(TIMETRACK_ZONE_PREFIX, "")

        if not self._current_entry_id:
            # Try to find an open entry
            open_entry = self.store.get_open_entry()
            if open_entry:
                self._current_entry_id = open_entry["id"]
            else:
                _LOGGER.warning("Left zone %s but no open entry found", zone)
                return

        entry = self.store.clock_out(self._current_entry_id, self.rounding_minutes)
        if entry:
            duration = entry.duration_hours
            min_hours = self.min_session_minutes / 60

            if duration < min_hours:
                _LOGGER.info(
                    "⏱️ Discarded: %s (%.1f min < %d min minimum)",
                    client_name,
                    duration * 60,
                    self.min_session_minutes,
                )
                # Delete the entry — GPS noise
                conn = self.store._connect()
                conn.execute(
                    "DELETE FROM time_entries WHERE id = ?", (entry.id,)
                )
                conn.commit()
                conn.close()
            else:
                rounded = self.store._round_hours(duration, self.rounding_minutes)
                _LOGGER.info(
                    "⏱️ Clocked OUT: %s — %.2fh → %.2fh (rounded)",
                    client_name,
                    duration,
                    rounded,
                )

                # Fire event for MSP Manager sync
                self.hass.bus.async_fire(
                    "timetrack_clock_out",
                    {
                        "entry_id": entry.id,
                        "client": client_name,
                        "zone": zone,
                        "clock_in": entry.clock_in.isoformat(),
                        "clock_out": entry.clock_out.isoformat(),
                        "raw_hours": duration,
                        "rounded_hours": rounded,
                    },
                )

        self._current_entry_id = None
        self._notify_listeners()

    # ── Manual services ──

    def manual_clock_in(self, client: str, zone: str = "") -> dict:
        """Manually clock in."""
        if not zone:
            zone = f"{TIMETRACK_ZONE_PREFIX}{client}"
        self._handle_clock_in(zone)
        return {"status": "clocked_in", "client": client}

    def manual_clock_out(self) -> dict:
        """Manually clock out the current entry."""
        open_entry = self.store.get_open_entry()
        if not open_entry:
            return {"status": "error", "message": "No open entry"}
        self._handle_clock_out(open_entry["zone_name"])
        return {"status": "clocked_out", "client": open_entry["client_name"]}

    @property
    def is_clocked_in(self) -> bool:
        return self.store.get_open_entry() is not None

    @property
    def current_client(self) -> str | None:
        entry = self.store.get_open_entry()
        return entry["client_name"] if entry else None

    @property
    def current_duration_hours(self) -> float:
        entry = self.store.get_open_entry()
        if not entry:
            return 0.0
        clock_in = datetime.fromisoformat(entry["clock_in"])
        return (datetime.now() - clock_in).total_seconds() / 3600
