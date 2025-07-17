"""PPA Contatto API client."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlencode, quote_plus

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    API_TIMEOUT,
    AUTH_HEADERS,
    AUTH_URL,
    CONNECTION_TIMEOUT,
    DEFAULT_HEADERS,
    DEVICE_CONFIG_ENDPOINT,
    DEVICE_CONTROL_ENDPOINT,
    DEVICE_REPORTS_ENDPOINT,
    DEVICES_ENDPOINT,
    REFRESH_TOKEN_URL,
    WEBSOCKET_BACKOFF_RESET_TIME,
    WEBSOCKET_MAX_RETRIES,
    WEBSOCKET_RECONNECT_DELAY,
    WEBSOCKET_URL,
)

_LOGGER = logging.getLogger(__name__)


class PPAContattoAuthError(Exception):
    """Exception for authentication errors."""


class PPAContattoAPIError(Exception):
    """Exception for API errors."""


class PPAContattoAPI:
    """API client for PPA Contatto."""

    def __init__(self, hass: HomeAssistant, email: str, password: str, config_entry=None) -> None:
        """Initialize the API client."""
        self.hass = hass
        self.email = email
        self.password = password
        self.session = async_get_clientsession(hass)
        self.config_entry = config_entry
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self._auth_lock = asyncio.Lock()

        # WebSocket related attributes
        self._websocket: Optional[aiohttp.ClientWebSocketResponse] = None
        self._websocket_session: Optional[aiohttp.ClientSession] = None
        self._websocket_connected = False
        self._websocket_namespace_connected = False
        self._websocket_reconnect_count = 0
        self._websocket_last_connect_time = 0
        self._websocket_last_retry_reset = time.time()
        self._device_update_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None
        self._websocket_task: Optional[asyncio.Task] = None
        self._websocket_listener_task: Optional[asyncio.Task] = None

        # Load stored tokens if available
        if config_entry and hasattr(config_entry, "data"):
            stored_data = config_entry.data
            self.access_token = stored_data.get("access_token")
            self.refresh_token = stored_data.get("refresh_token")
            if self.access_token:
                _LOGGER.debug("Loaded stored access token")
            if self.refresh_token:
                _LOGGER.debug("Loaded stored refresh token")

    async def authenticate(self) -> bool:
        """Authenticate with PPA Contatto API."""
        async with self._auth_lock:
            try:
                auth_data = {"email": self.email, "password": self.password}

                timeout = aiohttp.ClientTimeout(total=API_TIMEOUT, connect=CONNECTION_TIMEOUT)
                async with self.session.post(
                    AUTH_URL, headers=AUTH_HEADERS, data=json.dumps(auth_data), timeout=timeout
                ) as response:
                    if response.status != 200:
                        _LOGGER.error(
                            "Authentication failed with status %s: %s",
                            response.status,
                            await response.text(),
                        )
                        raise PPAContattoAuthError(f"Authentication failed: {response.status}")

                    data = await response.json()
                    self.access_token = data.get("accessToken")
                    self.refresh_token = data.get("refreshToken")

                    if not self.access_token:
                        raise PPAContattoAuthError("No access token received")

                    # Store tokens persistently
                    await self._store_tokens()

                    _LOGGER.debug("Authentication successful, tokens stored")
                    return True

            except aiohttp.ClientError as err:
                _LOGGER.error("Network error during authentication: %s", err)
                raise PPAContattoAuthError(f"Network error: {err}") from err
            except json.JSONDecodeError as err:
                _LOGGER.error("Invalid JSON response during authentication: %s", err)
                raise PPAContattoAuthError(f"Invalid response: {err}") from err

    async def _store_tokens(self) -> None:
        """Store tokens persistently in config entry."""
        if self.config_entry and self.access_token:
            new_data = dict(self.config_entry.data)
            new_data["access_token"] = self.access_token
            new_data["refresh_token"] = self.refresh_token

            self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
            _LOGGER.debug("Tokens stored in config entry")

    async def _clear_tokens(self) -> None:
        """Clear stored tokens."""
        self.access_token = None
        self.refresh_token = None

        if self.config_entry:
            new_data = dict(self.config_entry.data)
            new_data.pop("access_token", None)
            new_data.pop("refresh_token", None)

            self.hass.config_entries.async_update_entry(self.config_entry, data=new_data)
            _LOGGER.debug("Tokens cleared from config entry")

    async def _refresh_access_token(self) -> bool:
        """Refresh access token using refresh token."""
        if not self.refresh_token:
            _LOGGER.debug("No refresh token available, re-authenticating")
            return await self.authenticate()

        try:
            refresh_data = {"refreshToken": self.refresh_token}

            timeout = aiohttp.ClientTimeout(total=API_TIMEOUT, connect=CONNECTION_TIMEOUT)
            async with self.session.post(
                REFRESH_TOKEN_URL, headers=AUTH_HEADERS, data=json.dumps(refresh_data), timeout=timeout
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    self.access_token = data.get("accessToken")
                    # Update refresh token if provided (some APIs rotate refresh tokens)
                    if data.get("refreshToken"):
                        self.refresh_token = data.get("refreshToken")

                    if self.access_token:
                        await self._store_tokens()
                        _LOGGER.debug("Token refreshed successfully")
                        return True

                _LOGGER.warning("Token refresh failed with status %s, re-authenticating", response.status)
                error_text = await response.text()
                _LOGGER.debug("Refresh error response: %s", error_text)
                return await self.authenticate()

        except Exception as err:
            _LOGGER.warning("Token refresh error: %s, re-authenticating", err)
            return await self.authenticate()

    def _is_token_expired_error(self, status_code: int, error_text: str) -> bool:
        """Check if the error indicates an expired token."""
        if status_code in (401, 400):
            return True

        # Check for specific JWT expiration errors in 500 responses
        if status_code == 500:
            try:
                error_data = json.loads(error_text)
                error_name = error_data.get("name", "")
                error_message = error_data.get("message", "")

                # Check for JWT expired error patterns
                if error_name == "TokenExpiredError" or "jwt expired" in error_message.lower():
                    return True
            except (json.JSONDecodeError, AttributeError):
                # If we can't parse the error, check for common text patterns
                if "jwt expired" in error_text.lower() or "token expired" in error_text.lower():
                    return True

        return False

    def set_device_update_callback(self, callback: Callable[[str, Dict[str, Any]], None]) -> None:
        """Set callback function for device updates from WebSocket."""
        self._device_update_callback = callback

    async def start_websocket(self) -> bool:
        """Start WebSocket connection for real-time updates."""
        if not self.access_token:
            _LOGGER.warning("Cannot start WebSocket without access token")
            return False

        if self._websocket_connected:
            _LOGGER.debug("WebSocket already connected")
            return True

        try:
            # Build WebSocket URL (format proven to work by testing)
            params = {"auth": f"Bearer {self.access_token}", "EIO": "4", "transport": "websocket"}

            # Encode parameters (use + for space encoding like successful test)
            encoded_params = []
            for key, value in params.items():
                if key == "auth":
                    # Use + encoding for the Bearer token (like successful test)
                    encoded_value = value.replace(" ", "+")
                else:
                    encoded_value = value
                encoded_params.append(f"{key}={encoded_value}")

            websocket_url = f"{WEBSOCKET_URL}?{'&'.join(encoded_params)}"
            _LOGGER.debug("Connecting to WebSocket: %s", websocket_url.replace(self.access_token, "***TOKEN***"))

            # Create WebSocket session
            self._websocket_session = aiohttp.ClientSession()

            # Connect to WebSocket
            self._websocket = await self._websocket_session.ws_connect(websocket_url)

            _LOGGER.info("WebSocket connected to PPA Contatto - listening for device/status events")
            self._websocket_connected = True
            self._websocket_reconnect_count = 0
            self._websocket_last_connect_time = time.time()
            self._websocket_last_retry_reset = time.time()

            # Start message listener task
            self._websocket_listener_task = asyncio.create_task(self._websocket_message_listener())

            return True

        except Exception as err:
            _LOGGER.error("Failed to start WebSocket connection: %s", err)
            self._websocket_connected = False
            await self._cleanup_websocket()
            return False

    async def _websocket_message_listener(self) -> None:
        """Listen for WebSocket messages and parse Socket.IO protocol."""
        try:
            async for msg in self._websocket:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_websocket_message(msg.data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    _LOGGER.error("WebSocket error: %s", self._websocket.exception())
                    break
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                    _LOGGER.warning("WebSocket connection closed")
                    break
        except Exception as err:
            _LOGGER.error("Error in WebSocket message listener: %s", err)
        finally:
            self._websocket_connected = False
            self._websocket_namespace_connected = False

    async def _handle_websocket_message(self, message: str) -> None:
        """Handle and parse Socket.IO protocol messages."""
        try:
            # Socket.IO message format: TYPE[DATA]
            if message.startswith("0"):
                # Initial handshake message - respond with namespace connection request
                _LOGGER.debug("Received Socket.IO handshake: %s", message)
                if self._websocket and not self._websocket.closed:
                    await self._websocket.send_str("40")
                    _LOGGER.debug("Sent namespace connection request (40)")

            elif message.startswith("40"):
                # Namespace connection confirmed
                _LOGGER.info("Socket.IO namespace connected successfully")
                self._websocket_namespace_connected = True

            elif message.startswith("42"):
                # Event message with JSON data
                json_part = message[2:]  # Remove "42" prefix
                try:
                    data = json.loads(json_part)
                    if isinstance(data, list) and len(data) >= 2:
                        event_name = data[0]
                        event_data = data[1]

                        if event_name == "device/status":
                            await self._handle_device_status(event_data)
                        else:
                            _LOGGER.debug("Received unknown event: %s with data: %s", event_name, event_data)
                except json.JSONDecodeError as err:
                    _LOGGER.warning("Failed to parse Socket.IO event data: %s", err)

            # Send pong ("3") after every message EXCEPT the initial handshake (0)
            # Only send pong after namespace is connected
            if (
                not message.startswith("0")
                and self._websocket_namespace_connected
                and self._websocket
                and not self._websocket.closed
            ):
                await self._websocket.send_str("3")

        except Exception as err:
            _LOGGER.error("Error handling WebSocket message: %s", err)

    async def _handle_device_status(self, data: Dict[str, Any]) -> None:
        """Handle device/status events from WebSocket."""
        try:
            if self._device_update_callback and isinstance(data, dict):
                device_serial = data.get("serial")
                status = data.get("status", {})

                if device_serial and status:
                    # Transform the data to match our expected format
                    transformed_data = {
                        "gate": status.get("gate"),
                        "relay": status.get("relay"),
                        "serial": device_serial,
                    }
                    self._device_update_callback(device_serial, transformed_data)
        except Exception as err:
            _LOGGER.error("Error processing device/status event: %s", err)

    async def _cleanup_websocket(self) -> None:
        """Clean up WebSocket connection and resources."""
        self._websocket_connected = False
        self._websocket_namespace_connected = False

        if self._websocket_listener_task and not self._websocket_listener_task.done():
            self._websocket_listener_task.cancel()
            try:
                await self._websocket_listener_task
            except asyncio.CancelledError:
                pass

        if self._websocket and not self._websocket.closed:
            await self._websocket.close()

        if self._websocket_session and not self._websocket_session.closed:
            await self._websocket_session.close()

        self._websocket = None
        self._websocket_session = None
        self._websocket_listener_task = None

    async def stop_websocket(self) -> None:
        """Stop WebSocket connection."""
        try:
            await self._cleanup_websocket()
            _LOGGER.debug("WebSocket disconnected and cleaned up")
        except Exception as err:
            _LOGGER.error("Error stopping WebSocket: %s", err)

    async def ensure_websocket_connected(self) -> bool:
        """Ensure WebSocket connection is active, reconnect if needed.

        Uses a resilient reconnection strategy:
        - First 5 attempts use standard 5-second delays
        - After that, exponential backoff up to 5-minute delays
        - Never completely gives up trying to reconnect
        - Resets retry counter after 5 minutes of stable connection
        """
        if self._websocket_connected and self._websocket and not self._websocket.closed:
            return True

        # Reset retry counter if enough time has passed since last reset
        current_time = time.time()
        if current_time - self._websocket_last_retry_reset > WEBSOCKET_BACKOFF_RESET_TIME:
            if self._websocket_reconnect_count > 0:
                _LOGGER.info(
                    "Resetting WebSocket retry counter after %d seconds of stability", WEBSOCKET_BACKOFF_RESET_TIME
                )
            self._websocket_reconnect_count = 0
            self._websocket_last_retry_reset = current_time

        # Calculate delay with exponential backoff
        if self._websocket_reconnect_count < WEBSOCKET_MAX_RETRIES:
            delay = WEBSOCKET_RECONNECT_DELAY
        else:
            # After max retries, use exponential backoff but never give up completely
            backoff_multiplier = min(2 ** (self._websocket_reconnect_count - WEBSOCKET_MAX_RETRIES), 60)
            delay = WEBSOCKET_RECONNECT_DELAY * backoff_multiplier
            _LOGGER.warning(
                "WebSocket reconnection attempt %d (using %ds backoff delay)",
                self._websocket_reconnect_count + 1,
                delay,
            )

        self._websocket_reconnect_count += 1
        _LOGGER.info("Attempting WebSocket reconnection (%d) with %ds delay", self._websocket_reconnect_count, delay)

        # Try to refresh token first
        if not self.access_token or not await self._refresh_access_token():
            _LOGGER.error("Cannot reconnect WebSocket without valid access token")
            return False

        # Wait before reconnecting
        await asyncio.sleep(delay)

        return await self.start_websocket()

    async def _make_authenticated_request(self, method: str, url: str, **kwargs) -> Dict[str, Any]:
        """Make an authenticated request to the API."""
        if not self.access_token:
            await self.authenticate()

        headers = DEFAULT_HEADERS.copy()
        headers["Authorization"] = f"Bearer {self.access_token}"

        try:
            timeout = aiohttp.ClientTimeout(total=API_TIMEOUT, connect=CONNECTION_TIMEOUT)
            async with self.session.request(method, url, headers=headers, timeout=timeout, **kwargs) as response:
                if response.status != 200:
                    error_text = await response.text()

                    # Check if this is a token expiration error (401, 400, or 500 with JWT expired)
                    if self._is_token_expired_error(response.status, error_text):
                        _LOGGER.warning("Token expired error (status %s): %s", response.status, error_text)

                        # Try to refresh/re-authenticate
                        if await self._refresh_access_token():
                            headers["Authorization"] = f"Bearer {self.access_token}"

                            async with self.session.request(
                                method, url, headers=headers, timeout=timeout, **kwargs
                            ) as retry_response:
                                if retry_response.status == 200:
                                    return await retry_response.json()

                                error_text = await retry_response.text()
                                _LOGGER.error(
                                    "Retry failed (status %s): %s",
                                    retry_response.status,
                                    error_text,
                                )
                                raise PPAContattoAPIError(
                                    f"API request failed after auth retry: {retry_response.status} - {error_text}"
                                )
                        else:
                            raise PPAContattoAPIError("Authentication failed during retry")
                    else:
                        # Not a token error, raise the original error
                        _LOGGER.error("API error (status %s): %s", response.status, error_text)
                        raise PPAContattoAPIError(f"API request failed: {response.status} - {error_text}")

                return await response.json()

        except asyncio.TimeoutError as err:
            _LOGGER.error("API request timeout: %s", err)
            raise PPAContattoAPIError(f"API timeout: {err}") from err
        except aiohttp.ClientConnectorError as err:
            _LOGGER.error("Cannot connect to API server: %s", err)
            raise PPAContattoAPIError(f"Connection error: {err}") from err
        except aiohttp.ClientError as err:
            _LOGGER.error("Network error during API request: %s", err)
            raise PPAContattoAPIError(f"Network error: {err}") from err

    async def _make_authenticated_request_text(self, method: str, url: str, **kwargs) -> str:
        """Make an authenticated request that returns text (not JSON)."""
        if not self.access_token:
            await self.authenticate()

        headers = DEFAULT_HEADERS.copy()
        headers["Authorization"] = f"Bearer {self.access_token}"

        try:
            timeout = aiohttp.ClientTimeout(total=API_TIMEOUT, connect=CONNECTION_TIMEOUT)
            async with self.session.request(method, url, headers=headers, timeout=timeout, **kwargs) as response:
                if response.status != 200:
                    error_text = await response.text()

                    # Check if this is a token expiration error (401, 400, or 500 with JWT expired)
                    if self._is_token_expired_error(response.status, error_text):
                        _LOGGER.warning("Token expired error (status %s): %s", response.status, error_text)

                        # Try to refresh/re-authenticate
                        if await self._refresh_access_token():
                            headers["Authorization"] = f"Bearer {self.access_token}"

                            async with self.session.request(
                                method, url, headers=headers, timeout=timeout, **kwargs
                            ) as retry_response:
                                if retry_response.status == 200:
                                    return await retry_response.text()

                                error_text = await retry_response.text()
                                _LOGGER.error(
                                    "Retry failed (status %s): %s",
                                    retry_response.status,
                                    error_text,
                                )
                                raise PPAContattoAPIError(
                                    f"API request failed after auth retry: {retry_response.status} - {error_text}"
                                )
                        else:
                            raise PPAContattoAPIError("Authentication failed during retry")
                    else:
                        # Not a token error, raise the original error
                        _LOGGER.error("API error (status %s): %s", response.status, error_text)
                        raise PPAContattoAPIError(f"API request failed: {response.status} - {error_text}")

                return await response.text()

        except aiohttp.ClientError as err:
            _LOGGER.error("Network error during API request: %s", err)
            raise PPAContattoAPIError(f"Network error: {err}") from err

    async def get_devices(self) -> List[Dict[str, Any]]:
        """Get all devices from the API."""
        try:
            data = await self._make_authenticated_request("GET", DEVICES_ENDPOINT)
            _LOGGER.debug("Retrieved %d devices", len(data))
            return data
        except Exception as err:
            _LOGGER.error("Failed to get devices: %s", err)
            raise

    async def control_device(self, serial: str, device_type: str) -> bool:
        """Control a device (gate or relay)."""
        try:
            url = f"{DEVICE_CONTROL_ENDPOINT}/{serial}"

            # Prepare the JSON payload to specify which hardware to control
            payload = {"hardware": device_type}

            # Use text response version since this endpoint returns text/plain
            await self._make_authenticated_request_text("POST", url, data=json.dumps(payload))
            _LOGGER.debug("Successfully controlled device %s (%s)", serial, device_type)
            return True
        except Exception as err:
            _LOGGER.error("Failed to control device %s: %s", serial, err)
            raise

    async def get_device_reports(self, serial: str, page: int = 0, total: int = 10) -> List[Dict[str, Any]]:
        """Get device reports/history."""
        try:
            url = f"{DEVICE_REPORTS_ENDPOINT}/{serial}/reports"
            params = {"page": page, "total": total}
            data = await self._make_authenticated_request("GET", url, params=params)
            _LOGGER.debug("Retrieved %d reports for device %s", len(data), serial)
            return data
        except Exception as err:
            _LOGGER.error("Failed to get reports for device %s: %s", serial, err)
            raise

    async def get_latest_device_status(self, serial: str) -> Dict[str, Any]:
        """Get the latest status from device reports."""
        try:
            reports = await self.get_device_reports(serial, page=0, total=5)

            # Parse latest status from reports
            latest_status = {
                "gate": None,
                "relay": None,
                "last_action": None,
                "last_user": None,
            }

            for report in reports:
                target = report.get("target", "")
                created_at = report.get("createdAt")
                user_name = report.get("name")

                if "gate:" in target:
                    if latest_status["gate"] is None:
                        latest_status["gate"] = target.split("gate: ")[1]
                        if latest_status["last_action"] is None:
                            latest_status["last_action"] = created_at
                            latest_status["last_user"] = user_name

                elif "relay:" in target:
                    if latest_status["relay"] is None:
                        latest_status["relay"] = target.split("relay: ")[1]
                        if latest_status["last_action"] is None:
                            latest_status["last_action"] = created_at
                            latest_status["last_user"] = user_name

                # Stop if we have both statuses
                if latest_status["gate"] is not None and latest_status["relay"] is not None:
                    break

            return latest_status

        except Exception as err:
            _LOGGER.debug(
                "Failed to get latest status for %s, falling back to basic status: %s",
                serial,
                err,
            )
            return {"gate": None, "relay": None, "last_action": None, "last_user": None}

    async def get_device_configuration(self, serial: str) -> Dict[str, Any]:
        """Get device configuration."""
        try:
            url = f"{DEVICE_CONFIG_ENDPOINT}/{serial}"
            data = await self._make_authenticated_request("GET", url)
            _LOGGER.debug("Retrieved configuration for device %s", serial)
            return data
        except Exception as err:
            _LOGGER.error("Failed to get configuration for device %s: %s", serial, err)
            raise

    async def update_device_configuration(self, serial: str, config: Dict[str, Any]) -> bool:
        """Update device configuration via POST request."""
        try:
            url = f"{DEVICE_CONFIG_ENDPOINT}/{serial}"
            payload = {"config": config}
            await self._make_authenticated_request("POST", url, data=json.dumps(payload))
            _LOGGER.debug("Successfully updated configuration for device %s: %s", serial, config)
            return True
        except Exception as err:
            _LOGGER.error("Failed to update configuration for device %s: %s", serial, err)
            raise

    async def update_device_settings(self, serial: str, settings: Dict[str, Any]) -> bool:
        """Update device settings via PATCH request (legacy method for basic settings)."""
        try:
            url = f"{DEVICE_REPORTS_ENDPOINT}/{serial}"
            await self._make_authenticated_request("PATCH", url, data=json.dumps(settings))
            _LOGGER.debug("Successfully updated settings for device %s: %s", serial, settings)
            return True
        except Exception as err:
            _LOGGER.error("Failed to update settings for device %s: %s", serial, err)
            raise

    async def test_connection(self) -> bool:
        """Test the connection to the API."""
        try:
            _LOGGER.debug("Testing API connection...")

            # Clear any stale tokens
            await self._clear_tokens()

            # Authenticate fresh
            if not await self.authenticate():
                _LOGGER.error("Authentication failed during connection test")
                return False

            # Test device listing
            devices = await self.get_devices()
            if not isinstance(devices, list):
                _LOGGER.error("Invalid devices response: %s", type(devices))
                return False

            _LOGGER.debug("Connection test successful, found %d devices", len(devices))
            return True

        except PPAContattoAuthError as err:
            _LOGGER.error("Authentication error during connection test: %s", err)
            return False
        except PPAContattoAPIError as err:
            _LOGGER.error("API error during connection test: %s", err)
            return False
        except Exception as err:
            _LOGGER.error("Unexpected error during connection test: %s", err)
            return False
