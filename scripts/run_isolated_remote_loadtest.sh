#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_ID="grace-bridge-loadtest-$(date +%s)-$$"
NETWORK="${RUN_ID}"
POSTGRES="${RUN_ID}-postgres"
IMAGE="${RUN_ID}:local"
PASSWORD="loadtest-only-not-a-production-secret"

cleanup() {
  docker rm -f "${POSTGRES}" >/dev/null 2>&1 || true
  docker network rm "${NETWORK}" >/dev/null 2>&1 || true
  docker image rm -f "${IMAGE}" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

for value in "${RUN_ID}" "${NETWORK}" "${POSTGRES}" "${IMAGE}"; do
  [[ "${value}" == grace-bridge-loadtest-* ]] || {
    echo "Защитная проверка имени временного ресурса не пройдена" >&2
    exit 2
  }
done

docker network create --internal "${NETWORK}" >/dev/null
docker run -d \
  --name "${POSTGRES}" \
  --network "${NETWORK}" \
  --network-alias postgres \
  --memory 512m \
  --cpus 1 \
  --tmpfs /var/lib/postgresql/data:rw,noexec,nosuid,size=384m \
  -e POSTGRES_DB=grace_bridge_loadtest \
  -e POSTGRES_USER=postgres \
  -e POSTGRES_PASSWORD="${PASSWORD}" \
  postgres:16-alpine >/dev/null

for _ in $(seq 1 60); do
  if docker exec "${POSTGRES}" pg_isready -U postgres -d grace_bridge_loadtest >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
docker exec "${POSTGRES}" pg_isready -U postgres -d grace_bridge_loadtest >/dev/null

docker build --pull=false --tag "${IMAGE}" --file "${ROOT}/deploy/Dockerfile" "${ROOT}" >/dev/null
docker run --rm \
  --network "${NETWORK}" \
  --memory 384m \
  --cpus 1 \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --volume "${ROOT}:/test:ro" \
  --entrypoint python \
  -e LOAD_TEST_DATABASE_DSN="postgresql://postgres:${PASSWORD}@postgres:5432/grace_bridge_loadtest" \
  -e GRACE_LOAD_TEST_CONFIRM=ISOLATED_GRACE_BRIDGE_LOADTEST \
  "${IMAGE}" \
  /test/scripts/load_test_postgres.py --schema-dir /test/schema --users 40000 --batch-size 500

docker run --rm \
  --network "${NETWORK}" \
  --memory 384m \
  --cpus 1 \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --volume "${ROOT}:/test:ro" \
  --entrypoint python \
  -e LOAD_TEST_DATABASE_DSN="postgresql://postgres:${PASSWORD}@postgres:5432/grace_bridge_loadtest" \
  -e GRACE_LOAD_TEST_CONFIRM=ISOLATED_GRACE_BRIDGE_LOADTEST \
  "${IMAGE}" \
  /test/scripts/load_test_workers.py
