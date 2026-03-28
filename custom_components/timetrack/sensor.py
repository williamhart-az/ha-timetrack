"""Sensor platform for TimeTrack integration."""

from datetime import datetime
import logging

from homeassistant.components.sensor import (
    SensorEntity,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up TimeTrack sensors."""
    tracker = hass.data[DOMAIN][entry.entry_id]["tracker"]
    store = hass.data[DOMAIN][entry.entry_id]["store"]

    sensors = [
        TimeTrackCurrentClientSensor(tracker, store),
        TimeTrackCurrentDurationSensor(tracker, store),
        TimeTrackTodaySensor(tracker, store),
        TimeTrackWeekSensor(tracker, store),
        TimeTrackPendingEntriesSensor(tracker, store),
    ]

    async_add_entities(sensors, True)


class TimeTrackBaseSensor(SensorEntity):
    """Base class for TimeTrack sensors."""

    _attr_has_entity_name = True

    def __init__(self, tracker, store):
        self._tracker = tracker
        self._store = store

    async def async_added_to_hass(self) -> None:
        """Register update listener."""
        self._tracker.add_listener(self._handle_update)

    def _handle_update(self) -> None:
        """Handle tracker state change."""
        self.schedule_update_ha_state()


class TimeTrackCurrentClientSensor(TimeTrackBaseSensor):
    """Sensor showing the current client being tracked."""

    _attr_name = "TimeTrack Current Client"
    _attr_unique_id = "timetrack_current_client"
    _attr_icon = "mdi:account-clock"

    @property
    def native_value(self) -> str | None:
        return self._tracker.current_client

    @property
    def extra_state_attributes(self) -> dict:
        entry = self._store.get_open_entry()
        if not entry:
            return {}
        return {
            "entry_id": entry["id"],
            "zone": entry["zone_name"],
            "clock_in": entry["clock_in"],
            "source": entry["source"],
        }


class TimeTrackCurrentDurationSensor(TimeTrackBaseSensor):
    """Sensor showing how long the current session has been running."""

    _attr_name = "TimeTrack Current Duration"
    _attr_unique_id = "timetrack_current_duration"
    _attr_icon = "mdi:timer-outline"
    _attr_native_unit_of_measurement = "h"
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> float:
        return round(self._tracker.current_duration_hours, 2)


class TimeTrackTodaySensor(TimeTrackBaseSensor):
    """Sensor showing total hours tracked today."""

    _attr_name = "TimeTrack Today"
    _attr_unique_id = "timetrack_today"
    _attr_icon = "mdi:calendar-today"
    _attr_native_unit_of_measurement = "h"
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> float:
        return round(self._store.get_hours_today(), 2)


class TimeTrackWeekSensor(TimeTrackBaseSensor):
    """Sensor showing total hours tracked this week."""

    _attr_name = "TimeTrack This Week"
    _attr_unique_id = "timetrack_this_week"
    _attr_icon = "mdi:calendar-week"
    _attr_native_unit_of_measurement = "h"
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> float:
        return round(self._store.get_hours_this_week(), 2)


class TimeTrackPendingEntriesSensor(TimeTrackBaseSensor):
    """Sensor exposing pending entries for the dashboard card."""

    _attr_name = "TimeTrack Pending Entries"
    _attr_unique_id = "timetrack_pending_entries"
    _attr_icon = "mdi:clock-edit-outline"
    _attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> int:
        return len(self._store.get_pending_entries())

    @property
    def extra_state_attributes(self) -> dict:
        pending = self._store.get_pending_entries()
        recent = self._store.get_recent_entries()
        clients = self._store.get_all_clients()
        customers = self._store.get_msp_customers()
        rates = self._store.get_service_item_rates()
        tickets = self._store.get_tickets()
        aliases = self._store.get_zone_aliases()
        users = self._store.get_msp_users()

        # Get current resource ID from integration data
        entry_data = self._tracker._hass.data.get("timetrack", {}) if hasattr(self._tracker, '_hass') else {}
        current_resource_id = ""
        for eid, edata in entry_data.items():
            if isinstance(edata, dict) and "msp_resource_id" in edata:
                current_resource_id = edata.get("msp_resource_id", "")
                break

        def _entry_dict(e):
            return {
                "id": e["id"],
                "client": e["client_name"],
                "zone": e["zone_name"],
                "clock_in": e["clock_in"],
                "clock_out": e["clock_out"],
                "description": e.get("description", ""),
                "raw_hours": e.get("raw_hours", 0),
                "rounded_hours": e.get("rounded_hours", 0),
                "push_status": e.get("push_status", "pending"),
                "msp_ticket_id": e.get("resolved_ticket_id") or e.get("msp_ticket_id"),
                "ticket_number": e.get("ticket_number"),
                "ticket_from_default": bool(e.get("ticket_from_default", 0)),
                "billable": bool(e.get("billable", 1)),
            }

        return {
            "person_entity": self._tracker.person_entity,
            "entries": [_entry_dict(e) for e in recent],
            "pending_entries": [_entry_dict(e) for e in pending],
            "clients": [
                {
                    "name": c["name"],
                    "zone": c["zone_name"],
                    "ticket_id": c.get("msp_ticket_id"),
                    "rate_id": c.get("msp_service_item_rate_id"),
                    "msp_name": c.get("msp_client_name"),
                    "default_description": c.get("default_description", ""),
                }
                for c in clients
            ],
            "customers": [
                {"id": c["id"], "name": c["name"], "short": c["short_name"]}
                for c in customers
            ],
            "rates": [
                {"id": r["id"], "name": r["name"], "rate": r["rate"], "default": bool(r["is_default"])}
                for r in rates
            ],
            "tickets": [
                {
                    "id": t["id"],
                    "num": t["ticket_number"],
                    "title": t["title"],
                    "customer": t.get("customer_short", ""),
                    "status": t.get("status", "open"),
                }
                for t in tickets
            ],
            "zone_aliases": aliases,
            "users": [
                {"id": u["id"], "name": u["name"], "email": u.get("email", "")}
                for u in users
            ],
            "msp_resource_id": current_resource_id,
        }
