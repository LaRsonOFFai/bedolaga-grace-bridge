#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_env(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip().strip("\"'")
    return result


def command(args: list[str], *, cwd: Path) -> bool:
    completed = subprocess.run(args, cwd=cwd, check=False, timeout=300)
    return completed.returncode == 0


def latest_backup(state_dir: Path) -> Path:
    candidates = sorted(
        (
            item
            for item in state_dir.joinpath("backups").glob("20*")
            if item.is_dir() and (item / "manifest.json").is_file() and not (item / ".incomplete").exists()
        ),
        reverse=True,
    )
    if not candidates:
        raise RuntimeError("Завершённая резервная копия не найдена")
    return candidates[0]


def verify(manifest: dict) -> None:
    dump = Path(manifest["database_dump"])
    if not dump.is_file() or sha256(dump) != manifest["database_dump_sha256"]:
        raise RuntimeError("Контрольная сумма database.dump не совпала")
    for entry in manifest["files"]:
        stored = Path(entry["stored"])
        if not stored.is_file() or sha256(stored) != entry["sha256"]:
            raise RuntimeError(f"Повреждён файл резервной копии: {stored}")


def restore_files(manifest: dict) -> int:
    restored = 0
    for entry in manifest["files"]:
        source = Path(entry["stored"])
        destination = Path(entry["source"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".rescue")
        shutil.copy2(source, temporary)
        temporary.chmod(int(entry["mode"]))
        os.replace(temporary, destination)
        restored += 1
    return restored


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/bedolaga-grace-bridge"))
    parser.add_argument("--config-dir", type=Path, default=Path("/etc/bedolaga-grace-bridge"))
    args = parser.parse_args()
    if os.name != "posix" or os.geteuid() != 0:
        raise SystemExit("Запустите через sudo на Linux")
    config = parse_env(args.config_dir / "config.env")
    bedolaga_dir = Path(config.get("BEDOLAGA_DIR", "/opt/bedolaga"))
    compose = Path(config.get("BEDOLAGA_COMPOSE_FILE", "docker-compose.yml"))
    if not compose.is_absolute():
        compose = bedolaga_dir / compose
    service = config.get("BEDOLAGA_SERVICE", "bedolaga")
    backup = latest_backup(args.state_dir)
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    verify(manifest)
    print(f"Проверена резервная копия: {backup.name}")
    if input("Вернуть исходные файлы и образ Bedolaga? [y/N] ").strip().lower() != "y":
        print("Отменено. Ничего не изменено.")
        return 1

    override = args.state_dir / "docker-compose.grace-bridge.override.yml"
    if override.exists():
        command(
            ["docker", "compose", "-f", str(compose), "-f", str(override), "stop", "grace-bridge"],
            cwd=bedolaga_dir,
        )
    restored = restore_files(manifest)
    running = command(
        ["docker", "compose", "-f", str(compose), "up", "-d", service],
        cwd=bedolaga_dir,
    )
    print(f"Восстановлено файлов: {restored}")
    print("Полный дамп БД намеренно не восстанавливался.")
    print(
        "Grace overlay пользователей не изменялся автономным rescue; "
        "после запуска выполните gracectl rollback."
    )
    print("Bedolaga запущена." if running else "Bedolaga требует ручной проверки.")
    return 0 if running else 4


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"Аварийное восстановление остановлено: {error}", file=sys.stderr)
        raise SystemExit(3) from error
