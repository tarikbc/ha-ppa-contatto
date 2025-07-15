"""Number entities for PPA Contatto integration."""

from __future__ import annotations

import logging
from typing import Any, Optional

from homeassistant.components.number import (
    NumberEntity,
    NumberEntityDescription,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .config_entities import get_device_display_name
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

RELAY_DURATION_DESCRIPTION = NumberEntityDescription(
    key="relay_duration",
    name="Relay Duration",
    icon="mdi:timer",
    native_min_value=-1,
    native_max_value=30000,  # 30 seconds max
    native_step=100,
    native_unit_of_measurement=UnitOfTime.MILLISECONDS,
    mode=NumberMode.BOX,
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PPA Contatto number entities."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
    api = hass.data[DOMAIN][config_entry.entry_id]["api"]

    entities = []
    for device in coordinator.data.get("devices", []):
        serial = device.get("serial", "unknown")
        _LOGGER.info("Processing device %s for relay duration: %s", serial, device)

        # Add relay duration configuration for any PPA Contatto device
        # (all PPA devices have relay functionality)
        if serial and serial != "unknown":
            _LOGGER.info("Creating relay duration entity for device %s", serial)
            entities.append(
                PPAContattoRelayDurationNumber(
                    coordinator,
                    device,
                    RELAY_DURATION_DESCRIPTION,
                )
            )
        else:
            _LOGGER.warning(
                "Device %s missing serial number, skipping relay duration entity",
                device,
            )

    async_add_entities(entities)


class PPAContattoRelayDurationNumber(CoordinatorEntity, NumberEntity):
    """Relay duration number entity."""

    def __init__(
        self,
        coordinator,
        device: dict[str, Any],
        description: NumberEntityDescription,
    ) -> None:
        """Initialize the relay duration number entity."""
        super().__init__(coordinator)
        self.entity_description = description
        self.device = device

        self._attr_unique_id = f"{device['serial']}_{description.key}"
        self._attr_name = f"{device.get('name', device['serial'])} {description.name}"

        # Add to device configuration category
        self._attr_entity_category = "config"

        # Set device info with dynamic name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device["serial"])},
            name=get_device_display_name(device),
            manufacturer="PPA Contatto",
            model="Gate Controller",
            sw_version=device.get("version"),
            serial_number=device["serial"],
            configuration_url="https://play-lh.googleusercontent.com/qDtSOerKV_rVZ2ZMi_-pFe7jccoGVH0aHDbykUAQeE15_UoWa0Ej1dKt3FfaQCh1PoI=w480-h960-rw",
        )

        # Track current configuration
        self._current_config: Optional[dict] = None

    @property
    def native_value(self) -> Optional[float]:
        """Return the current relay duration value."""
        if self._current_config and "relayDuration" in self._current_config:
            return float(self._current_config["relayDuration"])

        # Default to 1000ms (1 second) for momentary behavior
        return 1000.0

    @property
    def name(self) -> str:
        """Return the current entity name from coordinator data."""
        # Get fresh device data from coordinator
        devices = self.coordinator.data.get("devices", [])
        device = None
        for d in devices:
            if d.get("serial") == self.device["serial"]:
                device = d
                break

        if not device:
            return self._attr_name  # Fallback to original name

        # Get device display name and combine with entity type
        device_name = get_device_display_name(device)
        return f"{device_name} {self.entity_description.name}"

    async def async_set_native_value(self, value: float) -> None:
        """Set the relay duration."""
        try:
            # Get current configuration first
            current_config = await self.coordinator.api.get_device_configuration(self.device["serial"])
            config_data = current_config.get("config", {})

            # Update relay duration
            config_data["relayDuration"] = int(value)

            # Update configuration
            await self.coordinator.api.update_device_configuration(self.device["serial"], config_data)

            # Store current config
            self._current_config = config_data

            _LOGGER.info(
                "Updated relay duration for %s to %d ms%s",
                self.device["serial"],
                int(value),
                " (on/off switch mode)" if value == -1 else " (momentary mode)",
            )

            # Update coordinator data
            await self.coordinator.async_request_refresh()

        except Exception as err:
            _LOGGER.error("Failed to set relay duration for %s: %s", self.device["serial"], err)
            raise

    async def async_update(self) -> None:
        """Update the current configuration."""
        try:
            config = await self.coordinator.api.get_device_configuration(self.device["serial"])
            self._current_config = config.get("config", {})
        except Exception as err:
            _LOGGER.debug("Failed to get configuration for %s: %s", self.device["serial"], err)
            self._current_config = {}

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        attrs = super().extra_state_attributes or {}

        value = self.native_value
        if value == -1:
            attrs["mode"] = "on_off_switch"
            attrs["behavior"] = "Toggle switch (stays on/off)"
        else:
            attrs["mode"] = "momentary"
            attrs["behavior"] = f"Momentary button ({int(value)}ms pulse)"

        return attrs
