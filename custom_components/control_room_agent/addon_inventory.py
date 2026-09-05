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


def _empty_inventory(*, supported: bool) -> dict[str, Any]:
    """Return a consistent empty inventory."""
    return {
        "supported": supported,
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


def _is_not_ready_error(err: Exception) -> bool:
    """Recognize Supervisor-not-ready across Home Assistant versions."""
    return err.__class__.__name__ == "HassioNotReadyError"


def _call_hassio_getter(
    module: Any,
    name: str,
    hass: HomeAssistant,
) -> tuple[bool, Any]:
    """Call an optional hassio cache getter across HA API generations.

    Home Assistant 2025.10 exposes add-on data through ``get_addons_info``
    plus ``get_supervisor_info()[\"addons\"]``. Newer HA versions also expose
    ``get_addons_list`` (and temporarily ``get_apps_list`` as an alias).
    """
    getter = getattr(module, name, None)
    if getter is None:
        return False, None

    try:
        return True, getter(hass)
    except Exception as err:  # noqa: BLE001 - compatibility boundary
        if _is_not_ready_error(err):
            return True, None
        raise


def _merge_addon_sources(
    addons_raw: Any,
    addons_info: Any,
) -> list[dict[str, Any]]:
    """Merge installed add-on summary and detailed cached information."""
    merged: dict[str, dict[str, Any]] = {}

    if isinstance(addons_raw, list):
        for addon in addons_raw:
            if not isinstance(addon, dict):
                continue
            slug = addon.get("slug")
            if not slug:
                continue
            merged[str(slug)] = dict(addon)

    if isinstance(addons_info, dict):
        for slug, details in addons_info.items():
            if not isinstance(details, dict):
                continue
            key = str(slug)
            item = merged.setdefault(key, {"slug": key})
            item.update(details)
            item.setdefault("slug", key)

    return list(merged.values())


async def async_collect_addon_inventory(
    hass: HomeAssistant,
) -> dict[str, Any]:
    """Collect Supervisor and add-on state from Home Assistant caches.

    Home Assistant Core/Container installations do not have Supervisor.
    In that case, publish an explicit unsupported state rather than
    treating the missing Supervisor as a collection error.

    The hassio component changed its public cache helpers after HA 2025.10.
    Keep this collector compatible with both the 2025.10 API and the newer
    ``get_addons_list`` API without directly depending on deprecated names.
    """
    if not is_hassio(hass):
        return _empty_inventory(supported=False)

    # Local import keeps Supervisor-specific code out of non-Supervisor
    # installations and avoids importing more than necessary at startup.
    from homeassistant.components import hassio  # noqa: PLC0415

    try:
        _, root_info = _call_hassio_getter(hassio, "get_info", hass)
        _, supervisor_info = _call_hassio_getter(
            hassio,
            "get_supervisor_info",
            hass,
        )
    except Exception as err:  # noqa: BLE001 - compatibility boundary
        if _is_not_ready_error(err):
            return _empty_inventory(supported=True)
        raise

    if not isinstance(supervisor_info, dict):
        return _empty_inventory(supported=True)

    # Newer HA versions expose the installed list explicitly. HA 2025.10.3
    # does not; there the installed add-on summary is folded into
    # supervisor_info["addons"].
    _, addons_raw = _call_hassio_getter(hassio, "get_addons_list", hass)
    if not isinstance(addons_raw, list):
        _, addons_raw = _call_hassio_getter(hassio, "get_apps_list", hass)
    if not isinstance(addons_raw, list):
        addons_raw = supervisor_info.get("addons", [])

    # Detailed cached information adds fields such as auto_update/startup when
    # available. On HA 2025.10 this is the authoritative complementary cache.
    _, addons_info = _call_hassio_getter(hassio, "get_addons_info", hass)
    merged_addons = _merge_addon_sources(addons_raw, addons_info)

    addons = [
        normalized
        for addon in merged_addons
        if (normalized := _normalize_addon(addon))
    ]

    # Keep ordering stable so retained payloads are easy to diff.
    addons.sort(
        key=lambda item: (
            str(item.get("name", "")).casefold(),
            str(item.get("slug", "")).casefold(),
        )
    )

    supervisor = _filtered_dict(supervisor_info, _SUPERVISOR_FIELDS)

    # Root Supervisor info owns health/support status. These values are
    # intentionally kept separate from "supported", which here means
    # "this HA installation supports Supervisor/add-ons".
    if isinstance(root_info, dict):
        if root_info.get("healthy") is not None:
            supervisor["healthy"] = bool(root_info["healthy"])
        if root_info.get("supported") is not None:
            supervisor["system_supported"] = bool(root_info["supported"])

        # Some HA versions expose the installed Supervisor version on root
        # info rather than supervisor info.
        if (
            "version" not in supervisor
            and root_info.get("supervisor") is not None
        ):
            supervisor["version"] = _json_safe_value(root_info["supervisor"])

    return {
        "supported": True,
        "available": True,
        "supervisor": supervisor,
        "summary": _summary(addons),
        "addons": addons,
    }
