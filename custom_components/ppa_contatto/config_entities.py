"""Configuration entities for PPA Contatto settings."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from homeassistant.components.switch import SwitchEntity
from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import PPAContattoAPI
from .const import DEVICE_TYPE_GATE, DEVICE_TYPE_RELAY, DOMAIN


def get_device_display_name(device: Dict[str, Any]) -> str:
    """Get the display name for a device based on API data."""
    serial = device.get("serial", "Unknown")

    # Try to get custom names from the device
    gate_name = device.get("name", {}).get("gate", {}).get("name", "")
    relay_name = device.get("name", {}).get("relay", {}).get("name", "")

    # Use the first available custom name, or fall back to serial
    if gate_name and relay_name:
        return f"{gate_name} / {relay_name}"
    elif gate_name:
        return gate_name
    elif relay_name:
        return relay_name
    else:
        return f"PPA Contatto {serial}"


_LOGGER = logging.getLogger(__name__)


async def async_setup_config_switches(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PPA Contatto configuration switch entities."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
    api = hass.data[DOMAIN][config_entry.entry_id]["api"]

    entities = []

    # Create configuration entities for each device
    for device in coordinator.data.get("devices", []):
        serial = device.get("serial")
        if not serial:
            continue

        # Configuration switches
        entities.extend(
            [
                PPAContattoConfigSwitch(
                    coordinator,
                    api,
                    device,
                    f"{serial}_favorite",
                    "Favorite",
                    "favorite",
                ),
                PPAContattoConfigSwitch(
                    coordinator,
                    api,
                    device,
                    f"{serial}_notifications",
                    "Notifications",
                    "notification",
                ),
            ]
        )

        # Show/hide switches for gate and relay
        if device.get("name", {}).get("gate"):
            entities.append(
                PPAContattoVisibilitySwitch(
                    coordinator,
                    api,
                    device,
                    f"{serial}_gate_visible",
                    "Gate Visible",
                    DEVICE_TYPE_GATE,
                )
            )

        if device.get("name", {}).get("relay"):
            entities.append(
                PPAContattoVisibilitySwitch(
                    coordinator,
                    api,
                    device,
                    f"{serial}_relay_visible",
                    "Relay Visible",
                    DEVICE_TYPE_RELAY,
                )
            )

    async_add_entities(entities)


async def async_setup_config_texts(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PPA Contatto configuration text entities."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
    api = hass.data[DOMAIN][config_entry.entry_id]["api"]

    entities = []

    # Create configuration entities for each device
    for device in coordinator.data.get("devices", []):
        serial = device.get("serial")
        if not serial:
            continue

        # Name configuration text entities
        if device.get("name", {}).get("gate"):
            entities.append(
                PPAContattoNameText(
                    coordinator,
                    api,
                    device,
                    f"{serial}_gate_name",
                    "Gate Name",
                    DEVICE_TYPE_GATE,
                )
            )

        if device.get("name", {}).get("relay"):
            entities.append(
                PPAContattoNameText(
                    coordinator,
                    api,
                    device,
                    f"{serial}_relay_name",
                    "Relay Name",
                    DEVICE_TYPE_RELAY,
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

    def _get_device_data(self) -> Optional[Dict[str, Any]]:
        """Get current device data from coordinator."""
        devices = self.coordinator.data.get("devices", [])
        for device in devices:
            if device.get("serial") == self._serial:
                return device
        return None

    async def _build_complete_device_payload(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Build complete device payload preserving all existing settings."""
        device = self._get_device_data()
        if not device:
            return updates

        # Start with a complete payload containing all current device settings
        complete_payload = {}

        # Preserve name configuration (gate and relay)
        current_names = device.get("name", {})
        if current_names:
            complete_payload["name"] = {
                "gate": {
                    "name": current_names.get("gate", {}).get("name", ""),
                    "show": current_names.get("gate", {}).get("show", True),
                },
                "relay": {
                    "name": current_names.get("relay", {}).get("name", ""),
                    "show": current_names.get("relay", {}).get("show", True),
                },
            }

        # Preserve other device settings
        complete_payload["favorite"] = device.get("favorite", False)
        complete_payload["notification"] = device.get("notification", False)

        # Apply the updates on top of the complete payload
        if "name" in updates:
            # For name updates, merge carefully to preserve both gate and relay
            if "name" not in complete_payload:
                complete_payload["name"] = {}

            update_names = updates["name"]
            for device_type in ["gate", "relay"]:
                if device_type in update_names:
                    if device_type not in complete_payload["name"]:
                        complete_payload["name"][device_type] = {"name": "", "show": True}
                    complete_payload["name"][device_type].update(update_names[device_type])
        else:
            # For other updates, apply directly
            complete_payload.update(updates)

        _LOGGER.debug("Built complete device payload for %s: %s", self._serial, complete_payload)
        return complete_payload

    async def _update_device_setting(self, updates: Dict[str, Any]) -> bool:
        """Update device setting via API with complete payload."""
        try:
            # Always build and send the complete device configuration
            complete_payload = await self._build_complete_device_payload(updates)
            success = await self._api.update_device_settings(self._serial, complete_payload)
            return success
        except Exception as err:
            _LOGGER.error("Failed to update device setting: %s", err)
            return False

    async def _update_device_name_in_registry(self) -> None:
        """Update device name in Home Assistant device registry."""
        try:
            device = self._get_device_data()
            if not device:
                return

            # Get device registry
            device_registry = dr.async_get(self.hass)

            # Find our device in the registry
            ha_device = device_registry.async_get_device(identifiers={(DOMAIN, self._serial)})

            if ha_device:
                # Calculate new device name
                new_name = get_device_display_name(device)

                # Update device name if it changed
                if ha_device.name != new_name:
                    device_registry.async_update_device(ha_device.id, name=new_name)
                    _LOGGER.debug(
                        "Updated device name in registry: %s -> %s",
                        ha_device.name,
                        new_name,
                    )

        except Exception as err:
            _LOGGER.warning("Failed to update device name in registry: %s", err)


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
    def name(self) -> str:
        """Return the current entity name from coordinator data."""
        device = self._get_device_data()
        if not device:
            return self._attr_name  # Fallback to original name

        # For general device settings, use device name + setting type
        device_name = get_device_display_name(device)

        # Use clearer setting names
        if self._setting_key == "favorite":
            return f"{device_name} Favorite"
        elif self._setting_key == "notification":
            return f"{device_name} Notifications"
        else:
            return f"{device_name} {self._attr_name}"

    @property
    def is_on(self) -> bool:
        """Return true if the setting is enabled."""
        device = self._get_device_data()
        if not device:
            return False
        return device.get(self._setting_key, False)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the setting on."""
        _LOGGER.debug("Turning on %s for %s", self._setting_key, self._serial)
        success = await self._update_device_setting({self._setting_key: True})
        if success:
            await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the setting off."""
        _LOGGER.debug("Turning off %s for %s", self._setting_key, self._serial)
        success = await self._update_device_setting({self._setting_key: False})
        if success:
            await self.coordinator.async_request_refresh()


