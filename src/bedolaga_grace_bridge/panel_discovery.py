from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from urllib.parse import urlsplit

from .runner import run


@dataclass(frozen=True, slots=True)
class PanelCandidate:
    url: str
    source: str
    local: bool
    container_name: str | None = None


def normalize_panel_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if not value:
        return ""
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Адрес панели должен быть корректным HTTP(S) URL")
    try:
        _ = parsed.port
    except ValueError as error:
        raise ValueError("В адресе панели указан некорректный порт") from error
    if not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Адрес панели не должен содержать логин или пароль")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("Укажите корневой URL панели без пути, query и fragment")
    return value


def candidate_from_bedolaga_env(values: dict[str, str]) -> PanelCandidate | None:
    raw = values.get("REMNAWAVE_API_URL", "").strip()
    if not raw:
        return None
    try:
        url = normalize_panel_url(raw)
    except ValueError:
        return None
    return PanelCandidate(url=url, source="настройки Bedolaga", local=False)


def _public_url(environment: list[str]) -> str:
    allowed = {
        "REMNAWAVE_API_URL",
        "FRONT_END_DOMAIN",
        "FRONTEND_DOMAIN",
        "PANEL_DOMAIN",
    }
    values: dict[str, str] = {}
    for item in environment:
        key, separator, value = item.partition("=")
        if separator and key in allowed:
            values[key] = value
    for key in ("REMNAWAVE_API_URL", "FRONT_END_DOMAIN", "FRONTEND_DOMAIN", "PANEL_DOMAIN"):
        try:
            normalized = normalize_panel_url(values.get(key, ""))
        except ValueError:
            continue
        if normalized:
            return normalized
    return ""


def _is_panel_container(record: dict[str, object]) -> bool:
    image = str(record.get("Image", "")).lower()
    name = str(record.get("Names", "")).lower()
    if "remnawave/node" in image or "subscription-page" in image:
        return False
    return "remnawave/backend" in image or name in {"remnawave", "remnawave-panel"}


def discover_local_panels() -> list[PanelCandidate]:
    """Return only public panel URLs; never expose the container environment."""
    if not shutil.which("docker"):
        return []
    listed = run(["docker", "ps", "--format", "{{json .}}"], check=False, timeout=15)
    if not listed.ok:
        return []
    candidates: list[PanelCandidate] = []
    seen: set[str] = set()
    for raw in listed.stdout.splitlines():
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or not _is_panel_container(record):
            continue
        identifier = str(record.get("ID", "")).strip()
        name = str(record.get("Names", "")).strip() or None
        if not identifier:
            continue
        inspected = run(
            ["docker", "inspect", "--format", "{{json .Config.Env}}", identifier],
            check=False,
            timeout=15,
        )
        if not inspected.ok:
            continue
        try:
            environment = json.loads(inspected.stdout)
        except json.JSONDecodeError:
            continue
        if not isinstance(environment, list):
            continue
        url = _public_url([str(item) for item in environment])
        if not url or url in seen:
            continue
        seen.add(url)
        candidates.append(
            PanelCandidate(
                url=url,
                source="локальный Docker",
                local=True,
                container_name=name,
            )
        )
    return candidates


def merge_candidates(*groups: list[PanelCandidate]) -> list[PanelCandidate]:
    result: list[PanelCandidate] = []
    seen: set[str] = set()
    for group in groups:
        for candidate in group:
            if candidate.url in seen:
                continue
            seen.add(candidate.url)
            result.append(candidate)
    return result
