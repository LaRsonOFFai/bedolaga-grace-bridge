from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

from . import __version__
from .backup import create_backup, list_backups
from .config import BridgeConfig, Paths, bridge_home, parse_env_file
from .deploy import (
    compose,
    deploy_disabled,
    start_bridge,
    wait_service_ready,
    write_override,
    write_runtime,
)
from .detect import discover_bedolaga_dir, discover_compose_file
from .diagnostics import create_bundle
from .drain import drain_grace
from .patcher import prepare_candidate
from .preflight import run_preflight
from .rollback import safe_rollback
from .runner import run
from .schema import apply_schema
from .state import InstallationState

ACTIVE_CONFIRM = "ВКЛЮЧИТЬ GRACE ДЛЯ ПУЛА"
CANARY_CONFIRM = "CANARY ПРОВЕРЕН"


def _paths(args: argparse.Namespace) -> Paths:
    return Paths(Path(args.config_dir), Path(args.state_dir), Path(args.log_dir))


def _manifest() -> Path:
    return bridge_home() / "patches" / "bedolaga" / "compatibility.json"


def _print_checks(report) -> None:
    for check in report.checks:
        marker = "OK" if check.ok else ("BLOCK" if check.blocking else "WARN")
        print(f"[{marker:5}] {check.name}: {check.message}")
    print("\nРезультат:", "можно продолжать" if report.safe_to_install else "изменения запрещены")


def _config(paths: Paths) -> BridgeConfig:
    try:
        return BridgeConfig.load(paths)
    except Exception as error:
        raise SystemExit(f"Ошибка конфигурации: {error}") from error


def _atomic_env_update(path: Path, updates: dict[str, str]) -> None:
    current = parse_env_file(path)
    current.update(updates)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        "".join(f"{key}={value}\n" for key, value in sorted(current.items())), encoding="utf-8"
    )
    if os.name != "nt":
        temporary.chmod(0o600)
    os.replace(temporary, path)


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def _first(values: dict[str, str], *keys: str) -> str:
    for key in keys:
        if values.get(key):
            return values[key]
    return ""


def cmd_configure(args: argparse.Namespace) -> int:
    paths = _paths(args)
    if paths.config_file.exists() or paths.secrets_file.exists():
        if input("Конфигурация уже существует. Перезаписать её? [y/N] ").strip().lower() != "y":
            print("Ничего не изменено.")
            return 1
    detected_dir = discover_bedolaga_dir()
    default_dir = str(detected_dir or Path("/opt/bedolaga"))
    bedolaga_dir = Path(_ask("Каталог Bedolaga", default_dir)).resolve()
    compose_detected = discover_compose_file(bedolaga_dir)
    compose_file = Path(
        _ask("Docker Compose файл", str(compose_detected or bedolaga_dir / "docker-compose.yml"))
    )
    if not compose_file.is_absolute():
        compose_file = bedolaga_dir / compose_file

    source_env = parse_env_file(bedolaga_dir / ".env")
    services: list[str] = []
    if compose_file.exists():
        detected = run(
            ["docker", "compose", "-f", str(compose_file), "config", "--services"],
            cwd=bedolaga_dir,
            check=False,
        )
        services = [line.strip() for line in detected.stdout.splitlines() if line.strip()]
    bedolaga_default = next((item for item in services if "bedolaga" in item or "bot" in item), "bedolaga")
    database_default = next(
        (item for item in services if "postgres" in item or item in {"db", "database"}), "postgres"
    )
    bedolaga_service = _ask("Service Bedolaga", bedolaga_default)
    database_service = _ask("Service PostgreSQL", database_default)
    database_name = _ask(
        "Имя базы PostgreSQL", _first(source_env, "POSTGRES_DB", "DATABASE_NAME", "DB_NAME") or "bedolaga"
    )
    database_user = _ask(
        "Пользователь PostgreSQL",
        _first(source_env, "POSTGRES_USER", "DATABASE_USER", "DB_USER") or "postgres",
    )

    detected_dsn = _first(source_env, "DATABASE_DSN", "DATABASE_URL")
    if not detected_dsn:
        password = _first(source_env, "POSTGRES_PASSWORD", "DATABASE_PASSWORD", "DB_PASSWORD")
        if password:
            detected_dsn = f"postgresql://{database_user}:{password}@{database_service}:5432/{database_name}"
    if (
        detected_dsn
        and input("Найдены параметры БД в .env Bedolaga. Использовать их? [Y/n] ").strip().lower() != "n"
    ):
        database_dsn = detected_dsn
    else:
        database_dsn = getpass.getpass("DATABASE_DSN (ввод скрыт): ").strip()

    detected_url = _first(source_env, "REMNAWAVE_API_URL")
    remnawave_url = _ask("URL Remnawave API", detected_url)
    detected_key = _first(source_env, "REMNAWAVE_API_KEY")
    if (
        detected_key
        and input("Найден Remnawave API key в .env Bedolaga. Использовать его? [Y/n] ").strip().lower() != "n"
    ):
        remnawave_key = detected_key
    else:
        remnawave_key = getpass.getpass("Remnawave API key (ввод скрыт): ").strip()
    squad_uuid = _ask("UUID internal squad для Grace")
    duration = _ask("Срок Grace в днях", "7")
    traffic_gb = _ask("Лимит Grace в GiB", "1")
    try:
        traffic_bytes = int(float(traffic_gb) * 1024**3)
    except ValueError as error:
        raise SystemExit("Некорректный лимит трафика") from error

    public = {
        "BEDOLAGA_DIR": str(bedolaga_dir),
        "BEDOLAGA_COMPOSE_FILE": str(compose_file),
        "BEDOLAGA_SERVICE": bedolaga_service,
        "DATABASE_SERVICE": database_service,
        "DATABASE_NAME": database_name,
        "DATABASE_USER": database_user,
        "GRACE_SQUAD_UUID": squad_uuid,
        "GRACE_DURATION_DAYS": duration,
        "GRACE_TRAFFIC_LIMIT_BYTES": str(traffic_bytes),
        "CANDIDATE_BATCH_SIZE": "500",
        "COMMAND_WORKERS": "4",
        "MAX_COMMAND_ATTEMPTS": "8",
        "ACTIVATION_PERCENT": "0",
    }
    secrets = {
        "DATABASE_DSN": database_dsn,
        "REMNAWAVE_API_URL": remnawave_url,
        "REMNAWAVE_API_KEY": remnawave_key,
        "CANARY_REMNAWAVE_UUID": "",
    }
    _atomic_env_update(paths.config_file, public)
    _atomic_env_update(paths.secrets_file, secrets)
    print(f"Конфигурация сохранена в {paths.config_dir}. Секреты не выводились в терминал.")
    return 0


