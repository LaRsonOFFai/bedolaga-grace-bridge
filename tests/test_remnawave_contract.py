from datetime import UTC, datetime

from bedolaga_grace_bridge.remnawave import panel_matches_grace, panel_snapshot


def test_panel_contract_accepts_string_squads() -> None:
    expires_at = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    user = {
        "status": "ACTIVE",
        "expireAt": "2026-07-19T12:00:04Z",
        "trafficLimitBytes": 1024,
        "trafficLimitStrategy": "NO_RESET",
        "activeInternalSquads": ["555271fb-9170-4587-a32f-1436383cfa94"],
        "tag": "GRACE_ACCESS",
    }
    assert panel_matches_grace(
        user,
        expires_at=expires_at,
        traffic_limit_bytes=1024,
        squad_uuid="555271fb-9170-4587-a32f-1436383cfa94",
        tag="GRACE_ACCESS",
    )
    assert panel_snapshot(user)["activeInternalSquads"] == ["555271fb-9170-4587-a32f-1436383cfa94"]


def test_panel_contract_rejects_wrong_reset_strategy() -> None:
    expires_at = datetime(2026, 7, 19, 12, 0, tzinfo=UTC)
    user = {
        "status": "ACTIVE",
        "expireAt": expires_at.isoformat(),
        "trafficLimitBytes": 1024,
        "trafficLimitStrategy": "DAY",
        "activeInternalSquads": [{"uuid": "555271fb-9170-4587-a32f-1436383cfa94"}],
        "tag": "GRACE_ACCESS",
    }
    assert not panel_matches_grace(
        user,
        expires_at=expires_at,
        traffic_limit_bytes=1024,
        squad_uuid="555271fb-9170-4587-a32f-1436383cfa94",
        tag="GRACE_ACCESS",
    )
