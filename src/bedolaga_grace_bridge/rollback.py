from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .backup import BackupManifest, load_backup
from .config import BridgeConfig, Paths
from .runner import run
from .state import InstallationState


@dataclass(frozen=True, slots=True)
class RollbackResult:
    backup_id: str
    restored_files: int
    bedolaga_running: bool


def safe_rollback(config: BridgeConfig, paths: Paths, backup_path: Path) -> RollbackResult:
    """Restore code/config without restoring the historical database dump."""
    manifest: BackupManifest = load_backup(backup_path)
    state = InstallationState.load(paths.state_file)
    state.update(phase="rollback_in_progress").save(paths.state_file)

    override = paths.state_dir / "docker-compose.grace-bridge.override.yml"
    compose_args = ["docker", "compose", "-f", str(config.compose_file)]
    if override.exists():
        compose_args.extend(["-f", str(override)])
        run([*compose_args, "stop", "grace-bridge"], cwd=config.bedolaga_dir, check=False, timeout=120)

    restored = 0
    for entry in manifest.files:
        source = Path(entry.stored)
        destination = Path(entry.source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".grace-rollback")
        shutil.copy2(source, temporary)
        if os.name != "nt":
            temporary.chmod(entry.mode)
        os.replace(temporary, destination)
        restored += 1

    # The original compose definition now controls the image again. We never
    # feed database.dump to pg_restore automatically because payments may have
    # been committed after the backup.
    start = run(
        ["docker", "compose", "-f", str(config.compose_file), "up", "-d", config.bedolaga_service],
        cwd=config.bedolaga_dir,
        check=False,
        timeout=300,
    )
    running = start.ok
    state.update(
        phase="rolled_back" if running else "rollback_needs_attention",
        activation_percent=0,
        last_error=None if running else (start.stderr.strip() or start.stdout.strip()),
    ).save(paths.state_file)
    return RollbackResult(manifest.backup_id, restored, running)
