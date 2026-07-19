from __future__ import annotations

import json
import os
import tarfile
from datetime import UTC, datetime
from pathlib import Path

from .config import BridgeConfig, Paths, parse_env_file
from .runner import run
from .security.redaction import redact
from .state import InstallationState


def create_bundle(config: BridgeConfig, paths: Paths) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    workspace = paths.reports_dir / f"report-{stamp}"
    workspace.mkdir(parents=True, exist_ok=False, mode=0o700)
    secrets = list(parse_env_file(paths.secrets_file).values())

    state = InstallationState.load(paths.state_file)
    (workspace / "state.json").write_text(
        redact(
            json.dumps(
                state.__dict__
                if hasattr(state, "__dict__")
                else {field: getattr(state, field) for field in state.__dataclass_fields__},
                ensure_ascii=False,
                indent=2,
            ),
            secrets,
        ),
        encoding="utf-8",
    )

    commands = {
        "docker-version.txt": ["docker", "version"],
        "compose-ps.txt": ["docker", "compose", "-f", str(config.compose_file), "ps"],
        "compose-config-services.txt": [
            "docker",
            "compose",
            "-f",
            str(config.compose_file),
            "config",
            "--services",
        ],
        "git-status.txt": ["git", "status", "--short"],
        "git-revision.txt": ["git", "rev-parse", "HEAD"],
    }
    for filename, args in commands.items():
        result = run(args, cwd=config.bedolaga_dir, check=False, timeout=60)
        content = f"exit={result.returncode}\n{result.stdout}\n{result.stderr}"
        (workspace / filename).write_text(redact(content, secrets), encoding="utf-8")

    if paths.log_dir.exists():
        for log in paths.log_dir.glob("*.log"):
            content = log.read_text(encoding="utf-8", errors="replace")[-2_000_000:]
            (workspace / log.name).write_text(redact(content, secrets), encoding="utf-8")

    archive = paths.reports_dir / f"grace-bridge-report-{stamp}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(workspace, arcname=workspace.name)
    if os.name != "nt":
        archive.chmod(0o600)
    return archive
