from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from .compatibility import CompatibilityRecord, sha256_file
from .config import BridgeConfig, Paths
from .runner import run


class PatchError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Candidate:
    image: str
    staging_dir: Path
    patch: Path


def _ignore_source(_directory: str, names: list[str]) -> set[str]:
    blocked = {
        ".git",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".env",
    }
    return {name for name in names if name in blocked or name.endswith((".pyc", ".log"))}


def prepare_candidate(
    config: BridgeConfig,
    paths: Paths,
    record: CompatibilityRecord,
    bridge_home: Path,
    backup_id: str,
) -> Candidate:
    patch = (bridge_home / record.patch).resolve()
    try:
        patch.relative_to(bridge_home.resolve())
    except ValueError as error:
        raise PatchError("Путь патча выходит за пределы дистрибутива") from error
    if not patch.is_file():
        raise PatchError(f"Патч не найден: {patch}")
    if not record.patch_sha256 or sha256_file(patch) != record.patch_sha256:
        raise PatchError("Контрольная сумма патча не совпала")

    staging = paths.state_dir / "staging" / backup_id
    if staging.exists():
        shutil.rmtree(staging)
    staging.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copytree(config.bedolaga_dir, staging, ignore=_ignore_source)

    check = run(["git", "apply", "--check", str(patch)], cwd=staging, check=False)
    if not check.ok:
        raise PatchError(check.stderr.strip() or "Патч не применим к временной копии")
    apply_result = run(["git", "apply", str(patch)], cwd=staging, check=False)
    if not apply_result.ok:
        raise PatchError(apply_result.stderr.strip() or "Не удалось применить патч")

    image = f"bedolaga-grace-bridge/bedolaga:{record.version}-{backup_id.lower()}"
    build = run(["docker", "build", "--pull=false", "-t", image, "."], cwd=staging, check=False, timeout=1800)
    if not build.ok:
        raise PatchError(build.stderr.strip() or build.stdout[-4000:] or "Сборка образа завершилась ошибкой")
    return Candidate(image=image, staging_dir=staging, patch=patch)
