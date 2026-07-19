from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from .config import BridgeConfig


class SchemaError(RuntimeError):
    pass


def _psql(config: BridgeConfig, sql: bytes, *, single_transaction: bool) -> str:
    args = [
        "docker",
        "compose",
        "-f",
        str(config.compose_file),
        "exec",
        "-T",
        config.database_service,
        "psql",
        "--username",
        config.database_user,
        "--dbname",
        config.database_name,
        "--set",
        "ON_ERROR_STOP=1",
        "--tuples-only",
        "--no-align",
    ]
    if single_transaction:
        args.append("--single-transaction")
    completed = subprocess.run(
        args,
        cwd=config.bedolaga_dir,
        input=sql,
        capture_output=True,
        check=False,
        timeout=600,
    )
    if completed.returncode:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise SchemaError(f"Не удалось применить схему Grace Bridge: {message}")
    return completed.stdout.decode("utf-8", errors="replace").strip()


def _recorded_checksum(config: BridgeConfig, version: int) -> str | None:
    query = (f"SELECT checksum FROM grace_bridge.schema_migrations WHERE version={version};").encode()
    value = _psql(config, query, single_transaction=True)
    return value or None


def _record_migration(config: BridgeConfig, version: int, checksum: str) -> None:
    marker = (
        f"INSERT INTO grace_bridge.schema_migrations(version, checksum) VALUES ({version}, '{checksum}');"
    ).encode()
    _psql(config, marker, single_transaction=True)


def apply_schema(config: BridgeConfig, schema_dir: Path) -> None:
    migrations = sorted(schema_dir.glob("[0-9][0-9][0-9]_*.sql"))
    if not migrations:
        raise SchemaError(f"В {schema_dir} не найдены SQL-миграции")
    bootstrap = b"""
    CREATE SCHEMA IF NOT EXISTS grace_bridge;
    CREATE TABLE IF NOT EXISTS grace_bridge.schema_migrations (
        version integer PRIMARY KEY,
        applied_at timestamptz NOT NULL DEFAULT now(),
        checksum text NOT NULL
    );
    """
    _psql(config, bootstrap, single_transaction=True)
    for migration in migrations:
        payload = migration.read_bytes()
        version = int(migration.name.split("_", 1)[0])
        checksum = hashlib.sha256(payload).hexdigest()
        recorded = _recorded_checksum(config, version)
        if recorded == checksum:
            continue
        if recorded is not None:
            raise SchemaError(f"Контрольная сумма уже применённой миграции {version} изменилась")
        # Each migration controls its own transaction semantics. In particular,
        # migration 002 uses CREATE INDEX CONCURRENTLY and must stay outside a
        # transaction block.
        _psql(config, payload, single_transaction=False)
        _record_migration(config, version, checksum)
