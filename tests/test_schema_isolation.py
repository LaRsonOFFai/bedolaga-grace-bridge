from pathlib import Path


def test_schema_does_not_modify_bedolaga_alembic() -> None:
    sql = Path("schema/001_initial.sql").read_text(encoding="utf-8")
    assert "CREATE SCHEMA IF NOT EXISTS grace_bridge" in sql
    assert "alembic_version" not in sql
    assert "grace_bridge.access_state" in sql
    assert "grace_bridge.commands" in sql
