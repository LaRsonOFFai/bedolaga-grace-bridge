from __future__ import annotations

import json

from bedolaga_grace_bridge.cli import build_parser, cmd_menu
from bedolaga_grace_bridge.state import InstallationState


def test_cli_without_subcommand_opens_menu() -> None:
    args = build_parser().parse_args([])
    assert args.handler is cmd_menu


def test_old_state_loads_with_new_managed_squad_field(tmp_path) -> None:
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"phase": "observing", "bridge_version": "0.1.0"}))

    state = InstallationState.load(state_file)

    assert state.phase == "observing"
    assert state.bridge_version == "0.1.0"
    assert state.managed_squad_uuid is None
