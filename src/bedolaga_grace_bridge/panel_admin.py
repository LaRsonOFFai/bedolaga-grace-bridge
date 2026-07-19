from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from uuid import UUID


class PanelApiError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PanelInbound:
    uuid: str
    profile_uuid: str
    tag: str
    protocol: str
    network: str
    security: str
    port: int | None


@dataclass(frozen=True, slots=True)
class PanelSquad:
    uuid: str
    name: str
    inbound_uuids: tuple[str, ...]


def _unwrap(payload: Any) -> Any:
    if isinstance(payload, dict) and "response" in payload:
        return payload["response"]
    return payload


def _collection(payload: Any, *keys: str) -> list[dict[str, Any]]:
    payload = _unwrap(payload)
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        for key in keys:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


class PanelAdminClient:
    def __init__(self, base_url: str, token: str, *, timeout: int = 20) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(  # noqa: S310 - URL comes from explicit admin input
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
                "X-Api-Key": self.token,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                raw = response.read()
        except urllib.error.HTTPError as error:
            raise PanelApiError(f"Remnawave API: HTTP {error.code}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            reason = error.reason if hasattr(error, "reason") else error
            raise PanelApiError(f"Remnawave API недоступен: {reason}") from error
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError as error:
            raise PanelApiError("Remnawave API вернул некорректный JSON") from error

    def health(self) -> None:
        self.request("GET", "/api/system/health")

    def list_inbounds(self) -> list[PanelInbound]:
        rows = _collection(self.request("GET", "/api/config-profiles/inbounds"), "inbounds")
        result: list[PanelInbound] = []
        for row in rows:
            try:
                uuid = str(UUID(str(row["uuid"])))
                profile_uuid = str(UUID(str(row.get("profileUuid") or row.get("profile_uuid"))))
            except (KeyError, ValueError, TypeError):
                continue
            raw_port = row.get("port")
            result.append(
                PanelInbound(
                    uuid=uuid,
                    profile_uuid=profile_uuid,
                    tag=str(row.get("tag", "")),
                    protocol=str(row.get("type", "")),
                    network=str(row.get("network") or ""),
                    security=str(row.get("security") or ""),
                    port=int(raw_port) if isinstance(raw_port, (int, float)) else None,
                )
            )
        return result

    def list_internal_squads(self) -> list[PanelSquad]:
        rows = _collection(
            self.request("GET", "/api/internal-squads"),
            "internalSquads",
            "internal_squads",
        )
        result: list[PanelSquad] = []
        for row in rows:
            try:
                uuid = str(UUID(str(row["uuid"])))
            except (KeyError, ValueError, TypeError):
                continue
            inbounds = row.get("inbounds") or []
            inbound_uuids = tuple(
                str(item["uuid"]) for item in inbounds if isinstance(item, dict) and item.get("uuid")
            )
            result.append(PanelSquad(uuid, str(row.get("name") or uuid), inbound_uuids))
        return result

    def create_internal_squad(self, name: str, inbound_uuids: list[str]) -> PanelSquad:
        payload = _unwrap(
            self.request(
                "POST",
                "/api/internal-squads",
                {"name": name, "inbounds": inbound_uuids},
            )
        )
        if not isinstance(payload, dict):
            raise PanelApiError("Remnawave не вернул созданный internal squad")
        try:
            uuid = str(UUID(str(payload["uuid"])))
        except (KeyError, ValueError, TypeError) as error:
            raise PanelApiError("Remnawave вернул internal squad без UUID") from error
        return PanelSquad(uuid, str(payload.get("name") or name), tuple(inbound_uuids))

    def delete_internal_squad(self, squad_uuid: str) -> None:
        self.request("DELETE", f"/api/internal-squads/{UUID(squad_uuid)}")
