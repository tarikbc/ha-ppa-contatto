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

# Reconnect: jittered exponential backoff. delay = min(BASE * 2^attempts, MAX) * jitter.
# We reset `attempts` to 0 the moment the FIRST frame arrives post-connect — that's the only
# honest "this connection is real" signal. No wall-clock-based reset.
WEBSOCKET_RECONNECT_BASE: Final = 1.0  # seconds — first retry is fast
WEBSOCKET_RECONNECT_MAX: Final = 300.0  # cap individual backoff at 5 min
WEBSOCKET_RECONNECT_JITTER: Final = 0.2  # ±20%

# Watchdog cadence. Tighter than v1.5.x because we now have many more cheap checks (no
# per-iteration log spam), and faster recovery is better when the cloud flaps.
WEBSOCKET_HEALTH_CHECK_INTERVAL: Final = 10  # seconds

# Stale-connection detection. We bias toward a fresh ping-pong probe over hard reconnect,
# so this is a safety upper bound for "no frames at all".
WEBSOCKET_STALE_TIMEOUT: Final = 60  # tightened from 90

# Active client ping cadence. We send Socket.IO "2" pings to (a) keep NAT/firewall warm,
# (b) actively probe half-open TCP — send() raises immediately on a dead socket.
WEBSOCKET_CLIENT_PING_INTERVAL: Final = 25
# How long we wait for the server's "3" pong after we send a "2" ping before we treat the
# connection as dead. Catches "TCP alive but server hung" cases.
WEBSOCKET_PONG_DEADLINE: Final = 5.0

# Some clouds eject long-lived connections deterministically. We preempt that by recycling
# our own connection every N hours with a graceful close — much smoother than waiting for
# the cloud to drop us in the middle of a gate event.
WEBSOCKET_PROACTIVE_RECYCLE: Final = 6 * 3600  # 6 hours

# Token refresh policy. We only re-auth proactively if N WS handshakes fail in a row;
# otherwise we trust the existing token. Refreshing on every reconnect attempt amplifies
# load on a struggling auth endpoint exactly when it can least handle it.
WEBSOCKET_AUTH_FAIL_THRESHOLD: Final = 5

# --- Event-sourced state replay -----------------------------------------------------
# The PPA cloud's /device/{serial}/reports endpoint is the canonical event log. We treat
# the WebSocket as a low-latency hint and the report log as the source of truth: every
# poll, we fetch any reports newer than the highest event ID we've already processed and
# replay them through the same state handler the WebSocket uses. Result: bounded
# staleness regardless of WS health, and zero lost gate events.
EVENT_REPLAY_LIMIT: Final = 50  # how many of the most-recent reports to scan per poll
EVENT_REPLAY_RECENCY_S: Final = 600  # only replay events within this many seconds of "now"
# (anything older is assumed already-handled history; prevents replay storms on first run
# after a long HA outage)

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
