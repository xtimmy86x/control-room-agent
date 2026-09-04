"""Event-driven inventory refresh helpers for Control Room Agent."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from homeassistant import config_entries
from homeassistant.components.hassio.const import EVENT_SUPERVISOR_EVENT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN
from .mqtt_client import ControlRoomMqttClient


INTEGRATIONS_SETTLE_SECONDS = 1.0
ADDONS_SETTLE_SECONDS = 2.0


class EventDrivenRefreshManager:
    """Refresh integration/add-on inventories when Home Assistant changes."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: ControlRoomMqttClient,
    ) -> None:
        self._hass = hass
        self._client = client

        self._remove_config_entry_signal: Callable[[], None] | None = None
        self._remove_supervisor_signal: Callable[[], None] | None = None
        self._entry_state_unsubs: dict[str, Callable[[], None]] = {}

        self._cancel_integrations_refresh: Callable[[], None] | None = None
        self._cancel_addons_refresh: Callable[[], None] | None = None
        self._stopped = False

    @callback
    def start(self) -> None:
        """Start listening for config-entry and Supervisor changes."""
        if self._stopped:
            return

        self._remove_config_entry_signal = async_dispatcher_connect(
            self._hass,
            config_entries.SIGNAL_CONFIG_ENTRY_CHANGED,
            self._handle_config_entry_changed,
        )
        self._sync_config_entry_state_listeners()

        # On Core/Container this signal simply never fires. Registering the
        # listener is harmless and keeps one code path for all install types.
        self._remove_supervisor_signal = async_dispatcher_connect(
            self._hass,
            EVENT_SUPERVISOR_EVENT,
            self._handle_supervisor_event,
        )

    @callback
    def stop(self) -> None:
        """Remove all listeners and pending debounce timers."""
        self._stopped = True

        if self._remove_config_entry_signal is not None:
            self._remove_config_entry_signal()
            self._remove_config_entry_signal = None

        if self._remove_supervisor_signal is not None:
            self._remove_supervisor_signal()
            self._remove_supervisor_signal = None

        for unsubscribe in self._entry_state_unsubs.values():
            unsubscribe()
        self._entry_state_unsubs.clear()

        if self._cancel_integrations_refresh is not None:
            self._cancel_integrations_refresh()
            self._cancel_integrations_refresh = None

        if self._cancel_addons_refresh is not None:
            self._cancel_addons_refresh()
            self._cancel_addons_refresh = None

    @callback
    def _sync_config_entry_state_listeners(self) -> None:
        """Keep state listeners aligned with the current config-entry set."""
        current_ids: set[str] = set()

        for entry in self._hass.config_entries.async_entries():
            # Avoid reacting to our own unload/reload lifecycle.
            if entry.domain == DOMAIN:
                continue

            current_ids.add(entry.entry_id)
            if entry.entry_id in self._entry_state_unsubs:
                continue

            self._entry_state_unsubs[entry.entry_id] = (
                entry.async_on_state_change(self._handle_config_entry_state_changed)
            )

        for entry_id in set(self._entry_state_unsubs) - current_ids:
            self._entry_state_unsubs.pop(entry_id)()

    @callback
    def _handle_config_entry_changed(
        self,
        _change: config_entries.ConfigEntryChange,
        entry: config_entries.ConfigEntry,
    ) -> None:
        """Refresh when an entry is added, removed or updated."""
        if self._stopped or entry.domain == DOMAIN:
            return

        self._sync_config_entry_state_listeners()
        self._debounce_integrations_refresh()

    @callback
    def _handle_config_entry_state_changed(self) -> None:
        """Refresh when an existing config entry changes runtime state."""
        if self._stopped:
            return
        self._debounce_integrations_refresh()

    @callback
    def _handle_supervisor_event(self, _event: dict[str, Any]) -> None:
        """Refresh add-ons after Supervisor activity.

        Supervisor emits events for add-on lifecycle and store changes. We
        debounce all Supervisor events instead of depending on undocumented
        per-event payload shapes. The collector performs the authoritative
        Supervisor API read after the short settle delay.
        """
        if self._stopped:
            return
        self._debounce_addons_refresh()

    @callback
    def _debounce_integrations_refresh(self) -> None:
        """Coalesce bursts of config-entry transitions into one refresh."""
        if self._cancel_integrations_refresh is not None:
            self._cancel_integrations_refresh()

        self._cancel_integrations_refresh = async_call_later(
            self._hass,
            INTEGRATIONS_SETTLE_SECONDS,
            self._run_integrations_refresh,
        )

    @callback
    def _debounce_addons_refresh(self) -> None:
        """Coalesce bursts of Supervisor events into one refresh."""
        if self._cancel_addons_refresh is not None:
            self._cancel_addons_refresh()

        self._cancel_addons_refresh = async_call_later(
            self._hass,
            ADDONS_SETTLE_SECONDS,
            self._run_addons_refresh,
        )

    @callback
    def _run_integrations_refresh(self, _now: datetime) -> None:
        """Request the integrations inventory after the settle delay."""
        self._cancel_integrations_refresh = None
        if self._stopped or not self._client.connected:
            return

        # The MQTT client already prevents overlap. If a periodic collection
        # is currently running, retry after the same short settle period so
        # the event-driven refresh is not lost.
        if getattr(self._client, "_integrations_update_in_progress", False):
            self._debounce_integrations_refresh()
            return

        self._client._schedule_integrations_update()

    @callback
    def _run_addons_refresh(self, _now: datetime) -> None:
        """Request the add-on inventory after the Supervisor settle delay."""
        self._cancel_addons_refresh = None
        if self._stopped or not self._client.connected:
            return

        if getattr(self._client, "_addons_update_in_progress", False):
            self._debounce_addons_refresh()
            return

        self._client._schedule_addons_update()
