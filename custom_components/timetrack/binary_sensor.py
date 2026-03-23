"""Binary sensor platform for TimeTrack integration."""

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorDeviceClass,
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
    """Set up TimeTrack binary sensors."""
    tracker = hass.data[DOMAIN][entry.entry_id]["tracker"]
    store = hass.data[DOMAIN][entry.entry_id]["store"]

    async_add_entities([TimeTrackClockedInSensor(tracker, store)], True)


class TimeTrackClockedInSensor(BinarySensorEntity):
    """Binary sensor showing if currently clocked in."""

    _attr_name = "TimeTrack Clocked In"
    _attr_unique_id = "timetrack_clocked_in"
    _attr_icon = "mdi:clock-check"
    _attr_has_entity_name = True

    def __init__(self, tracker, store):
        self._tracker = tracker
        self._store = store

    async def async_added_to_hass(self) -> None:
        self._tracker.add_listener(self._handle_update)

    def _handle_update(self) -> None:
        self.schedule_update_ha_state()

    @property
    def is_on(self) -> bool:
        return self._tracker.is_clocked_in

    @property
    def extra_state_attributes(self) -> dict:
        if not self._tracker.is_clocked_in:
            return {}
        return {
            "client": self._tracker.current_client,
            "duration_hours": round(self._tracker.current_duration_hours, 2),
        }
