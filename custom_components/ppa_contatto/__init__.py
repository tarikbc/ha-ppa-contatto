"""The PPA Contatto integration."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any, Dict

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import PPAContattoAPI, PPAContattoAPIError, PPAContattoAuthError
from .const import DOMAIN, UPDATE_INTERVAL

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
        # Clean up WebSocket connection
        entry_data = hass.data[DOMAIN].get(entry.entry_id)
        if entry_data and "api" in entry_data:
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
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL),  # Fallback polling interval
        )

        # Set up WebSocket callback for real-time updates
        self.api.set_device_update_callback(self._handle_device_update)

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

                        # Only trigger coordinator update if data actually changed
                        if data_changed:
                            self.async_set_updated_data(self.data)
                        break

        except Exception as err:
            _LOGGER.error("Error handling WebSocket device update: %s", err)

    async def _async_update_data(self) -> Dict[str, Any]:
        """Update data via library."""
        try:
            _LOGGER.debug("Starting data update from PPA Contatto API")

            # Start WebSocket connection if not already started
            if not self._websocket_started:
                websocket_success = await self.api.start_websocket()
                if websocket_success:
                    _LOGGER.info("WebSocket connection established - switching to real-time updates")
                    self._websocket_started = True
                    # Increase polling interval since WebSocket provides real-time updates
                    self.update_interval = timedelta(minutes=5)  # Just for health checks
                else:
                    _LOGGER.warning("Failed to establish initial WebSocket connection - will retry automatically")
            else:
                # Ensure WebSocket is still connected (this will auto-reconnect if needed)
                websocket_reconnected = await self.api.ensure_websocket_connected()
                if not websocket_reconnected and self.api._websocket_connected:
                    # WebSocket is having issues, log but don't fail the update
                    _LOGGER.debug("WebSocket reconnection in progress - using polling fallback")

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