def cmd_preflight(args: argparse.Namespace) -> int:
    paths = _paths(args)
    report = run_preflight(_config(paths), paths, _manifest())
    _print_checks(report)
    return 0 if report.safe_to_install else 2


def cmd_status(args: argparse.Namespace) -> int:
    paths = _paths(args)
    state = InstallationState.load(paths.state_file)
    print(f"Bridge: {state.bridge_version}")
    print(f"Состояние: {state.phase}")
    print(f"Bedolaga: {state.bedolaga_version or 'не определена'}")
    print(f"Backup: {state.backup_id or 'нет'}")
    print(f"Активация: {state.activation_percent}%")
    if state.last_error:
        print(f"Последняя ошибка: {state.last_error}")
    return 0


def _build_bridge_image(home: Path) -> str:
    image = f"bedolaga-grace-bridge/controller:{__version__}"
    result = run(
        ["docker", "build", "-f", str(home / "deploy" / "Dockerfile"), "-t", image, "."],
        cwd=home,
        check=False,
        timeout=1800,
    )
    if not result.ok:
        raise RuntimeError(result.stderr.strip() or result.stdout[-4000:])
    return image


def cmd_install(args: argparse.Namespace) -> int:
    paths = _paths(args)
    config = _config(paths)
    report = run_preflight(config, paths, _manifest())
    _print_checks(report)
    if not report.safe_to_install or report.compatibility is None or report.compatibility.record is None:
        print("\nУстановка остановлена до любых изменений.")
        return 2
    if report.compatibility.record.status == "canary":
        print("\nВнимание: пакет совместимости v3.64.0 имеет статус canary.")
        print("Установка создаст только выключенный кандидат; массовый rollout потребует отдельных шагов.")
    if input("\nСоздать резервную копию и собрать выключенный кандидат? [y/N] ").strip().lower() != "y":
        print("Отменено.")
        return 1

    state = InstallationState.load(paths.state_file)
    backup = None
    try:
        record = report.compatibility.record
        backup = create_backup(config, paths, record)
        state.update(
            phase="backup_complete",
            backup_id=backup.backup_id,
            bedolaga_version=record.version,
            bedolaga_commit=record.commit,
            original_image=backup.original_image,
        ).save(paths.state_file)
        candidate = prepare_candidate(config, paths, record, bridge_home(), backup.backup_id)
        bridge_image = _build_bridge_image(bridge_home())
        apply_schema(config, bridge_home() / "schema")
        override = write_override(
            config,
            paths,
            bedolaga_image=candidate.image,
            bridge_image=bridge_image,
            integration_enabled=False,
        )
        deploy_disabled(config, paths, override)
        state.update(
            phase="installed_disabled",
            candidate_image=candidate.image,
            activation_percent=0,
            last_error=None,
        ).save(paths.state_file)
        print("\nУстановлено в выключенном режиме. Пользователи не изменялись.")
        print("Следующий шаг: sudo gracectl observe")
        return 0
    except Exception as error:
        state.update(phase="install_failed", last_error=str(error)).save(paths.state_file)
        print(f"\nОшибка установки: {error}", file=sys.stderr)
        if backup:
            print("Выполняется безопасный откат к созданной резервной копии.", file=sys.stderr)
            try:
                safe_rollback(config, paths, paths.backups_dir / backup.backup_id)
            except Exception as rollback_error:
                print(f"Автоматический откат требует внимания: {rollback_error}", file=sys.stderr)
        try:
            archive = create_bundle(config, paths)
            print(f"Обезличенный отчёт ошибки: {archive}", file=sys.stderr)
        except Exception as bundle_error:
            print(f"Не удалось создать диагностический архив: {bundle_error}", file=sys.stderr)
        print("Отправьте разработчику только обезличенный архив, но не .env и не database.dump.")
        return 3


