"""Support for PPA Contatto sensors."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import PPAContattoAPI
from .const import DEVICE_TYPE_GATE, DEVICE_TYPE_RELAY, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the PPA Contatto sensor platform."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
    api = hass.data[DOMAIN][config_entry.entry_id]["api"]

    entities = []

    # Create sensors for each device
    for device in coordinator.data.get("devices", []):
        serial = device.get("serial")
        if not serial:
            continue

        # Add last action sensor
        entities.append(PPAContattoLastActionSensor(coordinator, api, device, f"{serial}_last_action", "Last Action"))

        # Add last user sensor
        entities.append(PPAContattoLastUserSensor(coordinator, api, device, f"{serial}_last_user", "Last User"))

        # Add gate status sensor if gate is shown
        if device.get("name", {}).get("gate", {}).get("show", False):
            entities.append(
                PPAContattoStatusSensor(
                    coordinator,
                    api,
                    device,
                    DEVICE_TYPE_GATE,
                    f"{serial}_gate_status",
                    "Gate Status",
                )
            )

        # Add relay status sensor if relay is shown
        if device.get("name", {}).get("relay", {}).get("show", False):
            entities.append(
                PPAContattoStatusSensor(
                    coordinator,
                    api,
                    device,
                    DEVICE_TYPE_RELAY,
                    f"{serial}_relay_status",
                    "Relay Status",
                )
            )

    async_add_entities(entities)


class PPAContattoBaseSensor(CoordinatorEntity, SensorEntity):
    """Base class for PPA Contatto sensors."""

    def __init__(
        self,
        coordinator,
        api: PPAContattoAPI,
        device: Dict[str, Any],
        unique_id: str,
        name: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._api = api
        self._device = device
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

    def _get_device_data(self) -> Optional[Dict[str, Any]]:
        """Get current device data from coordinator."""
        devices = self.coordinator.data.get("devices", [])
        for device in devices:
            if device.get("serial") == self._serial:
                return device
        return None


class PPAContattoLastActionSensor(PPAContattoBaseSensor):
    """Sensor for the last action timestamp."""

    def __init__(self, coordinator, api, device, unique_id, name):
        """Initialize the last action sensor."""
        super().__init__(coordinator, api, device, unique_id, name)
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_icon = "mdi:clock-outline"

    @property
    def native_value(self) -> Optional[datetime]:
        """Return the last action timestamp."""
        device = self._get_device_data()
        if not device:
            return None

        latest_status = device.get("latest_status", {})
        last_action = latest_status.get("last_action")

        if last_action:
            try:
                # Parse ISO timestamp
                return dt_util.parse_datetime(last_action)
            except (ValueError, TypeError):
                _LOGGER.warning("Could not parse timestamp: %s", last_action)

        return None

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return additional state attributes."""
        device = self._get_device_data()
        if not device:
            return {}

        latest_status = device.get("latest_status", {})
        return {
            "last_user": latest_status.get("last_user"),
            "device_serial": self._serial,
        }


class PPAContattoLastUserSensor(PPAContattoBaseSensor):
    """Sensor for the last user who triggered an action."""

    def __init__(self, coordinator, api, device, unique_id, name):
        """Initialize the last user sensor."""
        super().__init__(coordinator, api, device, unique_id, name)
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_icon = "mdi:account"

    @property
    def native_value(self) -> Optional[str]:
        """Return the last user name."""
        device = self._get_device_data()
        if not device:
            return None

        latest_status = device.get("latest_status", {})
        last_user = latest_status.get("last_user")

        return last_user if last_user else "System"

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return additional state attributes."""
        device = self._get_device_data()
        if not device:
            return {}

        latest_status = device.get("latest_status", {})
        attrs = {
            "device_serial": self._serial,
        }

        if latest_status.get("last_action"):
            attrs["last_action"] = latest_status["last_action"]

        return attrs


class PPAContattoStatusSensor(PPAContattoBaseSensor):
    """Sensor for gate/relay status."""

    def __init__(self, coordinator, api, device, device_type, unique_id, name):
        """Initialize the status sensor."""
        super().__init__(coordinator, api, device, unique_id, name)
        self._device_type = device_type
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

        if device_type == DEVICE_TYPE_GATE:
            self._attr_icon = "mdi:gate"
        else:
            self._attr_icon = "mdi:electric-switch"

    @property
    def native_value(self) -> Optional[str]:
        """Return the current status."""
        device = self._get_device_data()
        if not device:
            return None

        # Try latest status first, then fallback to device status
        latest_status = device.get("latest_status", {})

        if self._device_type == DEVICE_TYPE_GATE:
            status = latest_status.get("gate")
            if status is None:
                status = device.get("status", {}).get("gate")
            return status
        else:  # relay
            status = latest_status.get("relay")
            if status is None:
                status = device.get("status", {}).get("relay")
            return status

    @property
    def extra_state_attributes(self) -> Dict[str, Any]:
        """Return additional state attributes."""
        device = self._get_device_data()
        if not device:
            return {}

        attrs = {
            "device_serial": self._serial,
            "device_type": self._device_type,
        }

        # Add both latest and device status for comparison
        latest_status = device.get("latest_status", {})
        device_status = device.get("status", {})

        if self._device_type == DEVICE_TYPE_GATE:
            attrs["device_status"] = device_status.get("gate")
            attrs["latest_status"] = latest_status.get("gate")
        else:
            attrs["device_status"] = device_status.get("relay")
            attrs["latest_status"] = latest_status.get("relay")

        if latest_status.get("last_action"):
            attrs["last_updated"] = latest_status["last_action"]
        if latest_status.get("last_user"):
            attrs["last_user"] = latest_status["last_user"]

        return attrs
