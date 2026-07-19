from __future__ import annotations

import json

from bedolaga_grace_bridge import cli
from bedolaga_grace_bridge.cli import build_parser, cmd_menu, cmd_wizard
from bedolaga_grace_bridge.state import InstallationState


def test_cli_without_subcommand_opens_menu() -> None:
    args = build_parser().parse_args([])
    assert args.handler is cmd_menu


def test_wizard_is_available_without_removing_individual_commands() -> None:
    parser = build_parser()

    assert parser.parse_args(["wizard"]).handler is cmd_wizard
    assert parser.parse_args(["preflight"]).handler is cli.cmd_preflight
    assert parser.parse_args(["rollback"]).handler is cli.cmd_rollback


def test_old_state_loads_with_new_managed_squad_field(tmp_path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"phase": "observing", "bridge_version": "0.1.0"}))

    state = InstallationState.load(state_file)

    assert state.phase == "observing"
    assert state.bridge_version == "0.1.0"
    assert state.managed_squad_uuid is None


def test_wizard_runs_complete_safe_sequence(monkeypatch, tmp_path, capsys) -> None:
    config_dir = tmp_path / "config"
    state_dir = tmp_path / "state"
    log_dir = tmp_path / "log"
    state_file = state_dir / "state.json"
    args = build_parser().parse_args(
        [
            "--config-dir",
            str(config_dir),
            "--state-dir",
            str(state_dir),
            "--log-dir",
            str(log_dir),
            "wizard",
        ]
    )
    calls: list[str] = []

    def save_phase(phase: str, percent: int = 0) -> None:
        state = InstallationState.load(state_file)
        state.update(phase=phase, activation_percent=percent).save(state_file)

    def configure(_args) -> int:
        calls.append("configure")
        config_dir.mkdir(parents=True)
        (config_dir / "config.env").write_text("configured=true\n", encoding="utf-8")
        (config_dir / "secrets.env").write_text("secret=true\n", encoding="utf-8")
        return 0

    def install(_args) -> int:
        calls.append("install")
        save_phase("installed_disabled")
        return 0

    def observe(_args) -> int:
        calls.append("observe")
        save_phase("observing")
        return 0

    def canary(_args) -> int:
        calls.append("canary")
        save_phase("canary_running")
        return 0

    def approve(_args) -> int:
        calls.append("approve")
        save_phase("canary_verified")
        return 0

    def activate(command_args) -> int:
        calls.append(f"activate-{command_args.percent}")
        save_phase("active", command_args.percent)
        return 0

    monkeypatch.setattr(cli, "cmd_configure", configure)
    monkeypatch.setattr(cli, "cmd_install", install)
    monkeypatch.setattr(cli, "cmd_observe", observe)
    monkeypatch.setattr(cli, "cmd_canary", canary)
    monkeypatch.setattr(cli, "cmd_approve_canary", approve)
    monkeypatch.setattr(cli, "cmd_activate", activate)
    monkeypatch.setattr(cli, "_confirm", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        cli,
        "_choice",
        lambda _prompt, choices, *, default: "1" if "1" in choices else default,
    )

    assert cmd_wizard(args) == 0
    assert calls == [
        "configure",
        "install",
        "observe",
        "canary",
        "approve",
        "activate-5",
        "activate-25",
        "activate-50",
        "activate-100",
    ]
    assert InstallationState.load(state_file).activation_percent == 100
    assert "Continuity включён для 100%" in capsys.readouterr().out


def test_wizard_does_not_reinstall_completed_system(tmp_path, capsys) -> None:
    state_dir = tmp_path / "state"
    state = InstallationState(phase="active", activation_percent=100)
    state.save(state_dir / "state.json")
    args = build_parser().parse_args(
        [
            "--config-dir",
            str(tmp_path / "config"),
            "--state-dir",
            str(state_dir),
            "--log-dir",
            str(tmp_path / "log"),
            "wizard",
        ]
    )

    assert cmd_wizard(args) == 0
    assert "уже включён для 100%" in capsys.readouterr().out
