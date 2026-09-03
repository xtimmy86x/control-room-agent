"""Safe integration inventory for Control Room Agent."""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.loader import Integration, async_get_integrations
from homeassistant.setup import async_get_loaded_integrations


_PROBLEM_STATES = {
    "setup_error",
    "migration_error",
    "setup_retry",
    "failed_unload",
}


def _base_loaded_domains(hass: HomeAssistant) -> set[str]:
    """Return loaded top-level integration domains."""
    return {
        component.split(".", 1)[0]
        for component in async_get_loaded_integrations(hass)
    }


def _entry_summary(entries: list[ConfigEntry]) -> dict[str, Any]:
    """Return a privacy-safe summary of config entries."""
    state_counts = Counter(entry.state.value for entry in entries)
    disabled_count = sum(
        1 for entry in entries if entry.disabled_by is not None
    )
    problem_count = sum(
        count
        for state, count in state_counts.items()
        if state in _PROBLEM_STATES
    )

    return {
        "config_entry_count": len(entries),
        "disabled_entry_count": disabled_count,
        "problem_entry_count": problem_count,
        "states": dict(sorted(state_counts.items())),
    }


async def async_collect_integration_inventory(
    hass: HomeAssistant,
) -> dict[str, Any]:
    """Build the Control Room integration inventory.

    Include:
    - integrations that have one or more config entries;
    - loaded custom integrations even if they have no config entry.

    Deliberately exclude ordinary loaded Core dependencies with no
    config entry to keep the inventory useful and compact.
    """
    entries_by_domain: dict[str, list[ConfigEntry]] = defaultdict(list)
    for entry in hass.config_entries.async_entries():
        # Ignore discovery entries whose sole purpose is to suppress
        # future discovery prompts.
        if entry.source == "ignore":
            continue
        entries_by_domain[entry.domain].append(entry)

    loaded_domains = _base_loaded_domains(hass)
    candidate_domains = set(entries_by_domain) | loaded_domains

    integrations_or_exceptions = await async_get_integrations(
        hass,
        candidate_domains,
    )

    items: list[dict[str, Any]] = []

    for domain in sorted(candidate_domains):
        integration_or_exc = integrations_or_exceptions.get(domain)
        entries = entries_by_domain.get(domain, [])
        loaded = domain in loaded_domains

        if isinstance(integration_or_exc, Integration):
            integration = integration_or_exc
            is_custom = not integration.is_built_in

            # Core dependencies without a config entry add noise to a
            # fleet inventory. Custom integrations remain useful even
            # if they are YAML-only or otherwise entry-less.
            if not entries and not is_custom:
                continue

            manifest = integration.manifest
            item: dict[str, Any] = {
                "domain": domain,
                "name": manifest.get("name", domain),
                "source": "custom" if is_custom else "core",
                "loaded": loaded,
                "integration_type": manifest.get(
                    "integration_type",
                    "integration",
                ),
                **_entry_summary(entries),
            }

            if version := manifest.get("version"):
                item["version"] = version
        else:
            # Preserve config-entry visibility even if loader metadata
            # cannot currently be resolved.
            if not entries:
                continue
            item = {
                "domain": domain,
                "name": domain,
                "source": "unknown",
                "loaded": loaded,
                "integration_type": "unknown",
                "metadata_available": False,
                **_entry_summary(entries),
            }

        items.append(item)

    custom_count = sum(1 for item in items if item["source"] == "custom")
    core_count = sum(1 for item in items if item["source"] == "core")
    problem_integrations = sum(
        1 for item in items if item["problem_entry_count"] > 0
    )
    total_entries = sum(item["config_entry_count"] for item in items)
    disabled_entries = sum(
        item["disabled_entry_count"] for item in items
    )
    problem_entries = sum(item["problem_entry_count"] for item in items)

    return {
        "summary": {
            "integration_count": len(items),
            "custom_integration_count": custom_count,
            "core_integration_count": core_count,
            "config_entry_count": total_entries,
            "disabled_entry_count": disabled_entries,
            "problem_integration_count": problem_integrations,
            "problem_entry_count": problem_entries,
        },
        "integrations": items,
    }
