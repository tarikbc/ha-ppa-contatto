"""Constants for PPA Contatto integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "ppa_contatto"

# API Configuration
AUTH_URL: Final = "https://auth.ppacontatto.com.br/login/password"
REFRESH_TOKEN_URL: Final = "https://auth.ppacontatto.com.br/token/renew"
API_BASE_URL: Final = "https://api.ppacontatto.com.br"
DEVICES_ENDPOINT: Final = f"{API_BASE_URL}/devices"
DEVICE_CONTROL_ENDPOINT: Final = f"{API_BASE_URL}/device/hardware"
DEVICE_REPORTS_ENDPOINT: Final = f"{API_BASE_URL}/device"
DEVICE_CONFIG_ENDPOINT: Final = f"{API_BASE_URL}/device/configuration"

# WebSocket Configuration
WEBSOCKET_URL: Final = "wss://realtime.ppacontatto.com.br/socket.io/"
WEBSOCKET_RECONNECT_DELAY: Final = 5  # seconds
WEBSOCKET_MAX_RETRIES: Final = 5  # Max retries per session before backing off
WEBSOCKET_BACKOFF_RESET_TIME: Final = 300  # Reset retry counter after 5 minutes of stability

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
UPDATE_INTERVAL: Final = 15  # Poll every 15 seconds to reduce API load

# Timeout settings
API_TIMEOUT: Final = 30  # 30 seconds timeout for API requests
CONNECTION_TIMEOUT: Final = 10  # 10 seconds timeout for connection
