from __future__ import annotations

import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .compatibility import CompatibilityResult, verify_compatibility
from .config import BridgeConfig, Paths, assert_secret_permissions
from .runner import CommandError, run


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    blocking: bool
    message: str


@dataclass(frozen=True, slots=True)
class PreflightReport:
    checks: tuple[Check, ...]
    compatibility: CompatibilityResult | None

    @property
    def safe_to_install(self) -> bool:
        return all(check.ok or not check.blocking for check in self.checks)


def _tool(name: str) -> Check:
    found = shutil.which(name)
    return Check(f"tool:{name}", found is not None, True, found or f"Не найдена команда {name}")


def _remnawave_get(config: BridgeConfig, endpoint: str) -> tuple[bool, str]:
    url = f"{config.remnawave_api_url}{endpoint}"
    if urllib.parse.urlsplit(url).scheme not in {"http", "https"}:
        return False, "Разрешены только URL с протоколом HTTP или HTTPS"
    request = urllib.request.Request(  # noqa: S310 - scheme is restricted immediately above
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {config.remnawave_api_key}",
            "X-Api-Key": config.remnawave_api_key,
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310 - URL is explicit admin config
            return 200 <= response.status < 300, f"HTTP {response.status}"
    except urllib.error.HTTPError as error:
        return False, f"HTTP {error.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return False, f"Соединение не установлено: {error.reason if hasattr(error, 'reason') else error}"


def run_preflight(config: BridgeConfig, paths: Paths, manifest_path: Path) -> PreflightReport:
    checks: list[Check] = []
    linux = os.name == "posix"
    checks.append(Check("platform", linux, True, "Linux" if linux else "Поддерживается только Linux"))
    if linux:
        is_root = os.geteuid() == 0
        checks.append(Check("root", is_root, True, "root" if is_root else "Требуется sudo/root"))
    checks.extend(_tool(name) for name in ("docker", "git"))

    try:
        assert_secret_permissions(paths.secrets_file)
        checks.append(Check("secret-permissions", True, True, "Права на secrets.env безопасны"))
    except PermissionError as error:
        checks.append(Check("secret-permissions", False, True, str(error)))

    checks.append(
        Check(
            "bedolaga-dir",
            config.bedolaga_dir.is_dir(),
            True,
            str(config.bedolaga_dir),
        )
    )
    checks.append(
        Check(
            "compose-file",
            config.compose_file.is_file(),
            True,
            str(config.compose_file),
        )
    )

    compatibility: CompatibilityResult | None = None
    if config.bedolaga_dir.is_dir() and manifest_path.is_file():
        compatibility = verify_compatibility(config.bedolaga_dir, manifest_path)
        checks.append(Check("compatibility", compatibility.compatible, True, compatibility.reason))
    else:
        checks.append(Check("compatibility", False, True, "Матрица совместимости недоступна"))

    if config.compose_file.is_file() and shutil.which("docker"):
        try:
            result = run(
                ["docker", "compose", "-f", str(config.compose_file), "config", "--services"],
                cwd=config.bedolaga_dir,
                timeout=60,
            )
            services = {line.strip() for line in result.stdout.splitlines() if line.strip()}
            for label, service in (
                ("bedolaga-service", config.bedolaga_service),
                ("database-service", config.database_service),
            ):
                checks.append(
                    Check(
                        label,
                        service in services,
                        True,
                        service if service in services else f"Service {service} не найден",
                    )
                )
        except CommandError as error:
            checks.append(Check("compose-parse", False, True, str(error)))

        database = run(
            [
                "docker",
                "compose",
                "-f",
                str(config.compose_file),
                "exec",
                "-T",
                config.database_service,
                "psql",
                "--username",
                config.database_user,
                "--dbname",
                config.database_name,
                "--tuples-only",
                "--no-align",
                "--command",
                "SELECT to_regclass('public.users'),to_regclass('public.subscriptions'),"
                "to_regclass('public.transactions');",
            ],
            cwd=config.bedolaga_dir,
            check=False,
            timeout=30,
        )
        database_ok = database.ok and "users|subscriptions|transactions" in database.stdout
        checks.append(
            Check(
                "database-readonly",
                database_ok,
                True,
                "Основные таблицы Bedolaga доступны"
                if database_ok
                else (database.stderr.strip() or "Не найдены основные таблицы Bedolaga"),
            )
        )

    free = shutil.disk_usage(config.bedolaga_dir if config.bedolaga_dir.exists() else Path("/"))
    minimum = 2 * 1024**3
    checks.append(
        Check(
            "disk-space",
            free.free >= minimum,
            True,
            f"Свободно {free.free / 1024**3:.1f} GiB; требуется минимум 2 GiB",
        )
    )

    checks.append(
        Check(
            "database-dsn",
            bool(config.database_dsn),
            True,
            "DATABASE_DSN задан" if config.database_dsn else "DATABASE_DSN не задан",
        )
    )
    checks.append(
        Check(
            "remnawave-url",
            bool(config.remnawave_api_url),
            True,
            config.remnawave_api_url or "REMNAWAVE_API_URL не задан",
        )
    )
    checks.append(
        Check(
            "remnawave-key",
            bool(config.remnawave_api_key),
            True,
            "API-ключ задан" if config.remnawave_api_key else "REMNAWAVE_API_KEY не задан",
        )
    )
    checks.append(
        Check(
            "grace-squad",
            bool(config.grace_squad_uuid),
            True,
            "Grace squad задан" if config.grace_squad_uuid else "GRACE_SQUAD_UUID не задан",
        )
    )
    if config.remnawave_api_url and config.remnawave_api_key:
        health_ok, health_message = _remnawave_get(config, "/api/system/health")
        checks.append(Check("remnawave-health", health_ok, True, health_message))
        if config.grace_squad_uuid:
            squad_ok, squad_message = _remnawave_get(
                config, f"/api/internal-squads/{config.grace_squad_uuid}"
            )
            checks.append(Check("grace-squad-readonly", squad_ok, True, squad_message))
    return PreflightReport(tuple(checks), compatibility)
