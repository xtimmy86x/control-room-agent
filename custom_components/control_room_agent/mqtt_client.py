"""Dedicated MQTT-over-WebSocket client for Control Room Agent."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
import ssl
from threading import Event
from typing import Any
from uuid import uuid4

import paho.mqtt.client as mqtt

from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .system_metrics import collect_system_metrics
from .integration_inventory import async_collect_integration_inventory
from .addon_inventory import async_collect_addon_inventory
from .plc_inventory import async_collect_plc_inventory

from .const import (
    CONF_SITE_ID,
    CONF_WEBSOCKET_PATH,
    DEFAULT_ADDONS_INTERVAL,
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_PLC_INTERVAL,
    DEFAULT_INTEGRATIONS_INTERVAL,
    DEFAULT_SYSTEM_INTERVAL,
    TOPIC_ROOT,
    VERSION,
)

_LOGGER = logging.getLogger(__name__)


class CannotConnect(Exception):
    """Raised when the broker cannot be reached."""


class InvalidAuth(Exception):
    """Raised when the broker rejects the credentials."""


def _normalize_path(path: str) -> str:
    """Return a valid WebSocket path."""
    path = path.strip()
    if not path:
        return "/mqtt"
    if not path.startswith("/"):
        return f"/{path}"
    return path


def _build_client(
    *,
    client_id: str,
    host: str,
    port: int,
    websocket_path: str,
    username: str,
    password: str,
    will_topic: str | None = None,
    ssl_context: ssl.SSLContext | None = None,
) -> mqtt.Client:
    """Build an isolated MQTT client using secure WebSockets."""
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        clean_session=True,
        protocol=mqtt.MQTTv311,
        transport="websockets",
    )
    client.username_pw_set(username, password)
    client.ws_set_options(path=_normalize_path(websocket_path))
    client.tls_set_context(ssl_context or ssl.create_default_context())

    if will_topic is not None:
        client.will_set(
            will_topic,
            payload="offline",
            qos=1,
            retain=True,
        )

    return client


def test_connection(config: dict[str, Any], timeout: float = 10.0) -> None:
    """Synchronously test broker authentication and connectivity.

    This function is intended to run in Home Assistant's executor.
    """
    result = Event()
    state: dict[str, Any] = {"reason_code": None}

    site_id = str(config[CONF_SITE_ID])
    client = _build_client(
        client_id=f"control-room-probe-{site_id}-{uuid4().hex[:8]}",
        host=str(config["host"]),
        port=int(config["port"]),
        websocket_path=str(config[CONF_WEBSOCKET_PATH]),
        username=str(config["username"]),
        password=str(config["password"]),
    )

    def on_connect(
        _client: mqtt.Client,
        _userdata: Any,
        _flags: mqtt.ConnectFlags,
        reason_code: mqtt.ReasonCode,
        _properties: mqtt.Properties | None,
    ) -> None:
        state["reason_code"] = reason_code
        result.set()

    def on_disconnect(
        _client: mqtt.Client,
        _userdata: Any,
        _disconnect_flags: mqtt.DisconnectFlags,
        reason_code: mqtt.ReasonCode,
        _properties: mqtt.Properties | None,
    ) -> None:
        if state["reason_code"] is None and reason_code != 0:
            state["reason_code"] = reason_code
            result.set()

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect

    try:
        client.connect(
            str(config["host"]),
            int(config["port"]),
            keepalive=15,
        )
        client.loop_start()

        if not result.wait(timeout):
            raise CannotConnect

        reason_code = state["reason_code"]
        if reason_code is None:
            raise CannotConnect

        if reason_code != 0:
            reason_text = str(reason_code).lower()
            if (
                "auth" in reason_text
                or "password" in reason_text
                or "not authorized" in reason_text
                or "bad user" in reason_text
            ):
                raise InvalidAuth
            raise CannotConnect
    except InvalidAuth:
        raise
    except (OSError, mqtt.WebsocketConnectionError, ValueError) as err:
        raise CannotConnect from err
    finally:
        try:
            client.disconnect()
        except Exception:  # noqa: BLE001 - cleanup must never hide the real error
            pass
        try:
            client.loop_stop()
        except Exception:  # noqa: BLE001 - cleanup must never hide the real error
            pass


class ControlRoomMqttClient:
    """Manage the dedicated outbound Control Room MQTT connection."""

    def __init__(
        self,
        hass: HomeAssistant,
        config: dict[str, Any],
        ssl_context: ssl.SSLContext,
        *,
        system_info: dict[str, Any],
        process_started_at: datetime,
        config_dir: str,
    ) -> None:
        """Initialize the client."""
        self._hass = hass
        self._site_id = str(config[CONF_SITE_ID])
        self._host = str(config["host"])
        self._port = int(config["port"])
        self._path = str(config[CONF_WEBSOCKET_PATH])
        self._username = str(config["username"])
        self._password = str(config["password"])
        self._system_info = system_info
        self._process_started_at = process_started_at
        self._config_dir = config_dir

        self._connected = Event()
        self._remove_heartbeat = None
        self._remove_system_update = None
        self._remove_integrations_update = None
        self._remove_addons_update = None
        self._remove_plc_update = None
        self._system_update_in_progress = False
        self._integrations_update_in_progress = False
        self._addons_update_in_progress = False
        self._plc_update_in_progress = False
        self._session_id = uuid4().hex

        self._availability_topic = (
            f"{TOPIC_ROOT}/{self._site_id}/availability"
        )
        self._heartbeat_topic = f"{TOPIC_ROOT}/{self._site_id}/heartbeat"
        self._homeassistant_topic = (
            f"{TOPIC_ROOT}/{self._site_id}/homeassistant"
        )
        self._system_topic = f"{TOPIC_ROOT}/{self._site_id}/system"
        self._integrations_topic = (
            f"{TOPIC_ROOT}/{self._site_id}/integrations"
        )
        self._addons_topic = f"{TOPIC_ROOT}/{self._site_id}/addons"
        self._plc_topic = f"{TOPIC_ROOT}/{self._site_id}/plc"

        self._client = _build_client(
            client_id=f"control-room-agent-{self._site_id}",
            host=self._host,
            port=self._port,
            websocket_path=self._path,
            username=self._username,
            password=self._password,
            will_topic=self._availability_topic,
            ssl_context=ssl_context,
        )
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.reconnect_delay_set(min_delay=1, max_delay=60)

    @property
    def connected(self) -> bool:
        """Return whether the broker connection is active."""
        return self._connected.is_set()

    async def async_start(self) -> None:
        """Start the MQTT network loop and heartbeat."""
        _LOGGER.info(
            "Starting Control Room Agent for site %s via wss://%s:%s%s",
            self._site_id,
            self._host,
            self._port,
            _normalize_path(self._path),
        )

        self._client.connect_async(
            self._host,
            self._port,
            keepalive=30,
        )
        self._client.loop_start()

        self._remove_heartbeat = async_track_time_interval(
            self._hass,
            self._handle_heartbeat,
            timedelta(seconds=DEFAULT_HEARTBEAT_INTERVAL),
        )
        self._remove_system_update = async_track_time_interval(
            self._hass,
            self._handle_system_update,
            timedelta(seconds=DEFAULT_SYSTEM_INTERVAL),
        )
        self._remove_integrations_update = async_track_time_interval(
            self._hass,
            self._handle_integrations_update,
            timedelta(seconds=DEFAULT_INTEGRATIONS_INTERVAL),
        )
        self._remove_addons_update = async_track_time_interval(
            self._hass,
            self._handle_addons_update,
            timedelta(seconds=DEFAULT_ADDONS_INTERVAL),
        )
        self._remove_plc_update = async_track_time_interval(
            self._hass,
            self._handle_plc_update,
            timedelta(seconds=DEFAULT_PLC_INTERVAL),
        )

    async def async_stop(self) -> None:
        """Stop heartbeat and disconnect cleanly."""
        if self._remove_heartbeat is not None:
            self._remove_heartbeat()
            self._remove_heartbeat = None

        if self._remove_system_update is not None:
            self._remove_system_update()
            self._remove_system_update = None

        if self._remove_integrations_update is not None:
            self._remove_integrations_update()
            self._remove_integrations_update = None

        if self._remove_addons_update is not None:
            self._remove_addons_update()
            self._remove_addons_update = None

        if self._remove_plc_update is not None:
            self._remove_plc_update()
            self._remove_plc_update = None

        await self._hass.async_add_executor_job(self._stop_sync)

    def _stop_sync(self) -> None:
        """Cleanly disconnect from the broker."""
        if self._connected.is_set():
            try:
                info = self._client.publish(
                    self._availability_topic,
                    payload="offline",
                    qos=1,
                    retain=True,
                )
                info.wait_for_publish(timeout=2.0)
            except Exception:  # noqa: BLE001
                _LOGGER.debug(
                    "Could not publish graceful offline state for site %s",
                    self._site_id,
                    exc_info=True,
                )

        try:
            self._client.disconnect()
        finally:
            self._client.loop_stop()
            self._connected.clear()

    def _on_connect(
        self,
        _client: mqtt.Client,
        _userdata: Any,
        _flags: mqtt.ConnectFlags,
        reason_code: mqtt.ReasonCode,
        _properties: mqtt.Properties | None,
    ) -> None:
        """Handle MQTT connection."""
        if reason_code != 0:
            _LOGGER.warning(
                "Control Room MQTT connection rejected for site %s: %s",
                self._site_id,
                reason_code,
            )
            self._connected.clear()
            return

        self._connected.set()
        _LOGGER.info("Control Room connected for site %s", self._site_id)

        self._client.publish(
            self._availability_topic,
            payload="online",
            qos=1,
            retain=True,
        )
        self._publish_homeassistant_info()
        self._publish_heartbeat()
        self._hass.loop.call_soon_threadsafe(self._schedule_system_update)
        self._hass.loop.call_soon_threadsafe(
            self._schedule_integrations_update
        )
        self._hass.loop.call_soon_threadsafe(self._schedule_addons_update)
        self._hass.loop.call_soon_threadsafe(self._schedule_plc_update)

    def _on_disconnect(
        self,
        _client: mqtt.Client,
        _userdata: Any,
        _disconnect_flags: mqtt.DisconnectFlags,
        reason_code: mqtt.ReasonCode,
        _properties: mqtt.Properties | None,
    ) -> None:
        """Handle MQTT disconnection."""
        self._connected.clear()
        if reason_code != 0:
            _LOGGER.warning(
                "Control Room disconnected unexpectedly for site %s: %s",
                self._site_id,
                reason_code,
            )
        else:
            _LOGGER.info("Control Room disconnected for site %s", self._site_id)

    @callback
    def _handle_heartbeat(self, _now: datetime) -> None:
        """Send a periodic heartbeat."""
        if self._connected.is_set():
            self._publish_heartbeat()

    @callback
    def _handle_system_update(self, _now: datetime) -> None:
        """Schedule a periodic system metrics update."""
        self._schedule_system_update()

    @callback
    def _handle_integrations_update(self, _now: datetime) -> None:
        """Schedule a periodic integration inventory update."""
        self._schedule_integrations_update()

    @callback
    def _handle_addons_update(self, _now: datetime) -> None:
        """Schedule a periodic Supervisor/add-on inventory update."""
        self._schedule_addons_update()

    @callback
    def _handle_plc_update(self, _now: datetime) -> None:
        """Schedule a periodic ha-s7plc inventory update."""
        self._schedule_plc_update()

    @callback
    def _schedule_plc_update(self) -> None:
        """Schedule ha-s7plc inventory collection without overlap."""
        if (
            not self._connected.is_set()
            or self._plc_update_in_progress
        ):
            return

        self._plc_update_in_progress = True
        self._hass.async_create_task(
            self._async_publish_plc(),
            "control_room_agent_plc",
        )

    async def _async_publish_plc(self) -> None:
        """Collect and publish ha-s7plc fleet state."""
        try:
            inventory = await async_collect_plc_inventory(self._hass)

            if not self._connected.is_set():
                return

            payload = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "site_id": self._site_id,
                **inventory,
            }

            result = self._client.publish(
                self._plc_topic,
                payload=json.dumps(payload, separators=(",", ":")),
                qos=1,
                retain=True,
            )
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                _LOGGER.debug(
                    "PLC inventory publish failed for site %s: rc=%s",
                    self._site_id,
                    result.rc,
                )
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "Unable to collect ha-s7plc inventory for site %s",
                self._site_id,
                exc_info=True,
            )
        finally:
            self._plc_update_in_progress = False

    @callback
    def _schedule_addons_update(self) -> None:
        """Schedule Supervisor/add-on collection without overlap."""
        if (
            not self._connected.is_set()
            or self._addons_update_in_progress
        ):
            return

        self._addons_update_in_progress = True
        self._hass.async_create_task(
            self._async_publish_addons(),
            "control_room_agent_addons",
        )

    async def _async_publish_addons(self) -> None:
        """Collect and publish Supervisor/add-on inventory."""
        try:
            inventory = await async_collect_addon_inventory(self._hass)

            if not self._connected.is_set():
                return

            payload = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "site_id": self._site_id,
                **inventory,
            }

            result = self._client.publish(
                self._addons_topic,
                payload=json.dumps(payload, separators=(",", ":")),
                qos=1,
                retain=True,
            )
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                _LOGGER.debug(
                    "Add-on inventory publish failed for site %s: rc=%s",
                    self._site_id,
                    result.rc,
                )
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "Unable to collect Supervisor/add-on inventory for site %s",
                self._site_id,
                exc_info=True,
            )
        finally:
            self._addons_update_in_progress = False

    @callback
    def _schedule_integrations_update(self) -> None:
        """Schedule integration inventory collection without overlap."""
        if (
            not self._connected.is_set()
            or self._integrations_update_in_progress
        ):
            return

        self._integrations_update_in_progress = True
        self._hass.async_create_task(
            self._async_publish_integrations(),
            "control_room_agent_integrations",
        )

    async def _async_publish_integrations(self) -> None:
        """Collect and publish the integration inventory."""
        try:
            inventory = await async_collect_integration_inventory(self._hass)

            if not self._connected.is_set():
                return

            payload = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "site_id": self._site_id,
                **inventory,
            }

            result = self._client.publish(
                self._integrations_topic,
                payload=json.dumps(payload, separators=(",", ":")),
                qos=1,
                retain=True,
            )
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                _LOGGER.debug(
                    "Integration inventory publish failed for site %s: rc=%s",
                    self._site_id,
                    result.rc,
                )
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "Unable to collect integration inventory for site %s",
                self._site_id,
                exc_info=True,
            )
        finally:
            self._integrations_update_in_progress = False

    @callback
    def _schedule_system_update(self) -> None:
        """Schedule system metric collection without overlapping runs."""
        if (
            not self._connected.is_set()
            or self._system_update_in_progress
        ):
            return

        self._system_update_in_progress = True
        self._hass.async_create_task(
            self._async_publish_system(),
            "control_room_agent_system_metrics",
        )

    async def _async_publish_system(self) -> None:
        """Collect system metrics in the executor and publish them."""
        try:
            metrics = await self._hass.async_add_executor_job(
                collect_system_metrics,
                self._config_dir,
            )

            if not self._connected.is_set():
                return

            payload = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "site_id": self._site_id,
                **metrics,
            }

            result = self._client.publish(
                self._system_topic,
                payload=json.dumps(payload, separators=(",", ":")),
                qos=1,
                retain=True,
            )
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                _LOGGER.debug(
                    "System metrics publish failed for site %s: rc=%s",
                    self._site_id,
                    result.rc,
                )
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "Unable to collect system metrics for site %s",
                self._site_id,
                exc_info=True,
            )
        finally:
            self._system_update_in_progress = False

    def _publish_homeassistant_info(self) -> None:
        """Publish retained Home Assistant instance metadata."""
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "site_id": self._site_id,
            "home_assistant_version": HA_VERSION,
            "agent_version": VERSION,
            "started_at": self._process_started_at.isoformat(),
            **self._system_info,
        }

        result = self._client.publish(
            self._homeassistant_topic,
            payload=json.dumps(payload, separators=(",", ":")),
            qos=1,
            retain=True,
        )
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            _LOGGER.debug(
                "Home Assistant metadata publish failed for site %s: rc=%s",
                self._site_id,
                result.rc,
            )

    def _publish_heartbeat(self) -> None:
        """Publish current liveness and runtime metadata."""
        now = datetime.now(timezone.utc)
        uptime_seconds = max(
            0,
            int((now - self._process_started_at).total_seconds()),
        )

        payload = {
            "timestamp": now.isoformat(),
            "site_id": self._site_id,
            "session_id": self._session_id,
            "agent_version": VERSION,
            "home_assistant_version": HA_VERSION,
            "home_assistant_uptime_seconds": uptime_seconds,
        }

        result = self._client.publish(
            self._heartbeat_topic,
            payload=json.dumps(payload, separators=(",", ":")),
            qos=1,
            retain=True,
        )
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            _LOGGER.debug(
                "Heartbeat publish failed for site %s: rc=%s",
                self._site_id,
                result.rc,
            )
