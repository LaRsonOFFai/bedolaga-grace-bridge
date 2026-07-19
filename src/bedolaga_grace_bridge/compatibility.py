from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .runner import CommandError, run


@dataclass(frozen=True, slots=True)
class DetectedBedolaga:
    version: str | None
    commit: str | None
    source_dir: Path


@dataclass(frozen=True, slots=True)
class CompatibilityRecord:
    version: str
    commit: str
    status: str
    patch: str
    patch_sha256: str
    files: dict[str, str]


@dataclass(frozen=True, slots=True)
class CompatibilityResult:
    compatible: bool
    reason: str
    detected: DetectedBedolaga
    record: CompatibilityRecord | None = None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def detect_bedolaga(source_dir: Path) -> DetectedBedolaga:
    commit: str | None = None
    version: str | None = None
    if (source_dir / ".git").exists():
        try:
            commit = run(["git", "rev-parse", "HEAD"], cwd=source_dir).stdout.strip()
            version = run(["git", "describe", "--tags", "--always"], cwd=source_dir).stdout.strip()
        except CommandError:
            pass
    if version is None:
        pyproject = source_dir / "pyproject.toml"
        if pyproject.exists():
            for line in pyproject.read_text(encoding="utf-8").splitlines():
                if line.strip().startswith("version") and "=" in line:
                    version = line.split("=", 1)[1].strip().strip("\"'")
                    break
    return DetectedBedolaga(version=version, commit=commit, source_dir=source_dir)


def load_manifest(path: Path) -> list[CompatibilityRecord]:
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != 1 or not isinstance(payload.get("supported"), list):
        raise ValueError("Неподдерживаемый формат compatibility.json")
    records: list[CompatibilityRecord] = []
    for raw in payload["supported"]:
        records.append(
            CompatibilityRecord(
                version=str(raw["version"]),
                commit=str(raw["commit"]),
                status=str(raw.get("status", "experimental")),
                patch=str(raw["patch"]),
                patch_sha256=str(raw.get("patchSha256", "")).lower(),
                files={str(key): str(value).lower() for key, value in raw.get("files", {}).items()},
            )
        )
    return records


def verify_compatibility(
    source_dir: Path,
    manifest_path: Path,
) -> CompatibilityResult:
    detected = detect_bedolaga(source_dir)
    if detected.commit is None:
        return CompatibilityResult(False, "Не удалось определить точный Git commit Bedolaga", detected)
    records = load_manifest(manifest_path)
    record = next((item for item in records if item.commit == detected.commit), None)
    if record is None:
        label = detected.version or detected.commit[:12]
        return CompatibilityResult(
            False,
            f"Версия Bedolaga {label} отсутствует в проверенной матрице совместимости",
            detected,
        )
    if record.status not in {"stable", "canary"}:
        return CompatibilityResult(
            False, f"Пакет совместимости имеет статус {record.status}", detected, record
        )
    for relative, expected in record.files.items():
        candidate = source_dir / relative
        if not candidate.is_file():
            return CompatibilityResult(False, f"Отсутствует проверяемый файл {relative}", detected, record)
        actual = sha256_file(candidate)
        if actual != expected:
            return CompatibilityResult(
                False,
                f"Контрольная сумма {relative} не совпала; локальные изменения не будут перезаписаны",
                detected,
                record,
            )
    return CompatibilityResult(True, "Версия и контрольные суммы подтверждены", detected, record)