class PPAContattoVisibilitySwitch(PPAContattoConfigBase, SwitchEntity):
    """Switch to control gate/relay visibility."""

    def __init__(self, coordinator, api, device, unique_id, name, device_type):
        """Initialize the visibility switch."""
        super().__init__(coordinator, api, device, unique_id, name)
        self._device_type = device_type
        self._attr_icon = "mdi:eye" if device_type == DEVICE_TYPE_GATE else "mdi:eye-outline"

    @property
    def name(self) -> str:
        """Return the current entity name from coordinator data."""
        device = self._get_device_data()
        if not device:
            return self._attr_name  # Fallback to original name

        # For visibility switches, use clear labels based on device type
        if self._device_type == DEVICE_TYPE_GATE:
            gate_name = device.get("name", {}).get("gate", {}).get("name", "Gate")
            return f"Show {gate_name}"
        else:  # DEVICE_TYPE_RELAY
            relay_name = device.get("name", {}).get("relay", {}).get("name", "Door")
            return f"Show {relay_name}"

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

        # Build update for just this device type's visibility
        update_data = {"name": {self._device_type: {"show": True}}}

        success = await self._update_device_setting(update_data)
        if success:
            await self.coordinator.async_request_refresh()
            # Update device name in registry since visibility might affect display name
            await self._update_device_name_in_registry()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Hide the device."""
        device = self._get_device_data()
        if not device:
            return

        # Build update for just this device type's visibility
        update_data = {"name": {self._device_type: {"show": False}}}

        success = await self._update_device_setting(update_data)
        if success:
            await self.coordinator.async_request_refresh()
            # Update device name in registry since visibility might affect display name
            await self._update_device_name_in_registry()


class PPAContattoNameText(PPAContattoConfigBase, TextEntity):
    """Text entity for device name configuration."""

    def __init__(self, coordinator, api, device, unique_id, name, device_type):
        """Initialize the name text entity."""
        super().__init__(coordinator, api, device, unique_id, name)
        self._device_type = device_type
        self._attr_icon = "mdi:rename-box"
        self._attr_mode = "text"

    @property
    def name(self) -> str:
        """Return the current entity name from coordinator data."""
        device = self._get_device_data()
        if not device:
            return self._attr_name  # Fallback to original name

        # For name text entities, use simple clear labels
        if self._device_type == DEVICE_TYPE_GATE:
            return "Gate Name"
        else:  # DEVICE_TYPE_RELAY
            return "Door Name"

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

        # Build update for just this device type's name
        update_data = {"name": {self._device_type: {"name": value}}}

        _LOGGER.debug("Updating device name for %s %s: %s", self._serial, self._device_type, value)

        # Update the device setting (complete payload will be built automatically)
        success = await self._update_device_setting(update_data)

        if success:
            # After successful name update, fetch the latest device data to ensure
            # our storage reflects the current state from the API
            await self._refresh_device_names()

            # Update the device name in Home Assistant device registry
            await self._update_device_name_in_registry()

    async def _refresh_device_names(self) -> None:
        """Fetch latest device data and update coordinator storage."""
        try:
            # Get fresh device list from API
            fresh_devices = await self._api.get_devices()

            # Update coordinator data with fresh device information
            if hasattr(self.coordinator, "data") and "devices" in self.coordinator.data:
                # Find and update our specific device in the coordinator data
                for i, device in enumerate(self.coordinator.data["devices"]):
                    if device.get("serial") == self._serial:
                        # Find the corresponding fresh device data
                        for fresh_device in fresh_devices:
                            if fresh_device.get("serial") == self._serial:
                                # Preserve latest_status if it exists
                                if "latest_status" in device:
                                    fresh_device["latest_status"] = device["latest_status"]

                                # Update with fresh data
                                self.coordinator.data["devices"][i] = fresh_device
                                _LOGGER.debug(
                                    "Refreshed device data for %s with latest names",
                                    self._serial,
                                )
                                break
                        break

                # Notify all entities that the data has been updated
                self.coordinator.async_set_updated_data(self.coordinator.data)

                # Force immediate state refresh for all entities to pick up name changes
                _LOGGER.debug("Forcing immediate state refresh for all entities after name change")
                for entry in self.hass.data.get(DOMAIN, {}).values():
                    coordinator = entry.get("coordinator")
                    if coordinator:
                        coordinator.async_update_listeners()

        except Exception as err:
            _LOGGER.warning("Failed to refresh device names for %s: %s", self._serial, err)
            # Fallback to regular coordinator refresh
            await self.coordinator.async_request_refresh()
