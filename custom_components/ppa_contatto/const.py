"""Constants for PPA Contatto integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "ppa_contatto"

# API Configuration
AUTH_URL: Final = "https://auth.ppacontatto.com.br/login/password"
API_BASE_URL: Final = "https://api.ppacontatto.com.br"
DEVICES_ENDPOINT: Final = f"{API_BASE_URL}/devices"
DEVICE_CONTROL_ENDPOINT: Final = f"{API_BASE_URL}/device/hardware"
DEVICE_REPORTS_ENDPOINT: Final = f"{API_BASE_URL}/device"
DEVICE_CONFIG_ENDPOINT: Final = f"{API_BASE_URL}/device/configuration"

# Config Keys
CONF_EMAIL: Final = "email"
CONF_PASSWORD: Final = "password"

# Device Types
DEVICE_TYPE_GATE: Final = "gate"
DEVICE_TYPE_RELAY: Final = "relay"

# Headers
DEFAULT_HEADERS: Final = {
    "Host": "api.ppacontatto.com.br",
    "Connection": "keep-alive",
    "Accept": "*/*",
    "User-Agent": "Contatto/1 CFNetwork/3826.500.131 Darwin/24.5.0",
    "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
    "Content-Type": "application/json",
}

AUTH_HEADERS: Final = {
    "Host": "auth.ppacontatto.com.br",
    "Connection": "keep-alive",
    "Accept": "*/*",
    "User-Agent": "Contatto/1 CFNetwork/3826.500.131 Darwin/24.5.0",
    "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
    "Content-Type": "application/json",
}

# Update intervals
UPDATE_INTERVAL: Final = 30  # seconds
