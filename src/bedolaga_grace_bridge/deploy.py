from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .config import BridgeConfig, Paths
from .runner import run


def _atomic_private_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    if os.name != "nt":
        temporary.chmod(0o600)
    os.replace(temporary, path)


def write_runtime(paths: Paths, *, mode: str, write_enabled: bool, activation_percent: int) -> Path:
    if mode not in {"disabled", "observe", "canary", "active"}:
        raise ValueError("Недопустимый режим Bridge")
    if mode in {"disabled", "observe"} and (write_enabled or activation_percent):
        raise ValueError("Read-only режим не может разрешать записи")
    if mode == "canary" and activation_percent != 0:
        raise ValueError("Canary не использует процент массового развёртывания")
    if mode == "active" and not 1 <= activation_percent <= 100:
        raise ValueError("Active требует процент от 1 до 100")
    path = paths.config_dir / "runtime.env"
    lines = [
        f"GRACE_MODE={mode}",
        f"GRACE_WRITE_ENABLED={'true' if write_enabled else 'false'}",
        f"ACTIVATION_PERCENT={activation_percent}",
    ]
    if mode == "active":
        lines.append("GRACE_ALL_USERS_CONFIRM=ENABLE_ALL_ELIGIBLE_USERS")
    lines.append("")
    _atomic_private_write(
        path,
        "\n".join(lines),
    )
    return path


def write_override(
    config: BridgeConfig,
    paths: Paths,
    *,
    bedolaga_image: str,
    bridge_image: str,
    integration_enabled: bool,
) -> Path:
    override = paths.state_dir / "docker-compose.grace-bridge.override.yml"
    content = f"""services:
  {config.bedolaga_service}:
    image: {bedolaga_image}
    environment:
      GRACE_INTEGRATION_ENABLED: \"{"true" if integration_enabled else "false"}\"
      GRACE_BRIDGE_SCHEMA: grace_bridge

  grace-bridge:
    image: {bridge_image}
    restart: unless-stopped
    env_file:
      - {paths.config_file}
      - {paths.secrets_file}
      - {paths.config_dir / "runtime.env"}
    environment:
      GRACE_BRIDGE_SCHEMA: grace_bridge
    read_only: true
    tmpfs:
      - /tmp:rw,noexec,nosuid,size=64m
    security_opt:
      - no-new-privileges:true
    cap_drop:
      - ALL
    depends_on:
      - {config.database_service}
"""
    _atomic_private_write(override, content)
    return override


def compose(config: BridgeConfig, override: Path, *args: str, timeout: int = 300, check: bool = True):
    return run(
        ["docker", "compose", "-f", str(config.compose_file), "-f", str(override), *args],
        cwd=config.bedolaga_dir,
        timeout=timeout,
        check=check,
    )


def deploy_disabled(config: BridgeConfig, paths: Paths, override: Path) -> None:
    write_runtime(paths, mode="disabled", write_enabled=False, activation_percent=0)
    result = compose(config, override, "up", "-d", "--no-deps", config.bedolaga_service, check=False)
    if not result.ok:
        raise RuntimeError(result.stderr.strip() or "Не удалось запустить кандидат Bedolaga")
    wait_service_ready(config, override, config.bedolaga_service)


def start_bridge(config: BridgeConfig, override: Path) -> None:
    result = compose(config, override, "up", "-d", "grace-bridge", check=False)
    if not result.ok:
        raise RuntimeError(result.stderr.strip() or "Не удалось запустить Grace Bridge")
    wait_service_ready(config, override, "grace-bridge")


def wait_service_ready(
    config: BridgeConfig,
    override: Path,
    service: str,
    *,
    timeout_seconds: int = 90,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    running_since: float | None = None
    last_state = "контейнер не создан"
    while time.monotonic() < deadline:
        identity = compose(config, override, "ps", "-q", service, check=False)
        container_id = identity.stdout.strip()
        if container_id:
            inspected = run(
                ["docker", "inspect", "--format", "{{json .State}}", container_id],
                check=False,
                timeout=30,
            )
            if inspected.ok:
                try:
                    state = json.loads(inspected.stdout)
                except json.JSONDecodeError:
                    state = {}
                last_state = str(state.get("Status") or "unknown")
                if state.get("Running"):
                    health = state.get("Health")
                    if isinstance(health, dict):
                        last_state = f"running/{health.get('Status', 'unknown')}"
                        if health.get("Status") == "healthy":
                            return
                    else:
                        running_since = running_since or time.monotonic()
                        if time.monotonic() - running_since >= 5:
                            return
                else:
                    running_since = None
                    if state.get("Status") in {"dead", "exited"}:
                        break
        time.sleep(1)
    raise RuntimeError(f"Service {service} не перешёл в готовое состояние: {last_state}")