def _override(paths: Paths) -> Path:
    path = paths.state_dir / "docker-compose.grace-bridge.override.yml"
    if not path.is_file():
        raise SystemExit("Bridge ещё не установлен")
    return path


def cmd_observe(args: argparse.Namespace) -> int:
    paths = _paths(args)
    config = _config(paths)
    state = InstallationState.load(paths.state_file)
    if state.phase not in {"installed_disabled", "observing", "canary_running", "canary_verified", "active"}:
        raise SystemExit("Сначала выполните gracectl install")
    write_runtime(paths, mode="observe", write_enabled=False, activation_percent=0)
    start_bridge(config, _override(paths))
    state.update(phase="observing", activation_percent=0).save(paths.state_file)
    print("Observe включён. Bridge только читает кандидатов и никого не изменяет.")
    return 0


def cmd_canary(args: argparse.Namespace) -> int:
    paths = _paths(args)
    config = _config(paths)
    state = InstallationState.load(paths.state_file)
    if state.phase not in {"observing", "canary_running", "canary_verified"}:
        raise SystemExit("Перед canary запустите observe")
    value = input("Введите Remnawave UUID тестового пользователя: ").strip()
    try:
        from uuid import UUID

        normalized = str(UUID(value))
    except ValueError as error:
        raise SystemExit("Некорректный UUID") from error
    _atomic_env_update(paths.secrets_file, {"CANARY_REMNAWAVE_UUID": normalized})
    write_runtime(paths, mode="canary", write_enabled=True, activation_percent=0)
    override = write_override(
        config,
        paths,
        bedolaga_image=state.candidate_image or "",
        bridge_image=f"bedolaga-grace-bridge/controller:{__version__}",
        integration_enabled=True,
    )
    compose(config, override, "up", "-d", "--no-deps", config.bedolaga_service)
    wait_service_ready(config, override, config.bedolaga_service)
    start_bridge(config, override)
    import hashlib

    state.update(
        phase="canary_running",
        canary_uuid_hash=hashlib.sha256(normalized.encode()).hexdigest()[:12],
    ).save(paths.state_file)
    print("Canary запущен только для одного UUID. Массовая активация заблокирована.")
    return 0


def cmd_approve_canary(args: argparse.Namespace) -> int:
    paths = _paths(args)
    state = InstallationState.load(paths.state_file)
    if state.phase != "canary_running":
        raise SystemExit("Нет запущенного canary")
    print(
        "Подтвердите: Grace включился, оплата восстановила обычный доступ, повторная синхронизация безопасна."
    )
    if input(f"Введите: {CANARY_CONFIRM}\n> ").strip() != CANARY_CONFIRM:
        print("Подтверждение не совпало. Ничего не изменено.")
        return 1
    state.update(phase="canary_verified").save(paths.state_file)
    print("Canary отмечен проверенным. Доступна ступень 5%.")
    return 0


