"""Support for PPA Contatto switches."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import PPAContattoAPI
from .const import DEVICE_TYPE_GATE, DEVICE_TYPE_RELAY, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the PPA Contatto switch platform."""
    from .config_entities import async_setup_entry as config_setup_entry

    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
    api = hass.data[DOMAIN][config_entry.entry_id]["api"]

    entities = []

    # Create control switches for each device
    for device in coordinator.data.get("devices", []):
        serial = device.get("serial")
        if not serial:
            continue

        # Add gate switch if it's shown
        if device.get("name", {}).get("gate", {}).get("show", False):
            entities.append(
                PPAContattoSwitch(
                    coordinator,
                    api,
                    device,
                    DEVICE_TYPE_GATE,
                    f"{serial}_gate",
                    device.get("name", {}).get("gate", {}).get("name", "Gate"),
                )
            )

        # Add relay switch if it's shown
        if device.get("name", {}).get("relay", {}).get("show", False):
            entities.append(
                PPAContattoSwitch(
                    coordinator,
                    api,
                    device,
                    DEVICE_TYPE_RELAY,
                    f"{serial}_relay",
                    device.get("name", {}).get("relay", {}).get("name", "Relay"),
                )
            )

    async_add_entities(entities)

    # Also add configuration switches
    await config_setup_entry(hass, config_entry, async_add_entities, "switch")


class PPAContattoSwitch(CoordinatorEntity, SwitchEntity):
    """Representation of a PPA Contatto switch."""

    def __init__(
        self,
        coordinator,
        api: PPAContattoAPI,
        device: Dict[str, Any],
        device_type: str,
        unique_id: str,
        name: str,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self._api = api
        self._device = device
        self._device_type = device_type
        self._attr_unique_id = unique_id
        self._attr_name = name
        self._serial = device.get("serial")

        # Set device info
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._serial)},
            name=f"PPA Contatto {self._serial}",
            manufacturer="PPA Contatto",
            model="Gate Controller",
            sw_version=device.get("version"),
            serial_number=self._serial,
            configuration_url="https://play-lh.googleusercontent.com/qDtSOerKV_rVZ2ZMi_-pFe7jccoGVH0aHDbykUAQeE15_UoWa0Ej1dKt3FfaQCh1PoI=w480-h960-rw",
        )

        # Set entity description based on device type
        if self._device_type == DEVICE_TYPE_RELAY:
            self._attr_entity_registry_enabled_default = True
            # For relays, add description to clarify momentary behavior
            self._relay_is_momentary = True

    @property
    def is_on(self) -> bool:
        """Return true if the switch is on."""
        device = self._get_device_data()
        if not device:
            return False

        # Try to get status from latest reports first (more accurate)
        latest_status = device.get("latest_status", {})

        if self._device_type == DEVICE_TYPE_GATE:
            # Gates can stay open for extended periods
            # Check latest status first, then fall back to device status
            latest_gate_status = latest_status.get("gate")
            if latest_gate_status is not None:
                return latest_gate_status == "open"
            # Fallback to device status
            return device.get("status", {}).get("gate") == "open"

        elif self._device_type == DEVICE_TYPE_RELAY:
            # Relays are momentary buttons - they're "on" very briefly when activated
            # Most of the time they should show as "off"
            # Check latest status first, then fall back to device status
            latest_relay_status = latest_status.get("relay")
            if latest_relay_status is not None:
                return latest_relay_status == "on"
            # Fallback to device status
            return device.get("status", {}).get("relay") == "on"

        return False

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        device = self._get_device_data()
        if not device:
            return False
        return device.get("online", False) and device.get("authorized", False)

    @property
    def icon(self) -> str:
        """Return the icon to use in the frontend."""
        if self._device_type == DEVICE_TYPE_GATE:
            return "mdi:gate" if self.is_on else "mdi:gate-and"
        else:  # relay (momentary button)
            return "mdi:gesture-tap-button" if self.is_on else "mdi:radiobox-blank"

    def _get_device_data(self) -> Optional[Dict[str, Any]]:
        """Get current device data from coordinator."""
        devices = self.coordinator.data.get("devices", [])
        for device in devices:
            if device.get("serial") == self._serial:
                return device
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        try:
            await self._api.control_device(self._serial, self._device_type)
            _LOGGER.debug("Successfully activated %s %s", self._device_type, self._serial)

            # Schedule a delayed refresh to allow device status to update
            # Use asyncio.create_task to not block the response
            if hasattr(self.coordinator, "async_request_refresh_with_delay"):
                asyncio.create_task(self.coordinator.async_request_refresh_with_delay(1.5))
            else:
                # Fallback to immediate refresh
                await self.coordinator.async_request_refresh()

        except Exception as err:
            _LOGGER.error("Failed to turn on %s %s: %s", self._device_type, self._serial, err)
            raise

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        # For gates and relays, turning "off" is the same as turning "on"
        # (it's a momentary action - like pressing a button)
        await self.async_turn_on(**kwargs)

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
            attrs["relay_status"] = status.get("relay")

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
                attrs["latest_relay_status"] = latest_status["relay"]
            # Add note about momentary behavior
            attrs["behavior"] = "momentary_button"
            attrs["note"] = "Relay acts as momentary button - briefly shows 'on' when activated"

        return attrs
