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
        self._websocket_watchdog_task: Optional[asyncio.Task] = None
        # Lock that serializes ALL WebSocket connect/reconnect/cleanup calls.
        # Both the watchdog and the poll loop previously raced to reconnect
        # the WS simultaneously, creating duplicate connections and orphaned
        # listener tasks. With this lock, only one path can touch the WS at
        # a time.
        self._ws_reconnect_lock = asyncio.Lock()
        # Monotonic timestamp of the last successful ``_async_update_data``
        # return. The watchdog uses this to detect a dead coordinator poll
        # loop (e.g. stuck HTTP request, HA backoff growing too large).
        self._last_successful_poll: float = 0.0
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),  # Fallback polling interval
        )

        # Set up WebSocket callback for real-time updates
        self.api.set_device_update_callback(self._handle_device_update)

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

    async def _safe_ws_reconnect(self, reason: str) -> bool:
        """Reconnect the WebSocket under the shared lock.

        Returns True if the WS ended up connected (either because we
        successfully reconnected, or because a concurrent call already
        fixed it while we waited for the lock).
        """
        async with self._ws_reconnect_lock:
            # Re-check after acquiring the lock — another coroutine may
            # have fixed the WS while we were waiting.
            if self.api._websocket_connected and not self.api.websocket_is_stale():  # noqa: SLF001
                _LOGGER.debug("WS already healthy after acquiring lock (reason was: %s)", reason)
                return True
            _LOGGER.warning("WebSocket reconnecting: %s", reason)
            return await self.api.force_websocket_reconnect(reason)

    async def _safe_ws_ensure(self) -> bool:
        """Ensure WS is connected, under the shared lock."""
        async with self._ws_reconnect_lock:
            if self.api._websocket_connected:  # noqa: SLF001
                return True
            _LOGGER.warning("WebSocket disconnected — reconnecting via watchdog")
            return await self.api.ensure_websocket_connected()

    async def _websocket_watchdog(self) -> None:
        """Periodically verify the WebSocket is alive and reconnect if not."""
        _LOGGER.info("WebSocket watchdog started (interval=%ss)", WEBSOCKET_HEALTH_CHECK_INTERVAL)
        try:
            while True:
                try:
                    if not self._websocket_started:
                        async with self._ws_reconnect_lock:
                            if await self.api.start_websocket():
                                _LOGGER.info("WebSocket connection established via watchdog")
                                self._websocket_started = True
                    else:
                        if self.api.websocket_is_stale():
                            ok = await self._safe_ws_reconnect("no frames received within stale-timeout window")
                            if ok:
                                _LOGGER.info("WebSocket recovered after stale detection")
                            else:
                                _LOGGER.warning("WebSocket reconnect FAILED after stale detection")
                        elif not self.api._websocket_connected:  # noqa: SLF001
                            ok = await self._safe_ws_ensure()
                            if ok:
                                _LOGGER.info("WebSocket recovered after disconnect")
                            else:
                                _LOGGER.warning("WebSocket reconnect FAILED after disconnect")

                    # --- Poll-loop health check ---
                    # If the coordinator poll hasn't succeeded for more than
                    # 3x the current update_interval, something is stuck.
                    # Force a refresh to break out of any HA backoff spiral.
                    if self._last_successful_poll > 0:
                        poll_age = time.monotonic() - self._last_successful_poll
                        expected = (self.update_interval or timedelta(seconds=UPDATE_INTERVAL)).total_seconds()
                        if poll_age > expected * 3:
                            _LOGGER.warning(
                                "Coordinator poll appears stuck (last success %.0fs ago, expected every %.0fs) "
                                "— forcing refresh",
                                poll_age,
                                expected,
                            )
                            # Reset interval to short polling so we recover fast.
                            self.update_interval = timedelta(seconds=UPDATE_INTERVAL)
                            await self.async_request_refresh()

                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("WebSocket watchdog iteration error: %s", err)
                await asyncio.sleep(WEBSOCKET_HEALTH_CHECK_INTERVAL)
        except asyncio.CancelledError:
            _LOGGER.debug("WebSocket watchdog cancelled")
            raise

    def _handle_device_update(self, device_serial: str, update_data: Dict[str, Any]) -> None:
        """Handle real-time device updates from WebSocket."""
        try:
            # Update the device data in our coordinator's data
            if self.data and "devices" in self.data:
                devices = self.data["devices"]
                for device in devices:
                    if device.get("serial") == device_serial:
                        # Track if anything actually changed
                        data_changed = False

                        # Update device with new status data from WebSocket
                        latest_status = device.setdefault("latest_status", {})
                        device_status = device.setdefault("status", {})

                        # Check and update gate status if changed
                        if "gate" in update_data and update_data["gate"] is not None:
                            current_gate = latest_status.get("gate")
                            new_gate = update_data["gate"]
                            if current_gate != new_gate:
                                latest_status["gate"] = new_gate
                                device_status["gate"] = new_gate
                                data_changed = True

                        # Check and update relay status if changed
                        if "relay" in update_data and update_data["relay"] is not None:
                            current_relay = latest_status.get("relay")
                            new_relay = update_data["relay"]
                            if current_relay != new_relay:
                                latest_status["relay"] = new_relay
                                device_status["relay"] = new_relay
                                data_changed = True

                        if data_changed:
                            latest_status["last_action"] = datetime.now(timezone.utc).isoformat()
                            if not latest_status.get("last_user"):
                                latest_status["last_user"] = "WebSocket"
                            self.async_set_updated_data(self.data)
                        break

        except Exception as err:
            _LOGGER.error("Error handling WebSocket device update: %s", err)

    async def _async_update_data(self) -> Dict[str, Any]:
        """Update data via library.

        IMPORTANT: WebSocket management is handled EXCLUSIVELY by the
        watchdog task. This method only polls the REST API for device
        data. Mixing WS reconnection into the poll loop caused race
        conditions (Bug A, v1.5.5) and prevented the poll interval
        from resetting when the WS died (Bug B).
        """
        try:
            _LOGGER.debug("Starting data update from PPA Contatto API")

            # If the WS was once connected but isn't any more, reset the
            # poll interval to fast-polling so entities stay fresh while
            # the watchdog works on reconnecting.
            if self._websocket_started and not self.api._websocket_connected:  # noqa: SLF001
                if self.update_interval and self.update_interval > timedelta(seconds=UPDATE_INTERVAL):
                    _LOGGER.warning(
                        "WebSocket is down — resetting poll interval from %s to %ss",
                        self.update_interval,
                        UPDATE_INTERVAL,
                    )
                    self.update_interval = timedelta(seconds=UPDATE_INTERVAL)
            elif self._websocket_started and self.api._websocket_connected:  # noqa: SLF001
                # WS is healthy — keep the long interval.
                if self.update_interval and self.update_interval < timedelta(minutes=5):
                    _LOGGER.info("WebSocket healthy — extending poll interval to 5 min")
                    self.update_interval = timedelta(minutes=5)

            devices = await self.api.get_devices()

            # Enhance device data with latest status from reports
            enhanced_devices = []
            for device in devices:
                serial = device.get("serial")
                if serial:
                    try:
                        latest_status = await self.api.get_latest_device_status(serial)
                        device = device.copy()  # Don't modify original
                        device["latest_status"] = latest_status
                    except Exception as err:
                        _LOGGER.debug("Could not get latest status for %s: %s", serial, err)
                        device["latest_status"] = {
                            "gate": None,
                            "relay": None,
                            "last_action": None,
                            "last_user": None,
                        }

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
