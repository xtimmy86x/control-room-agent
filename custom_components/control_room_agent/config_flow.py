"""Config flow for Control Room Agent."""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_SITE_ID,
    CONF_WEBSOCKET_PATH,
    DEFAULT_PORT,
    DEFAULT_WEBSOCKET_PATH,
    DOMAIN,
)
from .mqtt_client import CannotConnect, InvalidAuth, test_connection

SITE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,63}$")


class ControlRoomConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Control Room Agent."""

    VERSION = 1

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle the initial setup step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            user_input[CONF_HOST] = str(user_input[CONF_HOST]).strip()
            user_input[CONF_SITE_ID] = str(user_input[CONF_SITE_ID]).strip().lower()
            user_input[CONF_WEBSOCKET_PATH] = str(
                user_input[CONF_WEBSOCKET_PATH]
            ).strip()

            if not SITE_ID_RE.fullmatch(user_input[CONF_SITE_ID]):
                errors[CONF_SITE_ID] = "invalid_site_id"
            else:
                try:
                    await self.hass.async_add_executor_job(
                        test_connection,
                        user_input,
                    )
                except InvalidAuth:
                    errors["base"] = "invalid_auth"
                except CannotConnect:
                    errors["base"] = "cannot_connect"
                except Exception:  # noqa: BLE001
                    errors["base"] = "unknown"
                else:
                    await self.async_set_unique_id(user_input[CONF_SITE_ID])
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=user_input[CONF_SITE_ID],
                        data=user_input,
                    )

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): TextSelector(
                    TextSelectorConfig(
                        type=TextSelectorType.TEXT,
                        autocomplete="hostname",
                    )
                ),
                vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=1, max=65535),
                ),
                vol.Required(
                    CONF_WEBSOCKET_PATH,
                    default=DEFAULT_WEBSOCKET_PATH,
                ): TextSelector(),
                vol.Required(CONF_SITE_ID): TextSelector(),
                vol.Required(CONF_USERNAME): TextSelector(
                    TextSelectorConfig(autocomplete="username")
                ),
                vol.Required(CONF_PASSWORD): TextSelector(
                    TextSelectorConfig(
                        type=TextSelectorType.PASSWORD,
                        autocomplete="current-password",
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )
