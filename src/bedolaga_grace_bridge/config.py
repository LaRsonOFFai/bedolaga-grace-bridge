from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_DIR = Path("/etc/bedolaga-grace-bridge")
DEFAULT_STATE_DIR = Path("/var/lib/bedolaga-grace-bridge")
DEFAULT_LOG_DIR = Path("/var/log/bedolaga-grace-bridge")


def parse_env_file(path: Path) -> dict[str, str]:
    """Read a deliberately small, non-executable KEY=VALUE format."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"{path}:{number}: ожидалось KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key.replace("_", "").isalnum() or not key[0].isalpha():
            raise ValueError(f"{path}:{number}: недопустимое имя параметра")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        values[key] = value
    return values


def _int(values: dict[str, str], key: str, default: int, minimum: int, maximum: int) -> int:
    value = int(values.get(key, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} должен быть между {minimum} и {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class Paths:
    config_dir: Path = DEFAULT_CONFIG_DIR
    state_dir: Path = DEFAULT_STATE_DIR
    log_dir: Path = DEFAULT_LOG_DIR

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.env"

    @property
    def secrets_file(self) -> Path:
        return self.config_dir / "secrets.env"

    @property
    def state_file(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def backups_dir(self) -> Path:
        return self.state_dir / "backups"

    @property
    def reports_dir(self) -> Path:
        return self.state_dir / "reports"


@dataclass(frozen=True, slots=True)
class BridgeConfig:
    bedolaga_dir: Path
    compose_file: Path
    bedolaga_service: str
    database_service: str
    database_name: str
    database_user: str
    database_dsn: str
    remnawave_api_url: str
    remnawave_api_key: str
    grace_squad_uuid: str
    grace_duration_days: int
    grace_traffic_limit_bytes: int
    candidate_batch_size: int
    command_workers: int
    max_command_attempts: int
    activation_percent: int
    canary_uuid: str | None

    @classmethod
    def load(cls, paths: Paths) -> BridgeConfig:
        public = parse_env_file(paths.config_file)
        secrets = parse_env_file(paths.secrets_file)
        values = {**public, **secrets}
        bedolaga_dir = Path(values.get("BEDOLAGA_DIR", "/opt/bedolaga")).resolve()
        compose_value = values.get("BEDOLAGA_COMPOSE_FILE", "docker-compose.yml")
        compose_file = Path(compose_value)
        if not compose_file.is_absolute():
            compose_file = bedolaga_dir / compose_file
        config = cls(
            bedolaga_dir=bedolaga_dir,
            compose_file=compose_file.resolve(),
            bedolaga_service=values.get("BEDOLAGA_SERVICE", "bedolaga").strip(),
            database_service=values.get("DATABASE_SERVICE", "postgres").strip(),
            database_name=values.get("DATABASE_NAME", "bedolaga").strip(),
            database_user=values.get("DATABASE_USER", "postgres").strip(),
            database_dsn=values.get("DATABASE_DSN", "").strip(),
            remnawave_api_url=values.get("REMNAWAVE_API_URL", "").rstrip("/"),
            remnawave_api_key=values.get("REMNAWAVE_API_KEY", ""),
            grace_squad_uuid=values.get("GRACE_SQUAD_UUID", ""),
            grace_duration_days=_int(values, "GRACE_DURATION_DAYS", 7, 1, 365),
            grace_traffic_limit_bytes=_int(
                values, "GRACE_TRAFFIC_LIMIT_BYTES", 1024**3, 16 * 1024**2, 1024**4
            ),
            candidate_batch_size=_int(values, "CANDIDATE_BATCH_SIZE", 500, 1, 5000),
            command_workers=_int(values, "COMMAND_WORKERS", 4, 1, 16),
            max_command_attempts=_int(values, "MAX_COMMAND_ATTEMPTS", 8, 1, 50),
            activation_percent=_int(values, "ACTIVATION_PERCENT", 0, 0, 100),
            canary_uuid=values.get("CANARY_REMNAWAVE_UUID") or None,
        )
        config.validate_static()
        return config

    def validate_static(self) -> None:
        if not self.bedolaga_service or not self.database_service:
            raise ValueError("Имена Docker Compose services не могут быть пустыми")
        for service in (self.bedolaga_service, self.database_service):
            if any(
                character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
                for character in service
            ):
                raise ValueError(f"Недопустимое имя Docker Compose service: {service}")
        if self.remnawave_api_url and not self.remnawave_api_url.startswith(("https://", "http://")):
            raise ValueError("REMNAWAVE_API_URL должен начинаться с https:// или http://")


def bridge_home() -> Path:
    configured = os.getenv("GRACE_BRIDGE_HOME", "").strip()
    if configured:
        return Path(configured).resolve()
    return Path(__file__).resolve().parents[2]


def assert_secret_permissions(path: Path) -> None:
    if os.name == "nt" or not path.exists():
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise PermissionError(f"{path} доступен другим пользователям; выполните chmod 600 {path}")
