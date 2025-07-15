"""Support for PPA Contatto configuration switches."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .config_entities import async_setup_config_switches


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PPA Contatto switch platform."""
    await async_setup_config_switches(hass, config_entry, async_add_entities)