def cmd_activate(args: argparse.Namespace) -> int:
    paths = _paths(args)
    config = _config(paths)
    state = InstallationState.load(paths.state_file)
    allowed_next = {0: 5, 5: 25, 25: 50, 50: 100}
    if state.phase not in {"canary_verified", "active"}:
        raise SystemExit("Массовая активация доступна только после approve-canary")
    expected = allowed_next.get(state.activation_percent)
    if expected is None:
        print("Уже включено 100% подходящего пула.")
        return 0
    requested = args.percent or expected
    if requested != expected:
        raise SystemExit(f"Следующая разрешённая ступень — только {expected}%")
    print(f"Будет включена ступень {requested}% подходящих пользователей.")
    if input(f"Введите: {ACTIVE_CONFIRM}\n> ").strip() != ACTIVE_CONFIRM:
        print("Подтверждение не совпало. Ничего не изменено.")
        return 1
    write_runtime(paths, mode="active", write_enabled=True, activation_percent=requested)
    start_bridge(config, _override(paths))
    state.update(phase="active", activation_percent=requested).save(paths.state_file)
    print(f"Активирована ступень {requested}%. Следующая ступень потребует отдельного запуска.")
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    paths = _paths(args)
    config = _config(paths)
    state = InstallationState.load(paths.state_file)
    if state.phase not in {"observing", "canary_running", "canary_verified", "active"}:
        raise SystemExit("Pause доступен только для запущенного Bridge")
    write_runtime(paths, mode="observe", write_enabled=False, activation_percent=0)
    start_bridge(config, _override(paths))
    state.update(
        phase="paused",
        paused_from_phase=state.phase,
        paused_activation_percent=state.activation_percent,
        activation_percent=0,
    ).save(paths.state_file)
    print("Новые Grace-активации остановлены.")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    paths = _paths(args)
    config = _config(paths)
    state = InstallationState.load(paths.state_file)
    if state.phase != "paused" or not state.paused_from_phase:
        raise SystemExit("Bridge не находится на паузе")
    previous_phase = state.paused_from_phase
    previous_percent = state.paused_activation_percent
    if previous_phase == "active":
        write_runtime(paths, mode="active", write_enabled=True, activation_percent=previous_percent)
    elif previous_phase in {"canary_running", "canary_verified"}:
        write_runtime(paths, mode="canary", write_enabled=True, activation_percent=0)
    else:
        previous_phase = "observing"
        previous_percent = 0
        write_runtime(paths, mode="observe", write_enabled=False, activation_percent=0)
    start_bridge(config, _override(paths))
    state.update(
        phase=previous_phase,
        activation_percent=previous_percent,
        paused_from_phase=None,
        paused_activation_percent=0,
    ).save(paths.state_file)
    print(f"Bridge продолжил работу из состояния {previous_phase}.")
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    paths = _paths(args)
    config = _config(paths)
    backups = list_backups(paths)
    if not backups:
        raise SystemExit("Завершённые резервные копии не найдены")
    selected = backups[0]
    print(f"Будет использована резервная копия {selected.name}.")
    print("Bridge сначала попытается снять только те overlay, которыми всё ещё владеет.")
    if input("Продолжить безопасный откат? [y/N] ").strip().lower() != "y":
        return 1
    try:
        drained = asyncio.run(drain_grace(config))
        print(
            f"Grace overlay: восстановлено {drained.restored}, оплаченных пропущено {drained.skipped_paid}, "
            f"внешних изменений пропущено {drained.skipped_external_change}, ошибок {drained.failed}."
        )
    except Exception as error:
        print(f"Не удалось полностью снять overlay: {error}", file=sys.stderr)
        if input("Продолжить откат кода без восстановления пользователей? [y/N] ").strip().lower() != "y":
            return 3
    result = safe_rollback(config, paths, selected)
    print(
        f"Откат {result.backup_id}: файлов {result.restored_files}; "
        f"Bedolaga {'работает' if result.bedolaga_running else 'требует проверки'}."
    )
    return 0 if result.bedolaga_running else 4


def cmd_bundle(args: argparse.Namespace) -> int:
    paths = _paths(args)
    archive = create_bundle(_config(paths), paths)
    print(f"Обезличенный диагностический архив: {archive}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gracectl", description="Bedolaga Grace Bridge")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config-dir", default="/etc/bedolaga-grace-bridge")
    parser.add_argument("--state-dir", default="/var/lib/bedolaga-grace-bridge")
    parser.add_argument("--log-dir", default="/var/log/bedolaga-grace-bridge")
    sub = parser.add_subparsers(dest="command", required=True)
    commands = {
        "configure": cmd_configure,
        "preflight": cmd_preflight,
        "status": cmd_status,
        "install": cmd_install,
        "observe": cmd_observe,
        "canary": cmd_canary,
        "approve-canary": cmd_approve_canary,
        "activate": cmd_activate,
        "pause": cmd_pause,
        "resume": cmd_resume,
        "rollback": cmd_rollback,
        "bundle": cmd_bundle,
    }
    for name, handler in commands.items():
        command = sub.add_parser(name)
        command.set_defaults(handler=handler)
        if name == "activate":
            command.add_argument("--percent", type=int, choices=(5, 25, 50, 100))
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    raise SystemExit(args.handler(args))


if __name__ == "__main__":
    main()
