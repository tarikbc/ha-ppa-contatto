"""Support for PPA Contatto covers (gates and doors).

This module uses direct WebSocket connection for real-time updates instead of HTTP polling.
The Socket.IO handshake sequence: 0{handshake} → 40 → 40{namespace} → 2/3 ping/pong + events.
When a gate/door is controlled, the state updates come through 'device/status' events over
WebSocket in Socket.IO format: 42["device/status",{"serial":"PO21CE63","status":{"gate":"open","relay":"off"}}].
This eliminates the need for multiple delayed HTTP requests to check status changes.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from homeassistant.components.cover import (
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import PPAContattoAPI
from .config_entities import get_device_display_name
from .const import DEVICE_TYPE_GATE, DEVICE_TYPE_RELAY, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the PPA Contatto cover platform."""

    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
    api = hass.data[DOMAIN][config_entry.entry_id]["api"]

    entities = []

    # Create cover entities for each device
    for device in coordinator.data.get("devices", []):
        serial = device.get("serial")
        if not serial:
            continue

        # Add gate cover if it's shown
        if device.get("name", {}).get("gate", {}).get("show", False):
            entities.append(
                PPAContattoCover(
                    coordinator,
                    api,
                    device,
                    DEVICE_TYPE_GATE,
                    f"{serial}_gate",
                    device.get("name", {}).get("gate", {}).get("name", "Gate"),
                    CoverDeviceClass.GATE,
                )
            )

        # Add door cover if relay is shown (relay controls door)
        if device.get("name", {}).get("relay", {}).get("show", False):
            entities.append(
                PPAContattoCover(
                    coordinator,
                    api,
                    device,
                    DEVICE_TYPE_RELAY,
                    f"{serial}_door",
                    device.get("name", {}).get("relay", {}).get("name", "Door"),
                    CoverDeviceClass.GATE,
                )
            )

    async_add_entities(entities)


