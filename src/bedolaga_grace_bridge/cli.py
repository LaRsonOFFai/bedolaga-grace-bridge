from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

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
from .panel_admin import PanelAdminClient, PanelApiError, PanelInbound, PanelSquad
from .panel_discovery import (
    PanelCandidate,
    candidate_from_bedolaga_env,
    discover_local_panels,
    merge_candidates,
    normalize_panel_url,
)
from .patcher import prepare_candidate
from .preflight import run_preflight
from .rollback import safe_rollback
from .runner import run
from .schema import apply_schema
from .state import InstallationState

ACTIVE_CONFIRM = "ВКЛЮЧИТЬ GRACE ДЛЯ ПУЛА"
CANARY_CONFIRM = "CANARY ПРОВЕРЕН"
ROLLOUT_STEPS = (5, 25, 50, 100)


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


def _confirm(prompt: str, *, default: bool = False) -> bool:
    marker = "Y/n" if default else "y/N"
    answer = input(f"{prompt} [{marker}] ").strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes", "д", "да"}


def _choice(prompt: str, choices: dict[str, str], *, default: str) -> str:
    while True:
        print(prompt)
        for key, label in choices.items():
            suffix = " (по умолчанию)" if key == default else ""
            print(f"  {key}. {label}{suffix}")
        answer = input("Выберите действие: ").strip() or default
        if answer in choices:
            return answer
        print("Введите номер из списка.")


def _panel_label(candidate: PanelCandidate) -> str:
    suffix = f", контейнер {candidate.container_name}" if candidate.container_name else ""
    return f"{candidate.url} ({candidate.source}{suffix})"


def _confirm_panel_url(url: str, *, local: bool) -> bool:
    parsed = urlsplit(url)
    loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme == "http" and not local and not loopback:
        print("Внимание: удалённый HTTP не шифрует API-ключ Remnawave.")
        if not _confirm("Всё равно использовать этот HTTP-адрес?", default=False):
            return False
    return _confirm(f"Использовать {url}?", default=True)


def _choose_panel_url(source_env: dict[str, str]) -> str:
    from_bedolaga = candidate_from_bedolaga_env(source_env)
    candidates = merge_candidates(
        [from_bedolaga] if from_bedolaga else [],
        discover_local_panels(),
    )
    if candidates:
        print("\nОбнаружены возможные панели Remnawave:")
        for number, candidate in enumerate(candidates, 1):
            print(f"  {number}. {_panel_label(candidate)}")
        print("  0. Указать другой адрес")
        while True:
            raw = input("Выберите панель [1]: ").strip() or "1"
            if raw == "0":
                break
            if raw.isdigit() and 1 <= int(raw) <= len(candidates):
                selected = candidates[int(raw) - 1]
                if _confirm_panel_url(selected.url, local=selected.local):
                    return selected.url
                break
            print("Введите номер из списка.")
    else:
        print("\nЛокальная панель и адрес в Bedolaga не обнаружены.")
    while True:
        raw = _ask("Корневой URL панели Remnawave (например, https://panel.example.com)")
        try:
            selected = normalize_panel_url(raw)
        except ValueError as error:
            print(f"Ошибка: {error}")
            continue
        if not selected:
            print("Укажите адрес панели.")
            continue
        if _confirm_panel_url(selected, local=False):
            return selected


def _select_numbers(prompt: str, maximum: int) -> list[int]:
    while True:
        raw = input(prompt).strip()
        try:
            selected = sorted({int(item.strip()) for item in raw.split(",") if item.strip()})
        except ValueError:
            selected = []
        if selected and all(1 <= number <= maximum for number in selected):
            return selected
        print("Укажите один или несколько номеров через запятую.")


def _show_inbound(number: int, inbound: PanelInbound) -> None:
    details = "/".join(item for item in (inbound.protocol, inbound.network, inbound.security) if item)
    port = f":{inbound.port}" if inbound.port is not None else ""
    print(f"  {number}. {inbound.tag}{port} — {details or 'без описания'}")


