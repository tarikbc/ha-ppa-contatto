"""Configuration entities for PPA Contatto settings."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from homeassistant.components.switch import SwitchEntity
from homeassistant.components.text import TextEntity
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
    entity_type: str,
) -> None:
    """Set up PPA Contatto configuration entities."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
    api = hass.data[DOMAIN][config_entry.entry_id]["api"]
    
    entities = []
    
    # Create configuration entities for each device
    for device in coordinator.data.get("devices", []):
        serial = device.get("serial")
        if not serial:
            continue
        
        if entity_type == "switch":
            # Configuration switches
            entities.extend([
                PPAContattoConfigSwitch(
                    coordinator, api, device, f"{serial}_favorite", 
                    "Favorite", "favorite"
                ),
                PPAContattoConfigSwitch(
                    coordinator, api, device, f"{serial}_notifications", 
                    "Notifications", "notification"
                ),
            ])
            
            # Show/hide switches for gate and relay
            if device.get("name", {}).get("gate"):
                entities.append(
                    PPAContattoVisibilitySwitch(
                        coordinator, api, device, f"{serial}_gate_visible",
                        "Gate Visible", DEVICE_TYPE_GATE
                    )
                )
            
            if device.get("name", {}).get("relay"):
                entities.append(
                    PPAContattoVisibilitySwitch(
                        coordinator, api, device, f"{serial}_relay_visible",
                        "Relay Visible", DEVICE_TYPE_RELAY
                    )
                )
                
        elif entity_type == "text":
            # Name configuration text entities
            if device.get("name", {}).get("gate"):
                entities.append(
                    PPAContattoNameText(
                        coordinator, api, device, f"{serial}_gate_name",
                        "Gate Name", DEVICE_TYPE_GATE
                    )
                )
            
            if device.get("name", {}).get("relay"):
                entities.append(
                    PPAContattoNameText(
                        coordinator, api, device, f"{serial}_relay_name",
                        "Relay Name", DEVICE_TYPE_RELAY
                    )
                )
    
    async_add_entities(entities)


class PPAContattoConfigBase(CoordinatorEntity):
    """Base class for PPA Contatto configuration entities."""

    def __init__(
        self,
        coordinator,
        api: PPAContattoAPI,
        device: Dict[str, Any],
        unique_id: str,
        name: str,
    ) -> None:
        """Initialize the configuration entity."""
        super().__init__(coordinator)
        self._api = api
        self._device = device
        self._attr_unique_id = unique_id
        self._attr_name = name
        self._serial = device.get("serial")
        self._attr_entity_category = EntityCategory.CONFIG
        
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

    def _get_device_data(self) -> Optional[Dict[str, Any]]:
        """Get current device data from coordinator."""
        devices = self.coordinator.data.get("devices", [])
        for device in devices:
            if device.get("serial") == self._serial:
                return device
        return None

    async def _update_device_setting(self, data: Dict[str, Any]) -> bool:
        """Update device setting via API."""
        try:
            success = await self._api.update_device_settings(self._serial, data)
            if success:
                # Request refresh to get updated data
                await self.coordinator.async_request_refresh()
            return success
        except Exception as err:
            _LOGGER.error("Failed to update device setting: %s", err)
            return False


class PPAContattoConfigSwitch(PPAContattoConfigBase, SwitchEntity):
    """Configuration switch for device settings."""

    def __init__(self, coordinator, api, device, unique_id, name, setting_key):
        """Initialize the configuration switch."""
        super().__init__(coordinator, api, device, unique_id, name)
        self._setting_key = setting_key
        
        # Set appropriate icons
        if setting_key == "favorite":
            self._attr_icon = "mdi:heart"
        elif setting_key == "notification":
            self._attr_icon = "mdi:bell"

    @property
    def is_on(self) -> bool:
        """Return true if the setting is enabled."""
        device = self._get_device_data()
        if not device:
            return False
        return device.get(self._setting_key, False)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the setting on."""
        await self._update_device_setting({self._setting_key: True})

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the setting off."""
        await self._update_device_setting({self._setting_key: False})


class PPAContattoVisibilitySwitch(PPAContattoConfigBase, SwitchEntity):
    """Switch to control gate/relay visibility."""

    def __init__(self, coordinator, api, device, unique_id, name, device_type):
        """Initialize the visibility switch."""
        super().__init__(coordinator, api, device, unique_id, name)
        self._device_type = device_type
        self._attr_icon = "mdi:eye" if device_type == DEVICE_TYPE_GATE else "mdi:eye-outline"

    @property
    def is_on(self) -> bool:
        """Return true if the device is visible."""
        device = self._get_device_data()
        if not device:
            return False
        
        name_config = device.get("name", {}).get(self._device_type, {})
        return name_config.get("show", False)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Make the device visible."""
        device = self._get_device_data()
        if not device:
            return
            
        # Preserve current name while setting show=True
        current_name = device.get("name", {}).get(self._device_type, {}).get("name", "")
        update_data = {
            "name": {
                self._device_type: {
                    "name": current_name,
                    "show": True
                }
            }
        }
        await self._update_device_setting(update_data)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Hide the device."""
        device = self._get_device_data()
        if not device:
            return
            
        # Preserve current name while setting show=False
        current_name = device.get("name", {}).get(self._device_type, {}).get("name", "")
        update_data = {
            "name": {
                self._device_type: {
                    "name": current_name,
                    "show": False
                }
            }
        }
        await self._update_device_setting(update_data)


class PPAContattoNameText(PPAContattoConfigBase, TextEntity):
    """Text entity for device name configuration."""

    def __init__(self, coordinator, api, device, unique_id, name, device_type):
        """Initialize the name text entity."""
        super().__init__(coordinator, api, device, unique_id, name)
        self._device_type = device_type
        self._attr_icon = "mdi:rename-box"
        self._attr_mode = "text"

    @property
    def native_value(self) -> Optional[str]:
        """Return the current device name."""
        device = self._get_device_data()
        if not device:
            return None
        
        name_config = device.get("name", {}).get(self._device_type, {})
        return name_config.get("name", "")

    async def async_set_value(self, value: str) -> None:
        """Set the device name."""
        device = self._get_device_data()
        if not device:
            return
            
        # Preserve current show setting while updating name
        current_show = device.get("name", {}).get(self._device_type, {}).get("show", True)
        update_data = {
            "name": {
                self._device_type: {
                    "name": value,
                    "show": current_show
                }
            }
        }
        await self._update_device_setting(update_data) 