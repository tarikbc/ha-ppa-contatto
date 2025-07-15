"""PPA Contatto API client."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    AUTH_HEADERS,
    AUTH_URL,
    DEFAULT_HEADERS,
    DEVICE_CONTROL_ENDPOINT,
    DEVICE_REPORTS_ENDPOINT,
    DEVICES_ENDPOINT,
    DEVICE_CONFIG_ENDPOINT,
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
        
        # Load stored tokens if available
        if config_entry and hasattr(config_entry, 'data'):
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
                auth_data = {
                    "email": self.email,
                    "password": self.password
                }
                
                async with self.session.post(
                    AUTH_URL,
                    headers=AUTH_HEADERS,
                    data=json.dumps(auth_data)
                ) as response:
                    if response.status != 200:
                        _LOGGER.error(
                            "Authentication failed with status %s: %s",
                            response.status,
                            await response.text()
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
            
            self.hass.config_entries.async_update_entry(
                self.config_entry, data=new_data
            )
            _LOGGER.debug("Tokens stored in config entry")

    async def _clear_tokens(self) -> None:
        """Clear stored tokens."""
        self.access_token = None
        self.refresh_token = None
        
        if self.config_entry:
            new_data = dict(self.config_entry.data)
            new_data.pop("access_token", None)
            new_data.pop("refresh_token", None)
            
            self.hass.config_entries.async_update_entry(
                self.config_entry, data=new_data
            )
            _LOGGER.debug("Tokens cleared from config entry")

    async def _refresh_access_token(self) -> bool:
        """Refresh access token using refresh token."""
        if not self.refresh_token:
            _LOGGER.debug("No refresh token available, re-authenticating")
            return await self.authenticate()
        
        try:
            refresh_data = {"refreshToken": self.refresh_token}
            
            # Try to find refresh endpoint (this might need adjustment based on actual API)
            refresh_url = "https://auth.ppacontatto.com.br/refresh"  # Assuming this exists
            
            async with self.session.post(
                refresh_url,
                headers=AUTH_HEADERS,
                data=json.dumps(refresh_data)
            ) as response:
                if response.status == 200:
                    data = await response.json()
                    self.access_token = data.get("accessToken")
                    
                    if self.access_token:
                        await self._store_tokens()
                        _LOGGER.debug("Token refreshed successfully")
                        return True
                
                _LOGGER.debug("Token refresh failed, re-authenticating")
                return await self.authenticate()
                
        except Exception as err:
            _LOGGER.debug("Token refresh error: %s, re-authenticating", err)
            return await self.authenticate()

    async def _make_authenticated_request(
        self, method: str, url: str, **kwargs
    ) -> Dict[str, Any]:
        """Make an authenticated request to the API."""
        if not self.access_token:
            await self.authenticate()
        
        headers = DEFAULT_HEADERS.copy()
        headers["Authorization"] = f"Bearer {self.access_token}"
        
        try:
            async with self.session.request(
                method, url, headers=headers, **kwargs
            ) as response:
                if response.status in (401, 400):
                    # 401 = unauthorized, 400 might also be auth-related
                    error_text = await response.text()
                    _LOGGER.warning("Auth error (status %s): %s", response.status, error_text)
                    
                    # Try to refresh/re-authenticate
                    if await self._refresh_access_token():
                        headers["Authorization"] = f"Bearer {self.access_token}"
                        
                        async with self.session.request(
                            method, url, headers=headers, **kwargs
                        ) as retry_response:
                            if retry_response.status == 200:
                                return await retry_response.json()
                            
                            error_text = await retry_response.text()
                            _LOGGER.error("Retry failed (status %s): %s", retry_response.status, error_text)
                            raise PPAContattoAPIError(
                                f"API request failed after auth retry: {retry_response.status} - {error_text}"
                            )
                    else:
                        raise PPAContattoAPIError("Authentication failed during retry")
                
                elif response.status != 200:
                    error_text = await response.text()
                    _LOGGER.error("API error (status %s): %s", response.status, error_text)
                    raise PPAContattoAPIError(
                        f"API request failed: {response.status} - {error_text}"
                    )
                
                return await response.json()
                
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
            
            await self._make_authenticated_request("POST", url, data=json.dumps(payload))
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
            latest_status = {"gate": None, "relay": None, "last_action": None, "last_user": None}
            
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
            _LOGGER.debug("Failed to get latest status for %s, falling back to basic status: %s", serial, err)
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