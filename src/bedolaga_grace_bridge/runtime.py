from __future__ import annotations

import os
from dataclasses import dataclass
from uuid import UUID

WRITE_CONFIRMATION = "ENABLE_ALL_ELIGIBLE_USERS"


def _boolean(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    value = int(os.getenv(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class RuntimeSettings:
    mode: str
    write_enabled: bool
    database_dsn: str
    remnawave_api_url: str
    remnawave_api_key: str
    grace_squad_uuid: UUID | None
    canary_uuid: UUID | None
    activation_percent: int
    grace_duration_days: int
    grace_traffic_limit_bytes: int
    grace_tag: str
    candidate_batch_size: int
    command_workers: int
    max_command_attempts: int
    scan_interval_seconds: int
    reconcile_interval_seconds: int
    health_port: int
    lock_namespace: int

    @classmethod
    def from_environment(cls) -> RuntimeSettings:
        mode = os.getenv("GRACE_MODE", "disabled").strip().lower()
        if mode not in {"disabled", "observe", "canary", "active"}:
            raise ValueError("GRACE_MODE must be disabled, observe, canary, or active")
        write_enabled = _boolean("GRACE_WRITE_ENABLED")
        activation_percent = _integer("ACTIVATION_PERCENT", 0, 0, 100)
        canary_raw = os.getenv("CANARY_REMNAWAVE_UUID", "").strip()
        squad_raw = os.getenv("GRACE_SQUAD_UUID", "").strip()
        settings = cls(
            mode=mode,
            write_enabled=write_enabled,
            database_dsn=os.getenv("DATABASE_DSN", "").strip(),
            remnawave_api_url=os.getenv("REMNAWAVE_API_URL", "").strip().rstrip("/"),
            remnawave_api_key=os.getenv("REMNAWAVE_API_KEY", "").strip(),
            grace_squad_uuid=UUID(squad_raw) if squad_raw else None,
            canary_uuid=UUID(canary_raw) if canary_raw else None,
            activation_percent=activation_percent,
            grace_duration_days=_integer("GRACE_DURATION_DAYS", 7, 1, 365),
            grace_traffic_limit_bytes=_integer("GRACE_TRAFFIC_LIMIT_BYTES", 1024**3, 16 * 1024**2, 1024**4),
            grace_tag=os.getenv("GRACE_TAG", "GRACE_ACCESS").strip() or "GRACE_ACCESS",
            candidate_batch_size=_integer("CANDIDATE_BATCH_SIZE", 500, 1, 5000),
            command_workers=_integer("COMMAND_WORKERS", 4, 1, 16),
            max_command_attempts=_integer("MAX_COMMAND_ATTEMPTS", 8, 1, 50),
            scan_interval_seconds=_integer("SCAN_INTERVAL_SECONDS", 60, 10, 3600),
            reconcile_interval_seconds=_integer("RECONCILE_INTERVAL_SECONDS", 300, 30, 86400),
            health_port=_integer("HEALTH_PORT", 8080, 1, 65535),
            lock_namespace=_integer("GRACE_LOCK_NAMESPACE", 1196573509, 1, 2147483647),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.mode == "disabled":
            if self.write_enabled or self.activation_percent:
                raise ValueError("disabled mode cannot enable mutations")
            return
        if not self.database_dsn:
            raise ValueError("DATABASE_DSN is required")
        if self.mode == "observe":
            if self.write_enabled or self.activation_percent:
                raise ValueError("observe mode must be read-only")
            return
        if not self.write_enabled:
            raise ValueError("GRACE_WRITE_ENABLED=true is required for canary/active")
        if not self.remnawave_api_url or not self.remnawave_api_key or self.grace_squad_uuid is None:
            raise ValueError("Remnawave API and GRACE_SQUAD_UUID are required in write modes")
        if self.mode == "canary":
            if self.canary_uuid is None or self.activation_percent != 0:
                raise ValueError("canary mode requires one UUID and ACTIVATION_PERCENT=0")
        if self.mode == "active":
            if not 1 <= self.activation_percent <= 100:
                raise ValueError("active mode requires ACTIVATION_PERCENT between 1 and 100")
            if os.getenv("GRACE_ALL_USERS_CONFIRM", "") != WRITE_CONFIRMATION:
                raise ValueError(f"active mode requires GRACE_ALL_USERS_CONFIRM={WRITE_CONFIRMATION}")

    @property
    def mutation_enabled(self) -> bool:
        return self.write_enabled and self.mode in {"canary", "active"}
