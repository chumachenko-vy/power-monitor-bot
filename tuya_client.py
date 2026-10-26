"""
Tuya Cloud API client — checks online/offline status and power consumption.
Uses the official Tuya OpenAPI (iot.tuya.com).
"""

import time
import hmac
import hashlib
import logging
from aiohttp import ClientSession, ClientTimeout

logger = logging.getLogger(__name__)


class TuyaCloud:
    BASE_URLS = {
        "eu": "https://openapi.tuyaeu.com",
        "us": "https://openapi.tuyaus.com",
        "cn": "https://openapi.tuyacn.com",
        "in": "https://openapi.tuyain.com",
    }

    def __init__(self, access_id: str, access_secret: str,
                 device_id: str, region: str = "eu"):
        self.access_id = access_id
        self.access_secret = access_secret
        self.device_id = device_id
        self.base_url = self.BASE_URLS.get(region, self.BASE_URLS["eu"])
        self.token = None
        self.token_expires = 0

    def _sign(self, method: str, path: str, timestamp: str,
              token: str = "") -> str:
        content_hash = hashlib.sha256(b"").hexdigest()
        string_to_sign = f"{method}\n{content_hash}\n\n{path}"
        raw = self.access_id + token + timestamp + string_to_sign
        return hmac.new(
            self.access_secret.encode(),
            raw.encode(),
            hashlib.sha256,
        ).hexdigest().upper()

    async def _request(self, method: str, path: str) -> dict:
        timestamp = str(int(time.time() * 1000))
        timeout = ClientTimeout(total=15)

        async with ClientSession(timeout=timeout) as session:
            if not self.token or time.time() > self.token_expires:
                await self._get_token(session)

            sign = self._sign(method, path, timestamp, self.token)
            headers = {
                "client_id": self.access_id,
                "sign": sign,
                "t": timestamp,
                "sign_method": "HMAC-SHA256",
                "access_token": self.token,
            }
            async with session.request(method, self.base_url + path,
                                       headers=headers) as resp:
                data = await resp.json()

            if not data.get("success"):
                code = data.get("code", "?")
                msg = data.get("msg", "?")
                logger.error(f"Tuya API error: {code} — {msg}")
                if code in (1010, 1011, 1012):
                    self.token = None
                    return await self._request(method, path)
            return data

    async def _get_token(self, session: ClientSession):
        path = "/v1.0/token?grant_type=1"
        timestamp = str(int(time.time() * 1000))
        sign = self._sign("GET", path, timestamp)
        headers = {
            "client_id": self.access_id,
            "sign": sign,
            "t": timestamp,
            "sign_method": "HMAC-SHA256",
        }
        async with session.get(self.base_url + path,
                               headers=headers) as resp:
            data = await resp.json()

        if data.get("success"):
            result = data["result"]
            self.token = result["access_token"]
            self.token_expires = time.time() + result["expire_time"] - 60
            logger.info("Tuya: token acquired")
        else:
            logger.error(f"Tuya token error: {data}")

    async def get_device_status(self) -> dict | None:
        """Returns device status: online, switch, power_w, etc."""
        try:
            info = await self._request(
                "GET", f"/v1.0/devices/{self.device_id}"
            )
            if not info.get("success"):
                return None

            result = info["result"]
            online = result.get("online", False)

            power_w = None
            voltage = None
            current_ma = None

            if online:
                status = await self._request(
                    "GET", f"/v1.0/devices/{self.device_id}/status"
                )
                if status.get("success"):
                    for item in status.get("result", []):
                        code = item.get("code", "")
                        value = item.get("value")
                        if code == "cur_power":
                            power_w = value / 10.0 if value else 0
                        elif code == "cur_voltage":
                            voltage = value / 10.0 if value else 0
                        elif code == "cur_current":
                            current_ma = value

            return {
                "online": online,
                "power_w": power_w,
                "voltage": voltage,
                "current_ma": current_ma,
                "name": result.get("name", ""),
            }
        except Exception as e:
            logger.error(f"Tuya status error: {e}")
            return None

    async def is_online(self) -> bool | None:
        """Simple check: online or not. None = error."""
        status = await self.get_device_status()
        if status is None:
            return None
        return status["online"]
