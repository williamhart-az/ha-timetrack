"""Config flow and Options flow for TimeTrack integration."""

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback

from .const import (
    DOMAIN,
    CONF_PERSON_ENTITY,
    CONF_MSP_URL,
    CONF_MSP_API_KEY,
    CONF_MSP_DRY_RUN,
    CONF_ROUNDING_MINUTES,
    CONF_MIN_SESSION_MINUTES,
    DEFAULT_PERSON_ENTITY,
    DEFAULT_ROUNDING_MINUTES,
    DEFAULT_MIN_SESSION_MINUTES,
    DEFAULT_MSP_URL,
)


class TimeTrackConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for TimeTrack."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Get the options flow for this handler."""
        return TimeTrackOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title="TimeTrack",
                data=user_input,
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PERSON_ENTITY, default=DEFAULT_PERSON_ENTITY
                    ): str,
                    vol.Optional(CONF_MSP_URL, default=DEFAULT_MSP_URL): str,
                    vol.Optional(CONF_MSP_API_KEY, default=""): str,
                    vol.Optional(
                        CONF_MSP_DRY_RUN, default=True
                    ): bool,
                    vol.Optional(
                        CONF_ROUNDING_MINUTES, default=DEFAULT_ROUNDING_MINUTES
                    ): int,
                    vol.Optional(
                        CONF_MIN_SESSION_MINUTES,
                        default=DEFAULT_MIN_SESSION_MINUTES,
                    ): int,
                }
            ),
        )


class TimeTrackOptionsFlow(OptionsFlow):
    """Handle options for TimeTrack (reconfigure without removing)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={**self.config_entry.data, **user_input},
            )
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.data

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PERSON_ENTITY,
                        default=current.get(CONF_PERSON_ENTITY, DEFAULT_PERSON_ENTITY),
                    ): str,
                    vol.Optional(
                        CONF_MSP_URL,
                        default=current.get(CONF_MSP_URL, DEFAULT_MSP_URL),
                    ): str,
                    vol.Optional(
                        CONF_MSP_API_KEY,
                        default=current.get(CONF_MSP_API_KEY, ""),
                    ): str,
                    vol.Optional(
                        CONF_MSP_DRY_RUN,
                        default=current.get(CONF_MSP_DRY_RUN, True),
                    ): bool,
                    vol.Optional(
                        CONF_ROUNDING_MINUTES,
                        default=current.get(CONF_ROUNDING_MINUTES, DEFAULT_ROUNDING_MINUTES),
                    ): int,
                    vol.Optional(
                        CONF_MIN_SESSION_MINUTES,
                        default=current.get(CONF_MIN_SESSION_MINUTES, DEFAULT_MIN_SESSION_MINUTES),
                    ): int,
                }
            ),
        )