def _manual_squad_uuid(default: str = "") -> str:
    from uuid import UUID

    while True:
        value = _ask("UUID internal squad для Continuity", default)
        try:
            return str(UUID(value))
        except ValueError:
            print("Некорректный UUID.")


def _select_or_create_squad(
    client: PanelAdminClient,
) -> tuple[PanelSquad, bool]:
    squads = client.list_internal_squads()
    print("\nНастройка Continuity internal squad:")
    if squads:
        print("  1. Выбрать существующий squad")
        print("  2. Создать отдельный squad автоматически")
        print("  3. Ввести UUID вручную")
        while True:
            choice = input("Выберите действие [1]: ").strip() or "1"
            if choice in {"1", "2", "3"}:
                break
            print("Введите 1, 2 или 3.")
    else:
        print("Существующие internal squads не найдены.")
        print("  1. Создать отдельный squad автоматически")
        print("  2. Ввести UUID вручную")
        while True:
            raw = input("Выберите действие [1]: ").strip() or "1"
            if raw in {"1", "2"}:
                break
            print("Введите 1 или 2.")
        choice = "2" if raw == "1" else "3"

    if choice == "1" and squads:
        for number, squad in enumerate(squads, 1):
            print(f"  {number}. {squad.name} ({len(squad.inbound_uuids)} inbound)")
        number = _select_numbers("Номер squad: ", len(squads))[0]
        return squads[number - 1], False

    if choice == "2":
        inbounds = client.list_inbounds()
        if not inbounds:
            raise PanelApiError("В панели нет доступных inbound для нового internal squad")
        print("\nВыберите inbound ограниченного доступа:")
        for number, inbound in enumerate(inbounds, 1):
            _show_inbound(number, inbound)
        selected = _select_numbers("Номера inbound через запятую: ", len(inbounds))
        selected_inbounds = [inbounds[number - 1] for number in selected]
        name = _ask("Название нового squad", "Continuity Access")
        if not 2 <= len(name) <= 30 or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_ -"
            for character in name
        ):
            raise PanelApiError("Название squad должно содержать 2–30 латинских символов, цифр, _ или -")
        print("\nБудет создан internal squad:")
        print(f"  Название: {name}")
        print("  Inbound: " + ", ".join(item.tag for item in selected_inbounds))
        if not _confirm("Создать этот объект в Remnawave?", default=False):
            raise PanelApiError("Создание internal squad отменено")
        created = client.create_internal_squad(name, [item.uuid for item in selected_inbounds])
        print(f"Internal squad создан: {created.name}")
        return created, True

    return PanelSquad(_manual_squad_uuid(), "Введён вручную", ()), False


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

    remnawave_url = _choose_panel_url(source_env)
    detected_key = _first(source_env, "REMNAWAVE_API_KEY")
    if (
        detected_key
        and input("Найден Remnawave API key в .env Bedolaga. Использовать его? [Y/n] ").strip().lower() != "n"
    ):
        remnawave_key = detected_key
    else:
        remnawave_key = getpass.getpass("Remnawave API key (ввод скрыт): ").strip()
    if not remnawave_key:
        raise SystemExit("Remnawave API key не указан")

    duration = _ask("Срок ограниченного доступа в днях", "7")
    traffic_gb = _ask("Лимит ограниченного доступа в GiB", "1")
    try:
        duration_days = int(duration)
        traffic_bytes = int(float(traffic_gb) * 1024**3)
    except ValueError as error:
        raise SystemExit("Срок и лимит трафика должны быть числами") from error
    if duration_days < 1 or traffic_bytes < 1:
        raise SystemExit("Срок и лимит трафика должны быть больше нуля")

    client = PanelAdminClient(remnawave_url, remnawave_key)
    created_squad: PanelSquad | None = None
    try:
        client.health()
        print("Подключение к Remnawave проверено.")
        squad, created = _select_or_create_squad(client)
        if created:
            created_squad = squad
    except PanelApiError as error:
        print(f"Не удалось завершить автоматическую настройку: {error}")
        if not _confirm("Продолжить и указать UUID internal squad вручную?", default=False):
            raise SystemExit("Настройка остановлена без изменения локальной конфигурации") from error
        squad = PanelSquad(_manual_squad_uuid(), "Введён вручную", ())

    public = {
        "BEDOLAGA_DIR": str(bedolaga_dir),
        "BEDOLAGA_COMPOSE_FILE": str(compose_file),
        "BEDOLAGA_SERVICE": bedolaga_service,
        "DATABASE_SERVICE": database_service,
        "DATABASE_NAME": database_name,
        "DATABASE_USER": database_user,
        "GRACE_SQUAD_UUID": squad.uuid,
        "GRACE_DURATION_DAYS": str(duration_days),
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
    print("\nИтоговая конфигурация:")
    print(f"  Bedolaga: {bedolaga_dir}")
    print(f"  Remnawave: {remnawave_url}")
    print(f"  Internal squad: {squad.name} ({squad.uuid})")
    print(f"  Ограниченный доступ: {duration_days} дн., {traffic_gb} GiB")
    print("  API key и пароль базы скрыты")
    if not _confirm("Сохранить эту конфигурацию?", default=True):
        if created_squad:
            try:
                client.delete_internal_squad(created_squad.uuid)
                print("Созданный internal squad удалён, так как настройка отменена.")
            except PanelApiError as cleanup_error:
                print(f"Не удалось удалить созданный internal squad: {cleanup_error}", file=sys.stderr)
        print("Локальная конфигурация не изменена.")
        return 1

    previous_config = paths.config_file.read_bytes() if paths.config_file.exists() else None
    previous_secrets = paths.secrets_file.read_bytes() if paths.secrets_file.exists() else None
    try:
        _atomic_env_update(paths.config_file, public)
        _atomic_env_update(paths.secrets_file, secrets)
        state = InstallationState.load(paths.state_file)
        state.update(
            bridge_version=__version__,
            managed_squad_uuid=created_squad.uuid if created_squad else None,
        ).save(paths.state_file)
    except Exception:
        for path, previous in (
            (paths.config_file, previous_config),
            (paths.secrets_file, previous_secrets),
        ):
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(previous)
        if created_squad:
            try:
                client.delete_internal_squad(created_squad.uuid)
            except PanelApiError as cleanup_error:
                print(f"Не удалось удалить созданный internal squad: {cleanup_error}", file=sys.stderr)
        raise
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
    if getattr(args, "guided", False):
        if not _confirm("Создать резервную копию и собрать выключенный кандидат?", default=True):
            print("Отменено.")
            return 1
    elif input("\nСоздать резервную копию и собрать выключенный кандидат? [y/N] ").strip().lower() != "y":
        print("Отменено.")
        return 1

    state = InstallationState.load(paths.state_file)
    backup = None
    try:
        record = report.compatibility.record
        backup = create_backup(config, paths, record)
        state.update(
            phase="backup_complete",
            bridge_version=__version__,
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
    if getattr(args, "guided", False):
        if not _confirm("Все проверки тестового пользователя действительно пройдены?", default=False):
            print("Тест пока не подтверждён. Canary продолжает работать только для одного UUID.")
            return 1
    elif input(f"Введите: {CANARY_CONFIRM}\n> ").strip() != CANARY_CONFIRM:
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
    if getattr(args, "guided", False):
        if not _confirm(f"Включить ступень {requested}%?", default=False):
            print("Ступень не изменена.")
            return 1
    elif input(f"Введите: {ACTIVE_CONFIRM}\n> ").strip() != ACTIVE_CONFIRM:
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
    if getattr(args, "guided", False):
        if not _confirm("Продолжить безопасный откат?", default=False):
            return 1
    elif input("Продолжить безопасный откат? [y/N] ").strip().lower() != "y":
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


def _menu_args(args: argparse.Namespace, **overrides: object) -> argparse.Namespace:
    values = vars(args).copy()
    values.update(overrides)
    return argparse.Namespace(**values)


def _wizard_call(
    args: argparse.Namespace,
    title: str,
    handler,
    **overrides: object,
) -> int:
    print("\n" + "-" * 62)
    print(title)
    print("-" * 62)
    try:
        result = handler(_menu_args(args, guided=True, **overrides))
    except KeyboardInterrupt:
        print("\nДействие отменено.")
        return 1
    except SystemExit as error:
        if error.code in {None, 0}:
            return 0
        print(f"Шаг не выполнен: {error}")
        return error.code if isinstance(error.code, int) else 2
    except Exception as error:
        print(f"Шаг завершился ошибкой: {error}", file=sys.stderr)
        return 3
    return int(result or 0)


def _wizard_offer_rollback(args: argparse.Namespace) -> int:
    paths = _paths(args)
    if not list_backups(paths):
        print("Завершённой резервной копии пока нет; рабочая Bedolaga не изменялась.")
        return 2
    return _wizard_call(args, "Безопасный откат", cmd_rollback)


def _wizard_stop_or_recover(args: argparse.Namespace, *, allow_continue: bool) -> str:
    choices = {
        "1": "Продолжить к следующей ступени",
        "2": "Остановить мастер и оставить текущую ступень",
        "3": "Поставить новые активации на паузу",
        "4": "Выполнить безопасный откат",
    }
    if not allow_continue:
        choices.pop("1")
    selected = _choice("Что сделать дальше?", choices, default="2")
    if selected == "3":
        _wizard_call(args, "Пауза новых активаций", cmd_pause)
    elif selected == "4":
        _wizard_offer_rollback(args)
    return selected


def cmd_wizard(args: argparse.Namespace) -> int:
    paths = _paths(args)
    state = InstallationState.load(paths.state_file)
    print("\n" + "=" * 62)
    print("Bedolaga Continuity — автоматический мастер")
    print("=" * 62)
    print("Мастер сам выполнит настройку, проверку, резервное копирование и запуск.")
    print("На безопасных контрольных точках он попросит только проверить результат.")
    print("Отдельные команды и расширенное меню останутся доступны.")

    if state.phase == "active" and state.activation_percent >= 100:
        print("\nContinuity уже включён для 100% подходящих пользователей.")
        return cmd_status(args)
    if state.phase == "rollback_needs_attention":
        print("\nПредыдущий откат требует внимания. Запустите: sudo gracebridge-rescue")
        return 4
    if state.phase == "paused":
        selected = _choice(
            "Система сейчас находится на паузе.",
            {
                "1": "Продолжить работу с сохранённого места",
                "2": "Оставить на паузе и выйти",
                "3": "Выполнить безопасный откат",
            },
            default="2",
        )
        if selected == "2":
            return 0
        if selected == "3":
            return _wizard_offer_rollback(args)
        if _wizard_call(args, "Возобновление работы", cmd_resume):
            return 3
        state = InstallationState.load(paths.state_file)

    configured = paths.config_file.is_file() and paths.secrets_file.is_file()
    needs_install = state.phase in {
        "absent",
        "backup_complete",
        "install_failed",
        "rolled_back",
    }
    if needs_install:
        if configured:
            print(f"\nНайдена сохранённая конфигурация: {paths.config_dir}")
            if _confirm("Изменить её перед установкой?", default=False):
                if _wizard_call(args, "Настройка подключения", cmd_configure):
                    return 1
        elif _wizard_call(args, "Настройка подключения", cmd_configure):
            return 1

        if _wizard_call(
            args,
            "Проверка совместимости, резервная копия и установка в выключенном режиме",
            cmd_install,
        ):
            return _wizard_offer_rollback(args)
        state = InstallationState.load(paths.state_file)

    if state.phase == "installed_disabled":
        if _wizard_call(args, "Наблюдение без изменения пользователей", cmd_observe):
            return _wizard_offer_rollback(args)
        state = InstallationState.load(paths.state_file)

    if state.phase == "observing":
        print("\nСледующий этап изменит только одного указанного тестового пользователя.")
        if not _confirm("Запустить безопасный тест на одном UUID сейчас?", default=True):
            print("Observe оставлен включённым. Позже снова запустите: sudo gracectl wizard")
            return 0
        if _wizard_call(args, "Тест на одном пользователе", cmd_canary):
            return _wizard_offer_rollback(args)
        state = InstallationState.load(paths.state_file)

    if state.phase == "canary_running":
        print("\nПроверьте тестового пользователя:")
        print("  • после окончания подписки включился ограниченный доступ;")
        print("  • доступны Telegram, Mini App, DNS и страница оплаты;")
        print("  • после оплаты вернулся обычный профиль;")
        print("  • повторная синхронизация Bedolaga не вернула Grace ошибочно.")
        selected = _choice(
            "Результат теста:",
            {
                "1": "Все проверки пройдены — продолжить",
                "2": "Нужно больше времени — оставить canary и выйти",
                "3": "Тест не пройден — перейти к восстановлению",
            },
            default="2",
        )
        if selected == "2":
            print("Canary остаётся только на одном UUID. Запустите мастер позже.")
            return 0
        if selected == "3":
            return _wizard_offer_rollback(args)
        if _wizard_call(args, "Подтверждение успешного теста", cmd_approve_canary):
            return 1
        state = InstallationState.load(paths.state_file)

    if state.phase not in {"canary_verified", "active"}:
        print(f"\nМастер не может автоматически продолжить из состояния: {state.phase}")
        print("Откройте расширенное меню или создайте диагностический архив.")
        return 2

    print("\nМассовое включение выполняется безопасными ступенями 5 → 25 → 50 → 100%.")
    print("Мастер сам применяет каждую ступень; после неё можно проверить сервис, остановиться или откатить.")
    if not _confirm("Начать или продолжить постепенное включение?", default=False):
        print("Текущий безопасный этап сохранён. Запустите мастер позже.")
        return 0

    while state.activation_percent < 100:
        next_percent = next(step for step in ROLLOUT_STEPS if step > state.activation_percent)
        if _wizard_call(
            args,
            f"Включение для {next_percent}% подходящих пользователей",
            cmd_activate,
            percent=next_percent,
        ):
            return _wizard_offer_rollback(args)
        state = InstallationState.load(paths.state_file)
        if state.activation_percent >= 100:
            break
        print(f"\nСтупень {state.activation_percent}% активна.")
        print("Проверьте ошибки Bedolaga, Remnawave, оплату и обращения пользователей.")
        selected = _wizard_stop_or_recover(args, allow_continue=True)
        if selected != "1":
            return 0

    print("\nГотово: Continuity включён для 100% подходящих пользователей.")
    print("Безопасный откат и отдельные команды по-прежнему доступны в меню.")
    return 0


def cmd_advanced_menu(args: argparse.Namespace) -> int:
    while True:
        paths = _paths(args)
        try:
            state = InstallationState.load(paths.state_file)
            phase = state.phase
        except Exception as error:
            phase = f"ошибка состояния: {error}"

        pause_label = "Продолжить работу" if phase == "paused" else "Поставить на паузу"
        print("\n" + "=" * 62)
        print("Bedolaga Continuity — расширенное управление")
        print(f"Текущее состояние: {phase}")
        print("=" * 62)
        print("  1. Настроить подключение и ограниченный доступ")
        print("  2. Проверить совместимость (без изменений)")
        print("  3. Создать бэкап и установить в выключенном режиме")
        print("  4. Запустить наблюдение без изменений пользователей")
        print("  5. Запустить тест на одном пользователе")
        print("  6. Подтвердить успешный тест")
        print("  7. Перейти на следующую ступень включения")
        print("  8. Показать состояние")
        print(f"  9. {pause_label}")
        print(" 10. Создать обезличенный диагностический архив")
        print(" 11. Безопасный откат")
        print("  0. Выход")
        choice = input("Выберите действие: ").strip()
        if choice == "0":
            return 0

        actions = {
            "1": (cmd_configure, {}),
            "2": (cmd_preflight, {}),
            "3": (cmd_install, {}),
            "4": (cmd_observe, {}),
            "5": (cmd_canary, {}),
            "6": (cmd_approve_canary, {}),
            "7": (cmd_activate, {"percent": None}),
            "8": (cmd_status, {}),
            "9": (cmd_resume if phase == "paused" else cmd_pause, {}),
            "10": (cmd_bundle, {}),
            "11": (cmd_rollback, {}),
        }
        selected = actions.get(choice)
        if selected is None:
            print("Неизвестный пункт меню.")
            continue
        handler, overrides = selected
        try:
            handler(_menu_args(args, **overrides))
        except KeyboardInterrupt:
            print("\nДействие отменено.")
        except SystemExit as error:
            if error.code not in {None, 0}:
                print(f"Действие не выполнено: {error}")
        except Exception as error:
            print(f"Действие завершилось ошибкой: {error}", file=sys.stderr)
        if sys.stdin.isatty():
            input("\nНажмите Enter, чтобы вернуться в меню...")


def cmd_menu(args: argparse.Namespace) -> int:
    while True:
        paths = _paths(args)
        try:
            state = InstallationState.load(paths.state_file)
            phase = state.phase
        except Exception as error:
            phase = f"ошибка состояния: {error}"
        pause_label = "Продолжить работу" if phase == "paused" else "Поставить на паузу"
        print("\n" + "=" * 62)
        print("Bedolaga Continuity")
        print(f"Текущее состояние: {phase}")
        print("=" * 62)
        print("  1. Автоматическая настройка и запуск (рекомендуется)")
        print("  2. Показать состояние")
        print(f"  3. {pause_label}")
        print("  4. Безопасный откат")
        print("  5. Расширенное управление и отдельные этапы")
        print("  0. Выход")
        choice = input("Выберите действие [1]: ").strip() or "1"
        if choice == "0":
            return 0
        actions = {
            "1": cmd_wizard,
            "2": cmd_status,
            "3": cmd_resume if phase == "paused" else cmd_pause,
            "4": cmd_rollback,
            "5": cmd_advanced_menu,
        }
        handler = actions.get(choice)
        if handler is None:
            print("Неизвестный пункт меню.")
            continue
        try:
            handler(_menu_args(args, guided=choice == "1"))
        except KeyboardInterrupt:
            print("\nДействие отменено.")
        except SystemExit as error:
            if error.code not in {None, 0}:
                print(f"Действие не выполнено: {error}")
        except Exception as error:
            print(f"Действие завершилось ошибкой: {error}", file=sys.stderr)
        if sys.stdin.isatty():
            input("\nНажмите Enter, чтобы вернуться в меню...")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gracectl", description="Bedolaga Continuity")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--config-dir", default="/etc/bedolaga-grace-bridge")
    parser.add_argument("--state-dir", default="/var/lib/bedolaga-grace-bridge")
    parser.add_argument("--log-dir", default="/var/log/bedolaga-grace-bridge")
    parser.set_defaults(handler=cmd_menu)
    sub = parser.add_subparsers(dest="command")
    commands = {
        "menu": cmd_menu,
        "wizard": cmd_wizard,
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
