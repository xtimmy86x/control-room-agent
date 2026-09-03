"""Supervisor and add-on inventory for Control Room Agent."""

from __future__ import annotations

from enum import Enum
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.hassio import is_hassio


_ADDON_FIELDS = (
    "slug",
    "name",
    "state",
    "version",
    "version_latest",
    "update_available",
    "auto_update",
    "startup",
    "watchdog",
)

_SUPERVISOR_FIELDS = (
    "version",
    "version_latest",
    "update_available",
    "channel",
)


def _json_safe_value(value: Any) -> Any:
    """Normalize values commonly returned by Supervisor models."""
    if isinstance(value, Enum):
        return value.value
    return value


def _filtered_dict(
    source: dict[str, Any],
    fields: tuple[str, ...],
) -> dict[str, Any]:
    """Return only explicitly allowed, non-null fields."""
    result: dict[str, Any] = {}
    for field in fields:
        if field not in source or source[field] is None:
            continue
        result[field] = _json_safe_value(source[field])
    return result


def _normalize_addon(addon: dict[str, Any]) -> dict[str, Any]:
    """Return the privacy-safe Control Room representation."""
    return _filtered_dict(addon, _ADDON_FIELDS)


def _summary(addons: list[dict[str, Any]]) -> dict[str, int]:
    """Build neutral add-on counters without declaring stopped an error."""
    started = 0
    stopped = 0
    other_state = 0
    updates = 0

    for addon in addons:
        state = str(addon.get("state", "")).lower()
        if state == "started":
            started += 1
        elif state == "stopped":
            stopped += 1
        else:
            other_state += 1

        if addon.get("update_available") is True:
            updates += 1

    return {
        "addon_count": len(addons),
        "started_count": started,
        "stopped_count": stopped,
        "other_state_count": other_state,
        "update_available_count": updates,
    }


async def async_collect_addon_inventory(
    hass: HomeAssistant,
) -> dict[str, Any]:
    """Collect Supervisor and add-on state from Home Assistant caches.

    Home Assistant Core/Container installations do not have Supervisor.
    In that case, publish an explicit unsupported state rather than
    treating the missing Supervisor as a collection error.
    """
    if not is_hassio(hass):
        return {
            "supported": False,
            "available": False,
            "summary": {
                "addon_count": 0,
                "started_count": 0,
                "stopped_count": 0,
                "other_state_count": 0,
                "update_available_count": 0,
            },
            "addons": [],
        }

    # Local import keeps Supervisor-specific code out of non-Supervisor
    # installations and avoids importing more than necessary at startup.
    from homeassistant.components import hassio  # noqa: PLC0415

    try:
        root_info = hassio.get_info(hass)
        supervisor_info = hassio.get_supervisor_info(hass)
        addons_raw = hassio.get_apps_list(hass)
    except hassio.HassioNotReadyError:
        return {
            "supported": True,
            "available": False,
            "summary": {
                "addon_count": 0,
                "started_count": 0,
                "stopped_count": 0,
                "other_state_count": 0,
                "update_available_count": 0,
            },
            "addons": [],
        }

    addons = [
        normalized
        for addon in addons_raw
        if isinstance(addon, dict)
        and (normalized := _normalize_addon(addon))
    ]

    # Keep ordering stable so retained payloads are easy to diff.
    addons.sort(
        key=lambda item: (
            str(item.get("name", "")).casefold(),
            str(item.get("slug", "")).casefold(),
        )
    )

    supervisor = _filtered_dict(
        supervisor_info if isinstance(supervisor_info, dict) else {},
        _SUPERVISOR_FIELDS,
    )

    # Root Supervisor info owns health/support status. These values are
    # intentionally kept separate from "supported", which here means
    # "this HA installation supports Supervisor/add-ons".
    if isinstance(root_info, dict):
        if root_info.get("healthy") is not None:
            supervisor["healthy"] = bool(root_info["healthy"])
        if root_info.get("supported") is not None:
            supervisor["system_supported"] = bool(
                root_info["supported"]
            )

        # Some HA versions expose the installed Supervisor version on
        # root info rather than supervisor info.
        if (
            "version" not in supervisor
            and root_info.get("supervisor") is not None
        ):
            supervisor["version"] = _json_safe_value(
                root_info["supervisor"]
            )

    return {
        "supported": True,
        "available": True,
        "supervisor": supervisor,
        "summary": _summary(addons),
        "addons": addons,
    }
