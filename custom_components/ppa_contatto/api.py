"""PPA Contatto API client."""

from __future__ import annotations

import asyncio
import json
import logging
import random
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
    EVENT_REPLAY_LIMIT,
    EVENT_REPLAY_RECENCY_S,
    REFRESH_TOKEN_URL,
    WEBSOCKET_AUTH_FAIL_THRESHOLD,
    WEBSOCKET_CLIENT_PING_INTERVAL,
    WEBSOCKET_PONG_DEADLINE,
    WEBSOCKET_PROACTIVE_RECYCLE,
    WEBSOCKET_RECONNECT_BASE,
    WEBSOCKET_RECONNECT_JITTER,
    WEBSOCKET_RECONNECT_MAX,
    WEBSOCKET_STALE_TIMEOUT,
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
        # Number of consecutive WS handshake failures since the last successful first-frame.
        # Used to gate a forced re-authentication; we DO NOT refresh the token on every
        # reconnect attempt because that piles auth-endpoint load on top of WS load.
        self._consecutive_handshake_fails = 0
        self._websocket_last_connect_time: float = 0.0  # epoch seconds when last connect succeeded
        self._device_update_callback: Optional[Callable[..., None]] = None
        self._on_websocket_connected_callback: Optional[Callable[[], Any]] = None
        self._websocket_task: Optional[asyncio.Task] = None
        self._websocket_listener_task: Optional[asyncio.Task] = None
        self._websocket_keepalive_task: Optional[asyncio.Task] = None
        # Monotonic timestamp of the last frame received from the server.
        # Used by the coordinator-level watchdog to detect zombie WebSockets
        # (TCP silently dead so aiohttp never fires a close event).
        self._websocket_last_message_at: float = 0.0
        # Whether ANY frame has arrived since the current connect. The reconnect
        # counter only resets to zero on first frame — that's the only honest
        # "this connection is real" signal; bare TCP+TLS handshake isn't enough.
        self._first_frame_received: bool = False
        # Active ping-pong tracking. After we send "2" we record the timestamp;
        # any inbound frame (server pong "3" or anything else) clears it. If we've
        # been waiting for a pong for longer than WEBSOCKET_PONG_DEADLINE, the
        # watchdog declares the socket dead even if TCP says otherwise.
        self._waiting_pong_since: float = 0.0
        # Last close diagnostics, for log + retry policy decisions.
        self._last_close_code: Optional[int] = None
        self._last_close_reason: Optional[str] = None

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

    def set_device_update_callback(self, callback: Callable[..., None]) -> None:
        """Set callback function for device updates from WebSocket / event replay.

        Callback signature: ``callback(serial: str, update: dict, *, source: str = 'ws',
        report: dict | None = None)``. Older code used a 2-arg signature; we still call
        it positionally so v1.5.x callbacks remain compatible.
        """
        self._device_update_callback = callback

    def set_on_websocket_connected_callback(self, callback: Callable[[], Any]) -> None:
        """Register a callback invoked once each time the WS namespace finishes connecting.

        Used by the coordinator to fire an immediate catch-up poll, so we replay any
        events the cloud delivered while we were disconnected.
        """
        self._on_websocket_connected_callback = callback

    async def start_websocket(self) -> bool:
        """Start WebSocket connection for real-time updates.

        Returns True if the TCP+TLS+WS handshake succeeded. The reconnect counter is
        NOT reset here — only on first frame received post-connect (see listener).
        """
        if not self.access_token:
            _LOGGER.warning("Cannot start WebSocket without access token")
            self._consecutive_handshake_fails += 1
            return False

        if self._websocket_connected:
            _LOGGER.debug("WebSocket already connected")
            return True

        try:
            # Build WebSocket URL (format proven to work by testing)
            params = {"auth": f"Bearer {self.access_token}", "EIO": "4", "transport": "websocket"}

            encoded_params = []
            for key, value in params.items():
                # Use + encoding for the Bearer token (like successful test)
                encoded_value = value.replace(" ", "+") if key == "auth" else value
                encoded_params.append(f"{key}={encoded_value}")

            websocket_url = f"{WEBSOCKET_URL}?{'&'.join(encoded_params)}"
            _LOGGER.debug("Connecting to WebSocket: %s", websocket_url.replace(self.access_token, "***TOKEN***"))

            # Create WebSocket session
            self._websocket_session = aiohttp.ClientSession()

            # Connect to WebSocket
            self._websocket = await self._websocket_session.ws_connect(websocket_url)

            _LOGGER.info("WebSocket TCP/TLS handshake OK — awaiting first frame to confirm liveness")
            self._websocket_connected = True
            self._websocket_last_connect_time = time.time()
            self._websocket_last_message_at = time.monotonic()
            self._first_frame_received = False
            self._waiting_pong_since = 0.0
            self._last_close_code = None
            self._last_close_reason = None

            # Start listener + keepalive
            self._websocket_listener_task = asyncio.create_task(self._websocket_message_listener())
            self._websocket_keepalive_task = asyncio.create_task(self._websocket_client_keepalive())

            return True

        except Exception as err:
            self._consecutive_handshake_fails += 1
            _LOGGER.error(
                "Failed to start WebSocket connection (handshake fails=%d): %s",
                self._consecutive_handshake_fails,
                err,
            )
            self._websocket_connected = False
            await self._cleanup_websocket()
            return False

    async def _websocket_message_listener(self) -> None:
        """Listen for WebSocket messages and parse Socket.IO protocol.

        Resets the reconnect counter on FIRST frame received (the only honest "this
        connection is real" signal). Captures close code/reason on disconnect for
        diagnostics and retry-policy decisions.
        """
        try:
            async for msg in self._websocket:
                # Any frame proves the TCP connection is alive.
                self._websocket_last_message_at = time.monotonic()
                # Any frame also clears any pending pong wait.
                self._waiting_pong_since = 0.0

                # First frame post-connect = success. Reset reconnect counters.
                if not self._first_frame_received:
                    self._first_frame_received = True
                    if self._websocket_reconnect_count > 0 or self._consecutive_handshake_fails > 0:
                        _LOGGER.info(
                            "WebSocket fully alive (first frame received after %d reconnect(s))",
                            self._websocket_reconnect_count,
                        )
                    self._websocket_reconnect_count = 0
                    self._consecutive_handshake_fails = 0

                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle_websocket_message(msg.data)
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    _LOGGER.error("WebSocket transport error: %s", self._websocket.exception())
                    break
                elif msg.type == aiohttp.WSMsgType.CLOSE:
                    # Server-initiated close frame. msg.data carries the close code,
                    # msg.extra the reason. This is the most diagnostic disconnect path.
                    self._last_close_code = msg.data if isinstance(msg.data, int) else None
                    self._last_close_reason = msg.extra if isinstance(msg.extra, str) else None
                    _LOGGER.warning(
                        "WebSocket closed by server (code=%s, reason=%r)",
                        self._last_close_code,
                        self._last_close_reason,
                    )
                    break
                elif msg.type in (aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                    # Closed for non-server-frame reason (network reset, our own close, etc.)
                    self._last_close_code = self._websocket.close_code if self._websocket is not None else None
                    _LOGGER.warning(
                        "WebSocket closed (type=%s, close_code=%s)",
                        msg.type.name,
                        self._last_close_code,
                    )
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
                # Tell the coordinator a connect just completed — it will fire an
                # immediate catch-up poll to replay any events the cloud delivered
                # while we were disconnected.
                if self._on_websocket_connected_callback is not None:
                    try:
                        result = self._on_websocket_connected_callback()
                        if asyncio.iscoroutine(result):
                            asyncio.create_task(result)
                    except Exception as err:  # noqa: BLE001
                        _LOGGER.debug("on_websocket_connected callback raised: %s", err)

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

    async def _websocket_client_keepalive(self) -> None:
        """Proactive ping + proactive recycle.

        Three jobs:
          1. Send a Socket.IO "2" ping every WEBSOCKET_CLIENT_PING_INTERVAL seconds. Acts
             as both a NAT keepalive and an active liveness probe (send() on a dead socket
             raises immediately).
          2. Track whether the corresponding "3" pong arrives within WEBSOCKET_PONG_DEADLINE.
             If not, the socket is "TCP alive but server hung" — declare dead.
          3. Proactively recycle the connection every WEBSOCKET_PROACTIVE_RECYCLE seconds.
             Some clouds evict long-lived connections deterministically; we'd rather take
             the brief gap on our schedule (seconds, with immediate replay catch-up) than
             have it land in the middle of a gate event.
        """
        try:
            while self._websocket_connected and self._websocket and not self._websocket.closed:
                await asyncio.sleep(WEBSOCKET_CLIENT_PING_INTERVAL)
                if not (self._websocket and not self._websocket.closed):
                    break

                # Pong-deadline check. If we already had a ping in flight and no inbound
                # frame cleared it, the listener will have updated _waiting_pong_since.
                # If that's been more than PONG_DEADLINE, declare dead.
                if (
                    self._waiting_pong_since > 0
                    and (time.monotonic() - self._waiting_pong_since) > WEBSOCKET_PONG_DEADLINE
                ):
                    _LOGGER.warning(
                        "WebSocket pong not received within %.1fs — declaring socket dead",
                        WEBSOCKET_PONG_DEADLINE,
                    )
                    self._websocket_connected = False
                    break

                # Proactive recycle.
                connection_age = time.time() - self._websocket_last_connect_time
                if connection_age > WEBSOCKET_PROACTIVE_RECYCLE:
                    _LOGGER.info(
                        "WebSocket reached %.0fs uptime — proactively recycling to preempt " "server-side eviction",
                        connection_age,
                    )
                    self._websocket_connected = False
                    break

                # Send the ping.
                try:
                    if self._websocket_namespace_connected:
                        await self._websocket.send_str("2")
                        if self._waiting_pong_since == 0:
                            self._waiting_pong_since = time.monotonic()
                except Exception as err:  # noqa: BLE001
                    _LOGGER.warning("WebSocket client ping failed (socket is likely dead): %s", err)
                    self._websocket_connected = False
                    break
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("WebSocket keepalive task exited unexpectedly: %s", err)

    def websocket_is_stale(self) -> bool:
        """Return True if no frames have arrived within WEBSOCKET_STALE_TIMEOUT.

        Called by the coordinator watchdog. If this returns True while the
        API still thinks it's connected, we have a zombie WebSocket and
        need to force a reconnect.
        """
        if not self._websocket_connected:
            return False
        if self._websocket_last_message_at == 0.0:
            return False
        return (time.monotonic() - self._websocket_last_message_at) > WEBSOCKET_STALE_TIMEOUT

    async def force_websocket_reconnect(self, reason: str) -> bool:
        """Tear down any existing WebSocket and reconnect from scratch."""
        _LOGGER.warning("Forcing WebSocket reconnect: %s", reason)
        await self._cleanup_websocket()
        return await self.start_websocket()

    async def _cleanup_websocket(self) -> None:
        """Clean up WebSocket connection and resources."""
        self._websocket_connected = False
        self._websocket_namespace_connected = False

        if self._websocket_keepalive_task and not self._websocket_keepalive_task.done():
            self._websocket_keepalive_task.cancel()
            try:
                await self._websocket_keepalive_task
            except asyncio.CancelledError:
                pass

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
        self._websocket_keepalive_task = None
        self._websocket_last_message_at = 0.0

    async def stop_websocket(self) -> None:
        """Stop WebSocket connection."""
        try:
            await self._cleanup_websocket()
            _LOGGER.debug("WebSocket disconnected and cleaned up")
        except Exception as err:
            _LOGGER.error("Error stopping WebSocket: %s", err)

    def _next_reconnect_delay(self) -> float:
        """Compute the next reconnect delay using jittered exponential backoff.

        delay = min(BASE * 2^count, MAX) * (1 ± JITTER)

        Counter advance happens here so the delay reflects the *upcoming* attempt.
        """
        n = self._websocket_reconnect_count
        base = min(WEBSOCKET_RECONNECT_BASE * (2**n), WEBSOCKET_RECONNECT_MAX)
        jitter = 1.0 + (random.random() * 2 - 1) * WEBSOCKET_RECONNECT_JITTER
        return max(0.0, base * jitter)

    def _close_code_indicates_auth_problem(self) -> bool:
        """True if the last close suggests we should re-authenticate before retrying.

        Heuristics: explicit auth-related WebSocket close codes (1008 = policy violation,
        4001/4003 = vendor-specific auth in many Socket.IO deployments) or a reason
        string mentioning auth/token.
        """
        if self._last_close_code in (1008, 4001, 4002, 4003):
            return True
        if self._last_close_reason and re.search(
            r"auth|token|unauthor|forbid|jwt", self._last_close_reason, re.IGNORECASE
        ):
            return True
        return False

    async def ensure_websocket_connected(self) -> bool:
        """Ensure the WebSocket is connected, reconnecting with policy if needed.

        Strategy (v1.6.0):
          - Jittered exponential backoff (no flat first-N attempts).
          - Token refresh ONLY on confirmed auth-related close OR after threshold of
            consecutive handshake failures — *not* on every retry. Avoids piling
            auth-endpoint load on top of WS load when the cloud is unhealthy.
          - Reconnect counter is reset on first frame received post-connect (in the
            listener), not here.
        """
        if self._websocket_connected and self._websocket and not self._websocket.closed:
            return True

        delay = self._next_reconnect_delay()
        self._websocket_reconnect_count += 1
        _LOGGER.info(
            "WebSocket reconnect attempt %d in %.1fs (last close code=%s reason=%r)",
            self._websocket_reconnect_count,
            delay,
            self._last_close_code,
            self._last_close_reason,
        )

        await asyncio.sleep(delay)

        # Refresh policy: only if no token at all, or last close was auth-related, or
        # we've failed handshakes too many times in a row.
        need_refresh = (
            not self.access_token
            or self._close_code_indicates_auth_problem()
            or self._consecutive_handshake_fails >= WEBSOCKET_AUTH_FAIL_THRESHOLD
        )
        if need_refresh:
            _LOGGER.info(
                "Refreshing token before WS reconnect (no_token=%s auth_close=%s handshake_fails=%d)",
                not self.access_token,
                self._close_code_indicates_auth_problem(),
                self._consecutive_handshake_fails,
            )
            if not await self._refresh_access_token():
                _LOGGER.error("Cannot reconnect WebSocket — token refresh failed")
                return False
            # Reset the handshake-fail counter so we don't refresh on every attempt.
            self._consecutive_handshake_fails = 0

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

    @staticmethod
    def _parse_report_target(target: str) -> Optional[tuple]:
        """Parse a report's ``target`` field into (kind, value) or None if not a state event."""
        if not target or ": " not in target:
            return None
        kind, _, value = target.partition(": ")
        kind, value = kind.strip(), value.strip()
        if kind not in ("gate", "relay"):
            return None
        return kind, value

    async def fetch_device_events_since(
        self,
        serial: str,
        last_event_id: int,
        *,
        limit: int = EVENT_REPLAY_LIMIT,
        recency_seconds: int = EVENT_REPLAY_RECENCY_S,
    ) -> Dict[str, Any]:
        """Pull recent reports and return new state-change events plus latest status.

        The PPA cloud's ``/device/{serial}/reports`` endpoint is the canonical event log.
        We pull the most recent N reports (newest first), filter to those with id strictly
        greater than ``last_event_id`` AND within ``recency_seconds`` of now, and return
        them sorted **chronologically** so the caller can replay them through the state
        handler in order.

        Returns:
            {
              "events": [ {"id", "kind", "value", "created_at", "user"} ],   # chronological
              "latest_status": {"gate", "relay", "last_action", "last_user"},
              "newest_id": int,    # max(report.id) seen, or unchanged last_event_id
            }
        """
        empty: Dict[str, Any] = {
            "events": [],
            "latest_status": {"gate": None, "relay": None, "last_action": None, "last_user": None},
            "newest_id": last_event_id,
        }

        try:
            reports = await self.get_device_reports(serial, page=0, total=limit)
        except Exception as err:
            _LOGGER.debug("Failed to fetch reports for %s: %s", serial, err)
            return empty

        if not reports:
            return empty

        now = time.time()
        recency_cutoff_iso = None  # filter out events older than recency_seconds
        if recency_seconds > 0:
            recency_cutoff_iso = now - recency_seconds

        # Latest-status derivation walks newest-first (the API's natural order).
        latest_status = {"gate": None, "relay": None, "last_action": None, "last_user": None}

        # Replayable events: filter to "newer than last_event_id" + recency.
        # Reports are returned newest-first; reverse to chronological order so callers
        # apply state changes in the order they happened.
        newest_id = last_event_id
        replayable: List[Dict[str, Any]] = []
        for r in reports:
            rid = r.get("id")
            if not isinstance(rid, int):
                continue
            parsed = self._parse_report_target(r.get("target", ""))
            if parsed is None:
                continue
            kind, value = parsed
            created_at = r.get("createdAt")
            user_name = r.get("name")

            # latest_status: take the FIRST (newest) of each kind we see.
            if latest_status[kind] is None:
                latest_status[kind] = value
                if latest_status["last_action"] is None:
                    latest_status["last_action"] = created_at
                    latest_status["last_user"] = user_name

            # Replay filter.
            if rid <= last_event_id:
                continue

            if recency_cutoff_iso is not None and created_at:
                try:
                    # ISO 8601 with 'Z' suffix; Python's fromisoformat doesn't accept Z
                    # before 3.11, so be defensive.
                    iso = created_at.replace("Z", "+00:00") if isinstance(created_at, str) else None
                    from datetime import datetime

                    if iso:
                        ts = datetime.fromisoformat(iso).timestamp()
                        if ts < recency_cutoff_iso:
                            continue
                except Exception:  # noqa: BLE001
                    # If we can't parse, err on the side of replaying.
                    pass

            replayable.append(
                {
                    "id": rid,
                    "kind": kind,
                    "value": value,
                    "created_at": created_at,
                    "user": user_name,
                }
            )
            if rid > newest_id:
                newest_id = rid

        replayable.reverse()  # chronological

        return {"events": replayable, "latest_status": latest_status, "newest_id": newest_id}

    async def get_latest_device_status(self, serial: str) -> Dict[str, Any]:
        """Backward-compat alias — returns just the latest_status portion of fetch_device_events_since.

        Kept for any external callers that may still depend on the old name.
        """
        result = await self.fetch_device_events_since(serial, last_event_id=0)
        return result["latest_status"]

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
