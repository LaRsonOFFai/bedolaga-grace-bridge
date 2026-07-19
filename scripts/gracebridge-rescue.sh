#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

STATE_DIR="${GRACE_BRIDGE_STATE_DIR:-/var/lib/bedolaga-grace-bridge}"
CONFIG_DIR="${GRACE_BRIDGE_CONFIG_DIR:-/etc/bedolaga-grace-bridge}"
CURRENT="${GRACE_BRIDGE_HOME:-/opt/bedolaga-grace-bridge/current}"

if [[ ${EUID} -ne 0 ]]; then
  echo "Запустите аварийное восстановление через sudo." >&2
  exit 1
fi

echo "Bedolaga Grace Bridge — аварийное восстановление"
echo "Полный дамп PostgreSQL автоматически восстанавливаться не будет."

if [[ -x "${CURRENT}/.venv/bin/gracectl" ]]; then
  exec "${CURRENT}/.venv/bin/gracectl" --config-dir "${CONFIG_DIR}" --state-dir "${STATE_DIR}" rollback
fi

latest="$(find "${STATE_DIR}/backups" -mindepth 1 -maxdepth 1 -type d -name '20*' 2>/dev/null | sort -r | head -n1 || true)"
if [[ -z "${latest}" || ! -f "${latest}/manifest.json" ]]; then
  echo "Завершённая резервная копия не найдена." >&2
  exit 2
fi

RESCUE_PY="/usr/local/lib/bedolaga-grace-bridge/rescue_standalone.py"
if [[ ! -f "${RESCUE_PY}" ]]; then
  echo "Основная CLI и автономный rescue-модуль недоступны." >&2
  echo "Резервная копия сохранена в: ${latest}" >&2
  exit 3
fi
exec python3 "${RESCUE_PY}" --config-dir "${CONFIG_DIR}" --state-dir "${STATE_DIR}"
