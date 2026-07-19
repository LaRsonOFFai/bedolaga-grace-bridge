from __future__ import annotations

from pathlib import Path

BEDOLAGA_CANDIDATES = (
    Path("/opt/bedolaga"),
    Path("/opt/remnawave-bedolaga"),
    Path("/root/remnawave-bedolaga-telegram-bot"),
    Path("/srv/bedolaga"),
)


def discover_bedolaga_dir() -> Path | None:
    for candidate in BEDOLAGA_CANDIDATES:
        if (candidate / "docker-compose.yml").exists() or (candidate / "compose.yml").exists():
            return candidate
    return None


def discover_compose_file(directory: Path) -> Path | None:
    for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        candidate = directory / name
        if candidate.exists():
            return candidate
    return None
