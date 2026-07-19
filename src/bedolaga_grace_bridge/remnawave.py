from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import aiohttp

from .runtime import RuntimeSettings


class RemnawaveError(RuntimeError):
    pass


class RemnawaveClient:
    def __init__(self, settings: RuntimeSettings) -> None:
        self.settings = settings
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> RemnawaveClient:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.settings.remnawave_api_key}",
            "X-Api-Key": self.settings.remnawave_api_key,
        }
        self.session = aiohttp.ClientSession(
            headers=headers,
            connector=aiohttp.TCPConnector(limit=self.settings.command_workers),
            timeout=aiohttp.ClientTimeout(total=45, connect=15),
        )
        return self

    async def __aexit__(self, *_args: Any) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    async def request(
        self, method: str, endpoint: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if self.session is None:
            raise RemnawaveError("Remnawave session is not initialized")
        url = f"{self.settings.remnawave_api_url}{endpoint}"
        for attempt in range(4):
            try:
                async with self.session.request(method, url, json=payload) as response:
                    text = await response.text()
                    body = json.loads(text) if text else {}
                    if response.status in {429, 502, 503, 504} and attempt < 3:
                        await asyncio.sleep(min(float(response.headers.get("Retry-After", 2**attempt)), 15))
                        continue
                    if response.status >= 400:
                        raise RemnawaveError(
                            f"Remnawave {method} {endpoint}: {body.get('message') or response.status}"
                        )
                    return body
            except (aiohttp.ClientError, TimeoutError, json.JSONDecodeError) as error:
                if attempt == 3:
                    raise RemnawaveError(f"Remnawave {method} {endpoint}: {error}") from error
                await asyncio.sleep(2**attempt)
        raise RemnawaveError("Remnawave request exhausted retries")

    async def get_user(self, user_uuid: str) -> dict[str, Any]:
        response = await self.request("GET", f"/api/users/{user_uuid}")
        user = response.get("response")
        if not isinstance(user, dict):
            raise RemnawaveError("Invalid Remnawave user response")
        return user

    async def apply_grace(
        self,
        user_uuid: str,
        expires_at: datetime,
        traffic_limit_bytes: int,
        squad_uuid: str,
        tag: str,
        *,
        reset_traffic: bool,
    ) -> dict[str, Any]:
        await self.request(
            "PATCH",
            "/api/users",
            {
                "uuid": user_uuid,
                "status": "ACTIVE",
                "expireAt": expires_at.astimezone(UTC).isoformat(),
                "trafficLimitBytes": traffic_limit_bytes,
                "trafficLimitStrategy": "NO_RESET",
                "activeInternalSquads": [squad_uuid],
                "tag": tag,
            },
        )
        if reset_traffic:
            await self.request("POST", f"/api/users/{user_uuid}/actions/reset-traffic")
        return await self.get_user(user_uuid)

    async def restore_snapshot(self, user_uuid: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "uuid": user_uuid,
            "status": snapshot.get("status", "DISABLED"),
            "expireAt": snapshot.get("expireAt"),
            "trafficLimitBytes": snapshot.get("trafficLimitBytes", 0),
            "trafficLimitStrategy": snapshot.get("trafficLimitStrategy", "NO_RESET"),
            "activeInternalSquads": snapshot.get("activeInternalSquads", []),
            "tag": snapshot.get("tag"),
        }
        await self.request("PATCH", "/api/users", payload)
        return await self.get_user(user_uuid)


def panel_snapshot(user: dict[str, Any]) -> dict[str, Any]:
    squads = sorted(_panel_squad_uuids(user))
    return {
        "status": user.get("status"),
        "expireAt": user.get("expireAt"),
        "trafficLimitBytes": user.get("trafficLimitBytes"),
        "trafficLimitStrategy": user.get("trafficLimitStrategy"),
        "activeInternalSquads": squads,
        "tag": user.get("tag"),
    }


def panel_matches_grace(
    user: dict[str, Any], *, expires_at: datetime, traffic_limit_bytes: int, squad_uuid: str, tag: str
) -> bool:
    squads = _panel_squad_uuids(user)
    raw_expiry = user.get("expireAt")
    try:
        actual_expiry = datetime.fromisoformat(str(raw_expiry).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return (
        str(user.get("status", "")).upper() == "ACTIVE"
        and abs((actual_expiry.astimezone(UTC) - expires_at.astimezone(UTC)).total_seconds()) <= 5
        and int(user.get("trafficLimitBytes") or 0) == traffic_limit_bytes
        and str(user.get("trafficLimitStrategy") or "").upper() == "NO_RESET"
        and squads == {squad_uuid}
        and user.get("tag") == tag
    )


def _panel_squad_uuids(user: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for squad in user.get("activeInternalSquads") or []:
        if isinstance(squad, str):
            result.add(squad)
        elif isinstance(squad, dict) and squad.get("uuid"):
            result.add(str(squad["uuid"]))
    return result
