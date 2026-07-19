from __future__ import annotations

import json
from pathlib import Path

import pytest

from bedolaga_grace_bridge.backup import BackupError, load_backup
from bedolaga_grace_bridge.compatibility import sha256_file


def create_backup(tmp_path: Path) -> Path:
    root = tmp_path / "20260719T000000Z"
    root.mkdir()
    stored = root / "files" / "compose.yml"
    stored.parent.mkdir()
    stored.write_text("services: {}\n", encoding="utf-8")
    dump = root / "database.dump"
    dump.write_bytes(b"x" * 2048)
    manifest = {
        "schema": 1,
        "backup_id": root.name,
        "created_at": "2026-07-19T00:00:00+00:00",
        "bedolaga_dir": "/opt/bedolaga",
        "compose_file": "/opt/bedolaga/compose.yml",
        "files": [
            {
                "source": "/opt/bedolaga/compose.yml",
                "stored": str(stored),
                "sha256": sha256_file(stored),
                "mode": 0o600,
            }
        ],
        "database_dump": str(dump),
        "database_dump_sha256": sha256_file(dump),
        "original_container_id": "container",
        "original_image": "sha256:image",
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def test_complete_backup_loads(tmp_path: Path) -> None:
    root = create_backup(tmp_path)
    assert load_backup(root).backup_id == root.name


def test_tampered_backup_is_rejected(tmp_path: Path) -> None:
    root = create_backup(tmp_path)
    (root / "database.dump").write_bytes(b"tampered")
    with pytest.raises(BackupError, match="Контрольная сумма"):
        load_backup(root)


def test_incomplete_backup_is_rejected(tmp_path: Path) -> None:
    root = create_backup(tmp_path)
    (root / ".incomplete").write_text("yes", encoding="utf-8")
    with pytest.raises(BackupError, match="не завершена"):
        load_backup(root)
