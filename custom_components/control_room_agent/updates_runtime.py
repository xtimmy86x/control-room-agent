"""Centralized Home Assistant update inventory publisher."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
from typing import Any

from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from .const import TOPIC_ROOT

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL_SECONDS = 300
UPDATE_SETTLE_SECONDS = 1.0

_CORE_ENTITY_IDS = {
    "update.home_assistant_core_update",
    "update.home_assistant_core",
}
_OS_ENTITY_IDS = {
    "update.home_assistant_operating_system_update",
    "update.home_assistant_os_update",
}
_SUPERVISOR_ENTITY_IDS = {
    "update.home_assistant_supervisor_update",
    "update.supervisor_update",
}


def _category_for_update(
    state: State,
    *,
    platform: str | None,
) -> str:
    """Classify one update entity without exposing entity identifiers."""
    entity_id = state.entity_id
    title = str(
        state.attributes.get("title")
        or state.attributes.get("friendly_name")
        or ""
    ).casefold()

    if entity_id in _CORE_ENTITY_IDS or "home assistant core" in title:
        return "core"
    if entity_id in _OS_ENTITY_IDS or "home assistant operating system" in title:
        return "os"
    if entity_id in _SUPERVISOR_ENTITY_IDS or title in {
        "home assistant supervisor",
        "supervisor",
    }:
        return "supervisor"

    platform_name = (platform or "").casefold()
    if platform_name == "hassio":
        return "addon"
    if platform_name == "hacs":
        return "integration"
    return "other"


def _safe_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def collect_update_inventory(hass: HomeAssistant) -> dict[str, Any]:
    """Return a privacy-safe snapshot of Home Assistant update entities."""
    registry = er.async_get(hass)
    items: list[dict[str, Any]] = []

    for state in hass.states.async_all():
        if not state.entity_id.startswith("update."):
            continue

        entry = registry.async_get(state.entity_id)
        platform = entry.platform if entry is not None else None
        attributes = state.attributes
        category = _category_for_update(state, platform=platform)
        available = state.state == "on"
        supported = state.state not in {STATE_UNAVAILABLE, STATE_UNKNOWN}

        item: dict[str, Any] = {
            "category": category,
            "name": str(
                attributes.get("title")
                or attributes.get("friendly_name")
                or "Update"
            ),
            "update_available": available,
            "available": supported,
        }

        for source, target in (
            ("installed_version", "installed_version"),
            ("latest_version", "latest_version"),
            ("skipped_version", "skipped_version"),
        ):
            value = attributes.get(source)
            if value not in (None, ""):
                item[target] = str(value)

        auto_update = _safe_bool(attributes.get("auto_update"))
        if auto_update is not None:
            item["auto_update"] = auto_update

        in_progress = attributes.get("in_progress")
        if isinstance(in_progress, (bool, int, float)):
            item["in_progress"] = in_progress

        items.append(item)

    order = {
        "core": 0,
        "os": 1,
        "supervisor": 2,
        "addon": 3,
        "integration": 4,
        "other": 5,
    }
    items.sort(
        key=lambda item: (
            order.get(str(item.get("category")), 99),
            str(item.get("name", "")).casefold(),
        )
    )

    available_items = [item for item in items if item["update_available"]]
    summary = {
        "total_entity_count": len(items),
        "total_available_count": len(available_items),
        "core_count": sum(
            1 for item in available_items if item["category"] == "core"
        ),
        "os_count": sum(
            1 for item in available_items if item["category"] == "os"
        ),
        "supervisor_count": sum(
            1
            for item in available_items
            if item["category"] == "supervisor"
        ),
        "addon_count": sum(
            1 for item in available_items if item["category"] == "addon"
        ),
        "integration_count": sum(
            1
            for item in available_items
            if item["category"] == "integration"
        ),
        "other_count": sum(
            1 for item in available_items if item["category"] == "other"
        ),
    }

    return {
        "summary": summary,
        "updates": items,
    }


class UpdatesPublisher:
    """Publish update inventory periodically and after update-entity changes."""

    def __init__(self, hass: HomeAssistant, client: Any) -> None:
        self._hass = hass
        self._client = client
        self._topic = f"{TOPIC_ROOT}/{client._site_id}/updates"
        self._remove_periodic = None
        self._remove_state_listener = None
        self._cancel_debounce = None
        self._running = False
        self._stopped = False

    @callback
    def start(self) -> None:
        if self._stopped:
            return
        self._remove_periodic = async_track_time_interval(
            self._hass,
            self._handle_periodic,
            timedelta(seconds=UPDATE_INTERVAL_SECONDS),
        )
        self._remove_state_listener = self._hass.bus.async_listen(
            EVENT_STATE_CHANGED,
            self._handle_state_changed,
        )
        self._schedule_publish()

    @callback
    def stop(self) -> None:
        self._stopped = True
        if self._remove_periodic is not None:
            self._remove_periodic()
            self._remove_periodic = None
        if self._remove_state_listener is not None:
            self._remove_state_listener()
            self._remove_state_listener = None
        if self._cancel_debounce is not None:
            self._cancel_debounce()
            self._cancel_debounce = None

    @callback
    def _handle_periodic(self, _now: datetime) -> None:
        self._schedule_publish()

    @callback
    def _handle_state_changed(self, event: Event) -> None:
        entity_id = str(event.data.get("entity_id") or "")
        if not entity_id.startswith("update."):
            return
        self._debounce_publish()

    @callback
    def _debounce_publish(self) -> None:
        if self._cancel_debounce is not None:
            self._cancel_debounce()
        self._cancel_debounce = async_call_later(
            self._hass,
            UPDATE_SETTLE_SECONDS,
            self._run_debounced,
        )

    @callback
    def _run_debounced(self, _now: datetime) -> None:
        self._cancel_debounce = None
        self._schedule_publish()

    @callback
    def _schedule_publish(self) -> None:
        if self._stopped or not self._client.connected or self._running:
            return
        self._running = True
        self._hass.async_create_task(
            self._async_publish(),
            "control_room_agent_updates",
        )

    async def _async_publish(self) -> None:
        try:
            inventory = collect_update_inventory(self._hass)
            if not self._client.connected:
                return

            payload = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "site_id": self._client._site_id,
                **inventory,
            }
            result = self._client._client.publish(
                self._topic,
                payload=json.dumps(payload, separators=(",", ":")),
                qos=1,
                retain=True,
            )
            if result.rc != 0:
                _LOGGER.debug(
                    "Update inventory publish failed for site %s: rc=%s",
                    self._client._site_id,
                    result.rc,
                )
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "Unable to collect update inventory for site %s",
                self._client._site_id,
                exc_info=True,
            )
        finally:
            self._running = False