class PPAContattoCover(CoordinatorEntity, CoverEntity):
    """Representation of a PPA Contatto cover (gate or door)."""

    def __init__(
        self,
        coordinator,
        api: PPAContattoAPI,
        device: Dict[str, Any],
        device_type: str,
        unique_id: str,
        name: str,
        device_class: CoverDeviceClass,
    ) -> None:
        """Initialize the cover."""
        super().__init__(coordinator)
        self._api = api
        self._device = device
        self._device_type = device_type
        self._attr_unique_id = unique_id
        self._attr_name = name
        self._serial = device.get("serial")
        self._attr_device_class = device_class

        # Initialize relay-specific attributes for all entities to avoid AttributeError
        self._relay_duration: Optional[int] = None
        self._momentary_active: bool = False
        self._momentary_task: Optional[asyncio.Task] = None

        # Set device info with dynamic name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            name=get_device_display_name(device),
            manufacturer="PPA Contatto",
            model="Gate Controller",
            sw_version=device.get("version"),
            serial_number=self._serial,
            configuration_url="https://brands.home-assistant.io/ppa_contatto/icon.png",
        )

        # Cover supports open and close
        self._attr_supported_features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE

    @property
    def is_closed(self) -> Optional[bool]:
        """Return if the cover is closed."""
        device = self._get_device_data()
        if not device:
            return None

        # Try to get status from latest reports first (more accurate)
        latest_status = device.get("latest_status", {})

        if self._device_type == DEVICE_TYPE_GATE:
            # Gates: closed = "closed", open = "open"
            latest_gate_status = latest_status.get("gate")
            if latest_gate_status is not None:
                return latest_gate_status == "closed"
            # Fallback to device status
            return device.get("status", {}).get("gate") == "closed"

        elif self._device_type == DEVICE_TYPE_RELAY:
            # For doors controlled by relay, behavior depends on duration setting
            if self._relay_duration == -1:
                # Toggle mode - use actual relay state
                latest_relay_status = latest_status.get("relay")
                if latest_relay_status is not None:
                    return latest_relay_status == "off"
                return device.get("status", {}).get("relay") == "off"
            else:
                # Momentary mode - door is "closed" when not being activated
                return not self._momentary_active

        return None

    @property
    def name(self) -> str:
        """Return the current entity name from coordinator data."""
        device = self._get_device_data()
        if not device:
            return self._attr_name  # Fallback to original name

        # Get the current name from device data
        name_config = device.get("name", {}).get(self._device_type, {})
        current_name = name_config.get("name", "")

        if current_name:
            return current_name

        # Fallback to default names if no custom name is set
        if self._device_type == DEVICE_TYPE_GATE:
            return "Gate"
        else:  # DEVICE_TYPE_RELAY
            return "Door"

    @property
    def is_opening(self) -> bool:
        """Return if the cover is opening."""
        # For momentary doors, show as opening when momentary is active
        if self._device_type == DEVICE_TYPE_RELAY and self._relay_duration != -1:
            return self._momentary_active

        # For gates, rely on WebSocket for state - no intermediate states
        return False

    @property
    def is_closing(self) -> bool:
        """Return if the cover is closing."""
        # For gates, rely on WebSocket for state - no intermediate states
        return False

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        # Check if coordinator has valid data (API is responding)
        if not self.coordinator.last_update_success:
            return False

        device = self._get_device_data()
        if not device:
            return False
        return device.get("online", False) and device.get("authorized", False)

    def _get_device_data(self) -> Optional[Dict[str, Any]]:
        """Get current device data from coordinator."""
        devices = self.coordinator.data.get("devices", [])
        for device in devices:
            if device.get("serial") == self._serial:
                return device
        return None

    async def async_update(self) -> None:
        """Update relay duration for doors."""
        if self._device_type == DEVICE_TYPE_RELAY:
            self._relay_duration = await self._get_relay_duration()

    async def _get_relay_duration(self) -> Optional[int]:
        """Get the current relay duration setting."""
        if self._device_type != DEVICE_TYPE_RELAY:
            return None

        try:
            config = await self._api.get_device_configuration(self._serial)
            return config.get("config", {}).get("relayDuration", 1000)  # Default to 1000ms
        except Exception as err:
            _LOGGER.debug("Failed to get relay duration for %s: %s", self._serial, err)
            return 1000  # Default to momentary behavior

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the cover."""
        try:
            # For momentary doors, implement button behavior
            if self._device_type == DEVICE_TYPE_RELAY and self._relay_duration != -1:
                await self._activate_momentary_door()
            else:
                # Simple behavior for gates and toggle doors - always trigger
                await self._api.control_device(self._serial, self._device_type)
                _LOGGER.debug("Successfully triggered %s %s", self._device_type, self._serial)

        except Exception as err:
            _LOGGER.error("Failed to open %s %s: %s", self._device_type, self._serial, err)
            raise

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the cover."""
        if self._device_type == DEVICE_TYPE_RELAY and self._relay_duration == -1:
            # Toggle door in switch mode
            await self._api.control_device(self._serial, self._device_type)
            _LOGGER.debug("Successfully triggered toggle door %s", self._serial)
        elif self._device_type == DEVICE_TYPE_GATE:
            # Simple behavior for gates - always trigger (same as open)
            await self._api.control_device(self._serial, self._device_type)
            _LOGGER.debug("Successfully triggered gate %s", self._serial)
        else:
            # For momentary doors, closing is the same as opening
            await self.async_open_cover(**kwargs)

    async def _activate_momentary_door(self) -> None:
        """Activate door in momentary button mode."""
        # Cancel any existing momentary task
        if self._momentary_task and not self._momentary_task.done():
            self._momentary_task.cancel()

        # Immediately show door as "opening"
        self._momentary_active = True
        self.async_write_ha_state()

        try:
            # Send the activation request
            await self._api.control_device(self._serial, self._device_type)
            _LOGGER.debug("Successfully activated momentary door %s", self._serial)

            # Create task to reset door after duration
            duration_seconds = (self._relay_duration or 1000) / 1000.0  # Convert ms to seconds
            self._momentary_task = asyncio.create_task(self._reset_momentary_door(duration_seconds))

        except Exception as err:
            # If API call failed, immediately reset door
            self._momentary_active = False
            self.async_write_ha_state()
            raise

    async def _reset_momentary_door(self, duration_seconds: float) -> None:
        """Reset momentary door after duration."""
        try:
            # Wait for the relay duration
            await asyncio.sleep(duration_seconds)

            # Set door back to "closed"
            self._momentary_active = False
            self.async_write_ha_state()

            # WebSocket will handle real-time updates, only do fallback refresh if WebSocket is not connected
            if not self._api._websocket_connected:
                _LOGGER.debug("WebSocket not connected, doing fallback refresh for momentary door %s", self._serial)
                await self.coordinator.async_request_refresh()

        except asyncio.CancelledError:
            # Task was cancelled, still reset the door
            self._momentary_active = False
            self.async_write_ha_state()
        except Exception as err:
            _LOGGER.error("Error in momentary door reset for %s: %s", self._serial, err)
            self._momentary_active = False
            self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return device specific state attributes."""
        device = self._get_device_data()
        if not device:
            return {}

        attrs = {
            "device_id": device.get("deviceId"),
            "mac_address": device.get("macAddress"),
            "version": device.get("version"),
            "role": device.get("role"),
            "favorite": device.get("favorite", False),
        }

        # Add status info from device
        status = device.get("status", {})
        if self._device_type == DEVICE_TYPE_GATE:
            attrs["gate_status"] = status.get("gate")
        else:
            attrs["door_status"] = status.get("relay")

        # Add enhanced status from reports
        latest_status = device.get("latest_status", {})
        if latest_status.get("last_action"):
            attrs["last_action"] = latest_status["last_action"]
        if latest_status.get("last_user"):
            attrs["last_user"] = latest_status["last_user"]

        # Add latest status for comparison
        if self._device_type == DEVICE_TYPE_GATE and latest_status.get("gate"):
            attrs["latest_gate_status"] = latest_status["gate"]
        elif self._device_type == DEVICE_TYPE_RELAY:
            if latest_status.get("relay"):
                attrs["latest_door_status"] = latest_status["relay"]

            # Add door behavior info based on duration setting
            if self._relay_duration is not None:
                attrs["door_duration_ms"] = self._relay_duration
                if self._relay_duration == -1:
                    attrs["behavior"] = "toggle_door"
                    attrs["mode"] = "on_off_door"
                    attrs["note"] = "Door acts as toggle - stays open/closed when activated"
                else:
                    attrs["behavior"] = "momentary_door"
                    attrs["mode"] = "momentary"
                    attrs["note"] = f"Door acts as momentary - {self._relay_duration}ms pulse when activated"
            else:
                attrs["behavior"] = "unknown"
                attrs["note"] = "Configure door duration in device settings"

        return attrs

    async def async_will_remove_from_hass(self) -> None:
        """Clean up when entity is removed."""
        if self._momentary_task and not self._momentary_task.done():
            self._momentary_task.cancel()
            try:
                await self._momentary_task
            except asyncio.CancelledError:
                pass
