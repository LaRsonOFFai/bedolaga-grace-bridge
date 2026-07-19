from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from bedolaga_grace_bridge.compatibility import verify_compatibility


def git(directory: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=directory, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def repository(tmp_path: Path) -> tuple[Path, str, Path]:
    source = tmp_path / "bedolaga"
    source.mkdir()
    git(source, "init")
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.invalid")
    target = source / "app.py"
    target.write_text("original\n", encoding="utf-8")
    git(source, "add", "app.py")
    git(source, "commit", "-m", "base")
    return source, git(source, "rev-parse", "HEAD"), target


def write_manifest(path: Path, commit: str, digest: str) -> None:
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "supported": [
                    {
                        "version": "test",
                        "commit": commit,
                        "status": "stable",
                        "patch": "patches/test.patch",
                        "patchSha256": "0" * 64,
                        "files": {"app.py": digest},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_unknown_commit_is_blocked(tmp_path: Path) -> None:
    source, _commit, _target = repository(tmp_path)
    manifest = tmp_path / "compatibility.json"
    manifest.write_text('{"schema":1,"supported":[]}', encoding="utf-8")
    result = verify_compatibility(source, manifest)
    assert not result.compatible
    assert "отсутствует" in result.reason


def test_modified_touched_file_is_blocked(tmp_path: Path) -> None:
    source, commit, target = repository(tmp_path)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    manifest = tmp_path / "compatibility.json"
    write_manifest(manifest, commit, digest)
    assert verify_compatibility(source, manifest).compatible
    target.write_text("administrator customization\n", encoding="utf-8")
    result = verify_compatibility(source, manifest)
    assert not result.compatible
    assert "Контрольная сумма" in result.reason


def test_unrelated_custom_file_does_not_block(tmp_path: Path) -> None:
    source, commit, target = repository(tmp_path)
    manifest = tmp_path / "compatibility.json"
    write_manifest(manifest, commit, hashlib.sha256(target.read_bytes()).hexdigest())
    (source / "custom-auth.py").write_text("keep me\n", encoding="utf-8")
    assert verify_compatibility(source, manifest).compatible
