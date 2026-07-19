#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

VERSION="${BEDOLAGA_GRACE_BRIDGE_VERSION:-0.2.0}"
REPOSITORY="${BEDOLAGA_GRACE_BRIDGE_REPOSITORY:-LaRsonOFFai/bedolaga-grace-bridge}"
PREFIX="${BEDOLAGA_GRACE_BRIDGE_PREFIX:-/opt/bedolaga-grace-bridge}"
RELEASE_DIR="${PREFIX}/releases/${VERSION}"
CURRENT_LINK="${PREFIX}/current"

die() {
  echo "Ошибка: $*" >&2
  exit 1
}

[[ "$(uname -s)" == "Linux" ]] || die "поддерживается только Linux"
[[ ${EUID} -eq 0 ]] || die "запустите установщик через sudo"
command -v python3 >/dev/null || die "не найден python3"
command -v docker >/dev/null || die "не найден Docker"
docker compose version >/dev/null 2>&1 || die "не найден Docker Compose v2"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SOURCE_DIR=""
TEMP_DIR=""
cleanup() {
  [[ -z "${TEMP_DIR}" ]] || rm -rf -- "${TEMP_DIR}"
}
trap cleanup EXIT

if [[ -f "${SCRIPT_DIR}/pyproject.toml" && -d "${SCRIPT_DIR}/src" ]]; then
  SOURCE_DIR="${SCRIPT_DIR}"
else
  command -v curl >/dev/null || die "не найден curl"
  command -v sha256sum >/dev/null || die "не найден sha256sum"
  TEMP_DIR="$(mktemp -d)"
  ASSET="bedolaga-grace-bridge-${VERSION}.tar.gz"
  BASE="https://github.com/${REPOSITORY}/releases/download/v${VERSION}"
  curl --fail --show-error --location "${BASE}/${ASSET}" --output "${TEMP_DIR}/${ASSET}"
  curl --fail --show-error --location "${BASE}/${ASSET}.sha256" --output "${TEMP_DIR}/${ASSET}.sha256"
  (cd "${TEMP_DIR}" && sha256sum --check "${ASSET}.sha256") || die "контрольная сумма релиза не совпала"
  mkdir -p "${TEMP_DIR}/source"
  tar -xzf "${TEMP_DIR}/${ASSET}" -C "${TEMP_DIR}/source" --strip-components=1
  SOURCE_DIR="${TEMP_DIR}/source"
fi

[[ -f "${SOURCE_DIR}/pyproject.toml" ]] || die "дистрибутив повреждён"
mkdir -p "${RELEASE_DIR}" /etc/bedolaga-grace-bridge /var/lib/bedolaga-grace-bridge /var/log/bedolaga-grace-bridge
chmod 700 /etc/bedolaga-grace-bridge /var/lib/bedolaga-grace-bridge /var/log/bedolaga-grace-bridge

if [[ ! -f "${RELEASE_DIR}/.installed" ]]; then
  cp -a "${SOURCE_DIR}/." "${RELEASE_DIR}/"
  python3 -m venv "${RELEASE_DIR}/.venv"
  "${RELEASE_DIR}/.venv/bin/pip" install --disable-pip-version-check --no-input "${RELEASE_DIR}"
  touch "${RELEASE_DIR}/.installed"
fi
ln -sfn "${RELEASE_DIR}" "${CURRENT_LINK}"

cat > /usr/local/bin/gracectl <<'WRAPPER'
#!/usr/bin/env bash
set -Eeuo pipefail
export GRACE_BRIDGE_HOME="/opt/bedolaga-grace-bridge/current"
exec /opt/bedolaga-grace-bridge/current/.venv/bin/gracectl "$@"
WRAPPER
chmod 755 /usr/local/bin/gracectl

install -m 755 "${RELEASE_DIR}/scripts/gracebridge-rescue.sh" /usr/local/sbin/gracebridge-rescue
mkdir -p /usr/local/lib/bedolaga-grace-bridge
install -m 755 "${RELEASE_DIR}/scripts/rescue_standalone.py" \
  /usr/local/lib/bedolaga-grace-bridge/rescue_standalone.py

if [[ ! -f /etc/bedolaga-grace-bridge/config.env ]]; then
  install -m 600 "${RELEASE_DIR}/templates/config.env.example" /etc/bedolaga-grace-bridge/config.env.example
fi
if [[ ! -f /etc/bedolaga-grace-bridge/secrets.env ]]; then
  install -m 600 "${RELEASE_DIR}/templates/secrets.env.example" /etc/bedolaga-grace-bridge/secrets.env.example
fi

echo "Bedolaga Grace Bridge ${VERSION} установлен. Ни один пользователь не изменён."
if [[ -t 0 && -t 1 ]]; then
  echo "Запускается автоматический мастер настройки и безопасного включения."
  gracectl wizard
else
  echo "Для запуска мастера выполните: sudo gracectl wizard"
fi
