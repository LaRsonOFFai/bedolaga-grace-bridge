from pathlib import Path
from types import SimpleNamespace

import pytest

from bedolaga_grace_bridge import schema


def test_matching_migration_is_not_reapplied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    migration = tmp_path / "001_initial.sql"
    migration.write_text("SELECT 1;\n", encoding="utf-8")
    checksum = schema.hashlib.sha256(migration.read_bytes()).hexdigest()
    calls: list[bytes] = []

    def fake_psql(_config, sql: bytes, *, single_transaction: bool) -> str:
        calls.append(sql)
        if b"SELECT checksum" in sql:
            return checksum
        return ""

    monkeypatch.setattr(schema, "_psql", fake_psql)
    schema.apply_schema(SimpleNamespace(), tmp_path)

    assert len(calls) == 2
    assert b"CREATE SCHEMA" in calls[0]
    assert b"SELECT checksum" in calls[1]


def test_changed_migration_stops_before_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    migration = tmp_path / "001_initial.sql"
    migration.write_text("SELECT 1;\n", encoding="utf-8")
    calls: list[bytes] = []

    def fake_psql(_config, sql: bytes, *, single_transaction: bool) -> str:
        calls.append(sql)
        return "different-checksum" if b"SELECT checksum" in sql else ""

    monkeypatch.setattr(schema, "_psql", fake_psql)
    with pytest.raises(schema.SchemaError, match="Контрольная сумма"):
        schema.apply_schema(SimpleNamespace(), tmp_path)

    assert migration.read_bytes() not in calls
