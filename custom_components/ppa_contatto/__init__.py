"""The PPA Contatto integration."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import PPAContattoAPI, PPAContattoAPIError, PPAContattoAuthError
from .const import DOMAIN, UPDATE_INTERVAL, WEBSOCKET_HEALTH_CHECK_INTERVAL

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.COVER,
    Platform.SWITCH,
    Platform.SENSOR,
    Platform.TEXT,
    Platform.NUMBER,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up PPA Contatto from a config entry."""
    api = PPAContattoAPI(hass, entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD], config_entry=entry)

    coordinator = PPAContattoDataUpdateCoordinator(hass, api)

    # Fetch initial data
    await coordinator.async_config_entry_first_refresh()

    # Start the WebSocket health watchdog — runs independently of the data
    # update coordinator so a dropped WebSocket is detected within seconds
    # instead of having to wait for the next (potentially 5-minute) poll.
    coordinator.start_websocket_watchdog()

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        entry_data = hass.data[DOMAIN].get(entry.entry_id)
        if entry_data:
            # Stop the WebSocket watchdog first so it doesn't race to
            # reconnect while we're tearing the connection down.
            coordinator: Optional["PPAContattoDataUpdateCoordinator"] = entry_data.get("coordinator")
            if coordinator is not None:
                await coordinator.stop_websocket_watchdog()

            # Clean up WebSocket connection
            if "api" in entry_data:
                api = entry_data["api"]
                await api.stop_websocket()

        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


class PPAContattoDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    def __init__(self, hass: HomeAssistant, api: PPAContattoAPI) -> None:
        """Initialize."""
        self.api = api
        self._websocket_started = False
        self._websocket_was_connected: bool = False  # for transition-only logging
        self._websocket_watchdog_task: Optional[asyncio.Task] = None
        # Lock that serializes ALL WebSocket connect/reconnect/cleanup calls.
        self._ws_reconnect_lock = asyncio.Lock()
        # Monotonic timestamp of the last successful ``_async_update_data`` return.
        self._last_successful_poll: float = 0.0
        # Per-device replay anchor: the highest report.id we've already processed.
        # WS events don't carry IDs, so they don't update this directly — but the
        # next poll-replay will see them in the report log and either no-op (state
        # already matches) or fire the missed transition. Persisted across HA restarts
        # via the coordinator's ``data`` payload.
        self._last_event_id: Dict[str, int] = {}
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),  # Fallback polling interval
        )

        # Real-time WebSocket pushes flow through the same handler as event replay.
        self.api.set_device_update_callback(self._handle_device_update)
        # Whenever the WS namespace finishes (re)connecting, immediately replay any
        # events the cloud delivered while we were disconnected.
        self.api.set_on_websocket_connected_callback(self._on_websocket_connected)

    def start_websocket_watchdog(self) -> None:
        """Start the independent WebSocket health watchdog task."""
        if self._websocket_watchdog_task and not self._websocket_watchdog_task.done():
            return
        self._websocket_watchdog_task = self.hass.loop.create_task(self._websocket_watchdog())

    async def stop_websocket_watchdog(self) -> None:
        """Cancel the WebSocket watchdog task if running."""
        task = self._websocket_watchdog_task
        self._websocket_watchdog_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Error awaiting cancelled WebSocket watchdog: %s", err)

    async def _safe_ws_action(self, *, reason: Optional[str] = None, force: bool = False) -> bool:
        """Single guarded entry point for connect / ensure / force-reconnect.

        ``force=True`` tears down any existing socket first (used when stale-detection
        wants a fresh connection even if aiohttp still thinks we're connected).
        Otherwise we either no-op (already connected) or call ensure_websocket_connected
        for the standard backoff path.
        """
        async with self._ws_reconnect_lock:
            # Re-check under the lock — a concurrent caller may have already fixed it.
            if self.api._websocket_connected and not self.api.websocket_is_stale():  # noqa: SLF001
                if force:
                    _LOGGER.warning("WebSocket force-reconnect requested: %s", reason)
                    return await self.api.force_websocket_reconnect(reason or "force")
                return True
            if force:
                _LOGGER.warning("WebSocket force-reconnect: %s", reason)
                return await self.api.force_websocket_reconnect(reason or "force")
            return await self.api.ensure_websocket_connected()

    def _on_websocket_connected(self) -> None:
        """Hook fired when the WS namespace handshake completes.

        Schedules an immediate catch-up poll so we replay any events delivered while
        we were disconnected. Returning from a callback path → use ``async_request_refresh``
        which is non-blocking.
        """
        _LOGGER.debug("WS namespace connected — kicking immediate catch-up refresh")
        self.hass.async_create_task(self.async_request_refresh())

    async def _websocket_watchdog(self) -> None:
        """Periodically verify the WebSocket is alive and reconnect if not.

        v1.6.0: logs on STATE TRANSITION only — chronic-flap log spam (the v1.5.x
        "983 disconnected lines" issue) is replaced with a single line per real
        disconnect/recovery edge.
        """
        _LOGGER.info("WebSocket watchdog started (interval=%ss)", WEBSOCKET_HEALTH_CHECK_INTERVAL)
        try:
            while True:
                try:
                    if not self._websocket_started:
                        async with self._ws_reconnect_lock:
                            if await self.api.start_websocket():
                                _LOGGER.info("WebSocket connection established via watchdog")
                                self._websocket_started = True
                                self._websocket_was_connected = True
                    else:
                        currently = self.api._websocket_connected  # noqa: SLF001

                        if self._websocket_was_connected and not currently:
                            _LOGGER.warning(
                                "WebSocket dropped — reconnecting (last close code=%s reason=%r)",
                                self.api._last_close_code,  # noqa: SLF001
                                self.api._last_close_reason,  # noqa: SLF001
                            )

                        if self.api.websocket_is_stale():
                            await self._safe_ws_action(
                                reason="no frames received within stale-timeout window",
                                force=True,
                            )
                        elif not currently:
                            await self._safe_ws_action()

                        # Edge-detect recovery for clean logging.
                        now_connected = self.api._websocket_connected  # noqa: SLF001
                        if now_connected and not self._websocket_was_connected:
                            _LOGGER.info("WebSocket reconnected")
                        self._websocket_was_connected = now_connected

                    # --- Poll-loop health check ---
                    if self._last_successful_poll > 0:
                        poll_age = time.monotonic() - self._last_successful_poll
                        expected = (self.update_interval or timedelta(seconds=UPDATE_INTERVAL)).total_seconds()
                        if poll_age > expected * 3:
                            _LOGGER.warning(
                                "Coordinator poll appears stuck (last success %.0fs ago, "
                                "expected every %.0fs) — forcing refresh",
                                poll_age,
                                expected,
                            )
                            self.update_interval = timedelta(seconds=UPDATE_INTERVAL)
                            await self.async_request_refresh()

                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("WebSocket watchdog iteration error: %s", err)
                await asyncio.sleep(WEBSOCKET_HEALTH_CHECK_INTERVAL)
        except asyncio.CancelledError:
            _LOGGER.debug("WebSocket watchdog cancelled")
            raise

    def _handle_device_update(
        self,
        device_serial: str,
        update_data: Dict[str, Any],
        *,
        source: str = "ws",
        report: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Idempotent state mutation, callable from WebSocket pushes OR event replay.

        State-based dedup: we only fire ``async_set_updated_data`` if the new value
        differs from the current cached value. So the same logical event arriving
        from both the WS fast-path and the poll-replay catch-up path is naturally
        a no-op the second time.

        ``source`` and ``report`` are diagnostic only.
        """
        try:
            if not (self.data and "devices" in self.data):
                return
            devices = self.data["devices"]
            for device in devices:
                if device.get("serial") != device_serial:
                    continue

                data_changed = False
                latest_status = device.setdefault("latest_status", {})
                device_status = device.setdefault("status", {})

                # Gate
                if "gate" in update_data and update_data["gate"] is not None:
                    current_gate = latest_status.get("gate")
                    new_gate = update_data["gate"]
                    if current_gate != new_gate:
                        latest_status["gate"] = new_gate
                        device_status["gate"] = new_gate
                        data_changed = True

                # Relay
                if "relay" in update_data and update_data["relay"] is not None:
                    current_relay = latest_status.get("relay")
                    new_relay = update_data["relay"]
                    if current_relay != new_relay:
                        latest_status["relay"] = new_relay
                        device_status["relay"] = new_relay
                        data_changed = True

                if data_changed:
                    # Prefer the report's metadata when this came from event replay.
                    if report is not None:
                        latest_status["last_action"] = report.get("created_at") or report.get("createdAt")
                        latest_status["last_user"] = report.get("user") or report.get("name") or source
                    else:
                        latest_status["last_action"] = datetime.now(timezone.utc).isoformat()
                        if not latest_status.get("last_user"):
                            latest_status["last_user"] = source
                    _LOGGER.info(
                        "Device %s state changed via %s: %s",
                        device_serial,
                        source,
                        {k: v for k, v in update_data.items() if v is not None},
                    )
                    self.async_set_updated_data(self.data)
                return
        except Exception as err:
            _LOGGER.error("Error handling device update: %s", err)

    async def _async_update_data(self) -> Dict[str, Any]:
        """Poll the report log for new events and merge with device list.

        v1.6.0 architecture: the PPA cloud's ``/device/{serial}/reports`` endpoint is
        the canonical event log. Each poll fetches reports newer than the highest
        ``report.id`` we've already processed and replays them through
        ``_handle_device_update`` (the same handler used by the WebSocket fast-path).

        Properties:
          • Bounded staleness regardless of WS health (≤ poll interval).
          • Zero lost events: anything missed during a WS outage is replayed on the
            next poll, in chronological order.
          • Idempotent: replayed events that match current state are no-ops.
          • Self-healing: a long disconnect just means a longer catch-up sweep.

        WebSocket management is handled EXCLUSIVELY by the watchdog task; this
        method only polls REST. Adaptive cadence: 15 s when WS is down, 5 min when
        healthy.
        """
        try:
            _LOGGER.debug("Starting data update from PPA Contatto API")

            # Adaptive poll cadence based on WS health.
            ws_started = self._websocket_started
            ws_connected = self.api._websocket_connected  # noqa: SLF001
            if ws_started and not ws_connected:
                if self.update_interval and self.update_interval > timedelta(seconds=UPDATE_INTERVAL):
                    _LOGGER.info(
                        "WebSocket is down — tightening poll interval from %s to %ss",
                        self.update_interval,
                        UPDATE_INTERVAL,
                    )
                    self.update_interval = timedelta(seconds=UPDATE_INTERVAL)
            elif ws_started and ws_connected:
                if self.update_interval and self.update_interval < timedelta(minutes=5):
                    _LOGGER.info("WebSocket healthy — extending poll interval to 5 min")
                    self.update_interval = timedelta(minutes=5)

            devices = await self.api.get_devices()

            enhanced_devices = []
            for device in devices:
                serial = device.get("serial")
                if not serial:
                    enhanced_devices.append(device)
                    continue

                # Inherit prior latest_status / last_event_id from the previous poll
                # so dedup works across coordinator runs.
                prior = None
                if self.data and "devices" in self.data:
                    prior = next((d for d in self.data["devices"] if d.get("serial") == serial), None)
                last_event_id = self._last_event_id.get(serial, 0)
                # First time we see this device: anchor on the newest report.id we
                # find right now so we don't replay ancient history.
                seed_anchor = last_event_id == 0

                try:
                    result = await self.api.fetch_device_events_since(serial, last_event_id)
                except Exception as err:
                    _LOGGER.debug("Could not fetch events for %s: %s", serial, err)
                    result = None

                device = device.copy()
                if result is not None:
                    if seed_anchor:
                        # First run: adopt the cloud's current view, anchor the replay
                        # cursor at the newest known event id so we don't replay
                        # ancient history on next poll.
                        self._last_event_id[serial] = result["newest_id"]
                        device["latest_status"] = result["latest_status"]
                    else:
                        # Replay events FIRST so transitions fire through
                        # _handle_device_update against the prior cached state
                        # (which is what's currently in self.data). DO NOT pre-set
                        # prior["latest_status"] — that would short-circuit the
                        # idempotent state diff and the transition would be lost.
                        for ev in result["events"]:
                            update = {ev["kind"]: ev["value"]}
                            self._handle_device_update(
                                serial,
                                update,
                                source="replay",
                                report={"created_at": ev["created_at"], "user": ev["user"]},
                            )
                        # The events have now mutated prior["latest_status"] to the
                        # final state (via _handle_device_update). Use the cloud's
                        # final view for the new enhanced_devices snapshot — it
                        # should match, and acts as a safety net if any event was
                        # malformed.
                        device["latest_status"] = result["latest_status"]
                        if result["newest_id"] > last_event_id:
                            self._last_event_id[serial] = result["newest_id"]
                else:
                    device["latest_status"] = (
                        prior.get("latest_status")
                        if prior
                        else {"gate": None, "relay": None, "last_action": None, "last_user": None}
                    )

                enhanced_devices.append(device)

            _LOGGER.debug("Successfully updated data for %d devices", len(enhanced_devices))
            self._last_successful_poll = time.monotonic()
            return {"devices": enhanced_devices}

        except asyncio.TimeoutError as err:
            _LOGGER.warning("PPA Contatto API timeout - server not responding")
            raise UpdateFailed("PPA Contatto API timeout - server not responding") from err
        except aiohttp.ClientConnectorError as err:
            _LOGGER.warning("Cannot connect to PPA Contatto API: %s", err)
            raise UpdateFailed(f"Cannot connect to PPA Contatto API: {err}") from err
        except aiohttp.ClientError as err:
            _LOGGER.warning("Network error connecting to PPA Contatto API: %s", err)
            raise UpdateFailed(f"Network error connecting to PPA Contatto API: {err}") from err
        except PPAContattoAuthError as err:
            _LOGGER.error("PPA Contatto authentication failed: %s", err)
            raise UpdateFailed(f"PPA Contatto authentication failed: {err}") from err
        except PPAContattoAPIError as err:
            _LOGGER.warning("PPA Contatto API error: %s", err)
            raise UpdateFailed(f"PPA Contatto API error: {err}") from err
        except Exception as err:
            _LOGGER.error("Unexpected error updating PPA Contatto data: %s", err)
            raise UpdateFailed(f"Unexpected error: {err}") from err

    async def async_request_refresh_with_delay(self, delay: float = 2.0) -> None:
        """Request refresh with a small delay to allow device status to update."""
        await asyncio.sleep(delay)
        await self.async_request_refresh()
