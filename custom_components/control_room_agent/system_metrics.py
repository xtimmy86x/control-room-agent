"""System metrics collection for Control Room Agent."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any

import psutil


def prime_cpu_percent() -> None:
    """Prime psutil's non-blocking CPU percentage calculation."""
    psutil.cpu_percent(interval=None)


def collect_system_metrics(config_dir: str) -> dict[str, Any]:
    """Collect lightweight host/runtime system metrics.

    This function performs operating-system calls and must be run in
    Home Assistant's executor.
    """
    now = datetime.now(timezone.utc)

    memory = psutil.virtual_memory()
    disk = psutil.disk_usage(str(Path(config_dir).resolve()))

    try:
        load_1m, load_5m, load_15m = os.getloadavg()
    except (AttributeError, OSError):
        load_1m = load_5m = load_15m = None

    try:
        boot_time = datetime.fromtimestamp(
            psutil.boot_time(),
            tz=timezone.utc,
        )
        system_uptime_seconds = max(
            0,
            int((now - boot_time).total_seconds()),
        )
    except (OSError, OverflowError, ValueError):
        boot_time = None
        system_uptime_seconds = None

    payload: dict[str, Any] = {
        "cpu_percent": round(psutil.cpu_percent(interval=None), 1),
        "cpu_count": psutil.cpu_count(logical=True),
        "memory_total_bytes": int(memory.total),
        "memory_used_bytes": int(memory.used),
        "memory_available_bytes": int(memory.available),
        "memory_percent": round(float(memory.percent), 1),
        "disk_total_bytes": int(disk.total),
        "disk_used_bytes": int(disk.used),
        "disk_free_bytes": int(disk.free),
        "disk_percent": round(float(disk.percent), 1),
    }

    if load_1m is not None:
        payload["load_1m"] = round(float(load_1m), 2)
        payload["load_5m"] = round(float(load_5m), 2)
        payload["load_15m"] = round(float(load_15m), 2)

    if boot_time is not None:
        payload["system_boot_time"] = boot_time.isoformat()

    if system_uptime_seconds is not None:
        payload["system_uptime_seconds"] = system_uptime_seconds

    return payload
