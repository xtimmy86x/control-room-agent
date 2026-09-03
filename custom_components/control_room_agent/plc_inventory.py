"""ha-s7plc fleet inventory for Control Room Agent."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.loader import (
    Integration,
    IntegrationNotFound,
    async_get_integration,
)

S7PLC_DOMAIN = "s7plc"


def _entry_status(
    entry_state: ConfigEntryState,
    connected: bool | None,
) -> str:
    """Return a simple fleet-facing PLC status."""
    if entry_state is not ConfigEntryState.LOADED:
        return entry_state.value
    if connected is True:
        return "connected"
    if connected is False:
        return "disconnected"
    return "unknown"


async def async_collect_plc_inventory(
    hass: HomeAssistant,
) -> dict[str, Any]:
    """Collect safe ha-s7plc runtime state.

    This intentionally avoids config-entry data/options so PLC host,
    rack/slot, TSAP values, addresses and other plant configuration are
    never sent to Control Room.
    """
    entries = hass.config_entries.async_entries(S7PLC_DOMAIN)

    try:
        integration = await async_get_integration(hass, S7PLC_DOMAIN)
    except IntegrationNotFound:
        integration = None

    if integration is None and not entries:
        return {
            "supported": False,
            "configured": False,
            "summary": {
                "plc_count": 0,
                "connected_count": 0,
                "disconnected_count": 0,
                "other_state_count": 0,
                "entity_count": 0,
            },
            "plcs": [],
        }

    integration_version = None
    if isinstance(integration, Integration):
        integration_version = integration.manifest.get("version")

    entity_registry = er.async_get(hass)

    plcs: list[dict[str, Any]] = []
    connected_count = 0
    disconnected_count = 0
    other_state_count = 0
    total_entities = 0

    for entry in entries:
        registered_entities = er.async_entries_for_config_entry(
            entity_registry,
            entry.entry_id,
        )
        entity_count = len(registered_entities)
        disabled_entity_count = sum(
            1
            for entity in registered_entities
            if entity.disabled_by is not None
        )
        total_entities += entity_count

        runtime_data = getattr(entry, "runtime_data", None)
        coordinator = getattr(runtime_data, "coordinator", None)

        connected: bool | None = None
        last_update_success: bool | None = None

        if coordinator is not None:
            is_connected = getattr(coordinator, "is_connected", None)
            if callable(is_connected):
                try:
                    connected = bool(is_connected())
                except Exception:  # noqa: BLE001
                    connected = None

            raw_last_update = getattr(
                coordinator,
                "last_update_success",
                None,
            )
            if raw_last_update is not None:
                last_update_success = bool(raw_last_update)

        status = _entry_status(entry.state, connected)

        if status == "connected":
            connected_count += 1
        elif status == "disconnected":
            disconnected_count += 1
        else:
            other_state_count += 1

        runtime_name = getattr(runtime_data, "name", None)
        name = (
            str(runtime_name)
            if runtime_name
            else entry.title
            if entry.title
            else "Siemens S7 PLC"
        )

        item: dict[str, Any] = {
            "plc_id": entry.entry_id,
            "name": name,
            "status": status,
            "config_entry_state": entry.state.value,
            "entity_count": entity_count,
            "disabled_entity_count": disabled_entity_count,
        }

        if connected is not None:
            item["connected"] = connected
        if last_update_success is not None:
            item["last_update_success"] = last_update_success

        plcs.append(item)

    plcs.sort(
        key=lambda item: (
            str(item["name"]).casefold(),
            str(item["plc_id"]),
        )
    )

    result: dict[str, Any] = {
        "supported": integration is not None,
        "configured": bool(entries),
        "summary": {
            "plc_count": len(plcs),
            "connected_count": connected_count,
            "disconnected_count": disconnected_count,
            "other_state_count": other_state_count,
            "entity_count": total_entities,
        },
        "plcs": plcs,
    }

    if integration_version:
        result["integration_version"] = integration_version

    return result
