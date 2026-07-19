from __future__ import annotations

from uuid import UUID

import pytest

from bedolaga_grace_bridge.controller import _selected_for_rollout
from bedolaga_grace_bridge.runtime import RuntimeSettings


def base_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "DATABASE_DSN": "postgresql://user:password@db/database",
        "REMNAWAVE_API_URL": "https://panel.example.test",
        "REMNAWAVE_API_KEY": "secret",
        "GRACE_SQUAD_UUID": "11111111-1111-4111-8111-111111111111",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_default_mode_is_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in tuple(__import__("os").environ):
        if key.startswith("GRACE_") or key in {"DATABASE_DSN", "REMNAWAVE_API_URL", "REMNAWAVE_API_KEY"}:
            monkeypatch.delenv(key, raising=False)
    settings = RuntimeSettings.from_environment()
    assert settings.mode == "disabled"
    assert not settings.mutation_enabled


def test_observe_rejects_write_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    base_environment(monkeypatch)
    monkeypatch.setenv("GRACE_MODE", "observe")
    monkeypatch.setenv("GRACE_WRITE_ENABLED", "true")
    with pytest.raises(ValueError, match="read-only"):
        RuntimeSettings.from_environment()


def test_canary_requires_exact_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    base_environment(monkeypatch)
    monkeypatch.setenv("GRACE_MODE", "canary")
    monkeypatch.setenv("GRACE_WRITE_ENABLED", "true")
    with pytest.raises(ValueError, match="one UUID"):
        RuntimeSettings.from_environment()


def test_active_requires_confirmation(monkeypatch: pytest.MonkeyPatch) -> None:
    base_environment(monkeypatch)
    monkeypatch.setenv("GRACE_MODE", "active")
    monkeypatch.setenv("GRACE_WRITE_ENABLED", "true")
    monkeypatch.setenv("ACTIVATION_PERCENT", "5")
    with pytest.raises(ValueError, match="GRACE_ALL_USERS_CONFIRM"):
        RuntimeSettings.from_environment()


def test_rollout_is_deterministic_and_nested() -> None:
    users = [str(UUID(int=index + 1)) for index in range(40_000)]
    five = {user for user in users if _selected_for_rollout(user, 5)}
    twenty_five = {user for user in users if _selected_for_rollout(user, 25)}
    fifty = {user for user in users if _selected_for_rollout(user, 50)}
    all_users = {user for user in users if _selected_for_rollout(user, 100)}
    assert five <= twenty_five <= fifty <= all_users
    assert len(all_users) == 40_000
    assert 1_900 <= len(five) <= 2_100
