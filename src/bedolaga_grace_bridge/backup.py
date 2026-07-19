from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .compatibility import CompatibilityRecord, sha256_file
from .config import BridgeConfig, Paths
from .runner import run


class BackupError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BackupEntry:
    source: str
    stored: str
    sha256: str
    mode: int


@dataclass(frozen=True, slots=True)
class BackupManifest:
    schema: int
    backup_id: str
    created_at: str
    bedolaga_dir: str
    compose_file: str
    files: tuple[BackupEntry, ...]
    database_dump: str
    database_dump_sha256: str
    original_container_id: str | None
    original_image: str | None


def _safe_copy(source: Path, destination: Path) -> BackupEntry:
    if source.is_symlink():
        raise BackupError(f"Отказ копировать символическую ссылку: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.copy2(source, destination)
    if os.name != "nt":
        destination.chmod(0o600)
    return BackupEntry(
        source=str(source),
        stored=str(destination),
        sha256=sha256_file(destination),
        mode=source.stat().st_mode & 0o777,
    )


def _dump_database(config: BridgeConfig, destination: Path) -> None:
    args = [
        "docker",
        "compose",
        "-f",
        str(config.compose_file),
        "exec",
        "-T",
        config.database_service,
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-acl",
        "--username",
        config.database_user,
        config.database_name,
    ]
    with destination.open("wb") as output:
        completed = subprocess.run(
            args,
            cwd=config.bedolaga_dir,
            stdout=output,
            stderr=subprocess.PIPE,
            check=False,
            timeout=1800,
        )
    if completed.returncode != 0:
        destination.unlink(missing_ok=True)
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise BackupError(f"pg_dump завершился ошибкой: {message}")
    if destination.stat().st_size < 1024:
        destination.unlink(missing_ok=True)
        raise BackupError("Дамп PostgreSQL подозрительно мал; установка остановлена")
    if os.name != "nt":
        destination.chmod(0o600)


def _container_identity(config: BridgeConfig) -> tuple[str | None, str | None]:
    result = run(
        [
            "docker",
            "compose",
            "-f",
            str(config.compose_file),
            "ps",
            "-q",
            config.bedolaga_service,
        ],
        cwd=config.bedolaga_dir,
        check=False,
    )
    container_id = result.stdout.strip() or None
    if not container_id:
        return None, None
    inspect = run(["docker", "inspect", "--format", "{{.Image}}", container_id], check=False)
    return container_id, inspect.stdout.strip() or None


def create_backup(
    config: BridgeConfig,
    paths: Paths,
    record: CompatibilityRecord,
) -> BackupManifest:
    backup_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    root = paths.backups_dir / backup_id
    incomplete = root / ".incomplete"
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    incomplete.write_text("backup in progress\n", encoding="utf-8")
    if os.name != "nt":
        incomplete.chmod(0o600)

    entries: list[BackupEntry] = []
    files: list[Path] = [config.compose_file]
    for optional in (
        config.bedolaga_dir / ".env",
        config.bedolaga_dir / "docker-compose.override.yml",
    ):
        if optional.is_file():
            files.append(optional)
    files.extend(config.bedolaga_dir / relative for relative in record.files)

    try:
        seen: set[Path] = set()
        for source in files:
            resolved = source.resolve()
            if resolved in seen or not source.is_file():
                continue
            seen.add(resolved)
            try:
                relative = source.relative_to(config.bedolaga_dir)
            except ValueError:
                relative = Path("external") / source.name
            entries.append(_safe_copy(source, root / "files" / relative))

        diff = run(["git", "diff", "--binary"], cwd=config.bedolaga_dir, check=False)
        diff_path = root / "working-tree.patch"
        diff_path.write_text(diff.stdout, encoding="utf-8")
        if os.name != "nt":
            diff_path.chmod(0o600)

        container_id, image = _container_identity(config)
        dump_path = root / "database.dump"
        _dump_database(config, dump_path)

        manifest = BackupManifest(
            schema=1,
            backup_id=backup_id,
            created_at=datetime.now(UTC).isoformat(),
            bedolaga_dir=str(config.bedolaga_dir),
            compose_file=str(config.compose_file),
            files=tuple(entries),
            database_dump=str(dump_path),
            database_dump_sha256=sha256_file(dump_path),
            original_container_id=container_id,
            original_image=image,
        )
        manifest_path = root / "manifest.json"
        manifest_path.write_text(
            json.dumps(asdict(manifest), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if os.name != "nt":
            manifest_path.chmod(0o600)
        incomplete.unlink()
        return manifest
    except Exception:
        # An incomplete backup is kept for diagnosis but can never be selected
        # by rollback because it has no trusted manifest.
        raise


def load_backup(path: Path) -> BackupManifest:
    manifest_path = path / "manifest.json"
    if not manifest_path.is_file() or (path / ".incomplete").exists():
        raise BackupError(f"Резервная копия {path} не завершена")
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = tuple(BackupEntry(**entry) for entry in raw.pop("files"))
    manifest = BackupManifest(files=entries, **raw)
    dump = Path(manifest.database_dump)
    if not dump.is_file() or sha256_file(dump) != manifest.database_dump_sha256:
        raise BackupError("Контрольная сумма дампа PostgreSQL не совпала")
    for entry in manifest.files:
        stored = Path(entry.stored)
        if not stored.is_file() or sha256_file(stored) != entry.sha256:
            raise BackupError(f"Повреждён файл резервной копии {stored}")
    return manifest


def list_backups(paths: Paths) -> list[Path]:
    if not paths.backups_dir.exists():
        return []
    return sorted(
        (item for item in paths.backups_dir.iterdir() if (item / "manifest.json").is_file()),
        reverse=True,
    )
