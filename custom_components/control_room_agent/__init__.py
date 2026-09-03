"""Control Room Agent integration."""

from __future__ import annotations

from datetime import datetime, timezone
import ssl
from typing import Any

from psutil import Process

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.system_info import async_get_system_info

from .mqtt_client import ControlRoomMqttClient
from .system_metrics import prime_cpu_percent


def _get_process_started_at() -> datetime:
    """Return the Home Assistant process start time in UTC.

    psutil may access process information from the operating system, so this
    helper is intentionally executed in Home Assistant's executor.
    """
    return datetime.fromtimestamp(Process().create_time(), tz=timezone.utc)


def _filter_system_info(system_info: dict[str, Any]) -> dict[str, Any]:
    """Return only non-sensitive system metadata needed by Control Room."""
    allowed_keys = (
        "installation_type",
        "python_version",
        "timezone",
        "arch",
        "os_name",
        "os_version",
        "host_os",
        "supervisor",
    )
    return {
        key: system_info[key]
        for key in allowed_keys
        if system_info.get(key) is not None
    }


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Control Room Agent from a config entry."""
    # ssl.create_default_context() loads the system CA store from disk, so it
    # must not run in Home Assistant's event loop.
    ssl_context = await hass.async_add_executor_job(ssl.create_default_context)

    # Home Assistant already exposes a helper that determines installation
    # type and basic runtime/OS metadata. Keep only the fields Control Room
    # actually needs and intentionally omit user/config/network details.
    system_info = _filter_system_info(await async_get_system_info(hass))

    # Reading process metadata can touch /proc or equivalent platform APIs.
    process_started_at = await hass.async_add_executor_job(
        _get_process_started_at
    )

    # Prime psutil's interval-less CPU percentage sampler outside the event
    # loop so the first published sample is meaningful.
    await hass.async_add_executor_job(prime_cpu_percent)

    client = ControlRoomMqttClient(
        hass,
        dict(entry.data),
        ssl_context,
        system_info=system_info,
        process_started_at=process_started_at,
        config_dir=hass.config.config_dir,
    )
    entry.runtime_data = client
    await client.async_start()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Control Room Agent config entry."""
    client: ControlRoomMqttClient = entry.runtime_data
    await client.async_stop()
    return True
