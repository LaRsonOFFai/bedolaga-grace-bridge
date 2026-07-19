from __future__ import annotations

import pytest

from bedolaga_grace_bridge.panel_discovery import (
    PanelCandidate,
    _public_url,
    candidate_from_bedolaga_env,
    merge_candidates,
    normalize_panel_url,
)


def test_normalize_panel_url_accepts_root_and_adds_https() -> None:
    assert normalize_panel_url("panel.example.com/") == "https://panel.example.com"
    assert normalize_panel_url("http://127.0.0.1:3000") == "http://127.0.0.1:3000"


@pytest.mark.parametrize(
    "value",
    [
        "ftp://panel.example.com",
        "https://panel.example.com/api",
        "https://panel.example.com?token=secret",
        "https://admin:secret@panel.example.com",
        "https://panel.example.com:invalid",
    ],
)
def test_normalize_panel_url_rejects_unsafe_or_non_root_values(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_panel_url(value)


def test_bedolaga_candidate_uses_only_panel_url() -> None:
    candidate = candidate_from_bedolaga_env(
        {
            "REMNAWAVE_API_URL": "https://panel.example.com/",
            "REMNAWAVE_API_KEY": "must-not-be-copied",
        }
    )
    assert candidate == PanelCandidate(
        url="https://panel.example.com",
        source="настройки Bedolaga",
        local=False,
    )


def test_docker_environment_parser_ignores_secrets() -> None:
    environment = [
        "DATABASE_PASSWORD=secret",
        "REMNAWAVE_API_KEY=secret",
        "FRONT_END_DOMAIN=https://panel.example.com",
    ]
    assert _public_url(environment) == "https://panel.example.com"


def test_merge_candidates_preserves_order_and_deduplicates() -> None:
    first = PanelCandidate("https://panel.example.com", "Bedolaga", False)
    duplicate = PanelCandidate("https://panel.example.com", "Docker", True, "remnawave")
    second = PanelCandidate("https://other.example.com", "Docker", True, "remnawave-2")

    assert merge_candidates([first], [duplicate, second]) == [first, second]
