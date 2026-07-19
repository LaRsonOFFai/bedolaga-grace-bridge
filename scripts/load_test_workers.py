#!/usr/bin/env python3
"""Изолированный тест конкурентных worker'ов с фальшивой Remnawave."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import defaultdict
from datetime import UTC, datetime
from urllib.parse import urlparse

import asyncpg
from aiohttp import web
from load_test_postgres import (
    CONFIRMATION,
    DATABASE_NAME,
    POPULATE_SUBSCRIPTIONS,
    POPULATE_TARIFFS,
    POPULATE_TRANSACTIONS,
    POPULATE_USERS,
)

from bedolaga_grace_bridge.controller import BridgeController
from bedolaga_grace_bridge.remnawave import RemnawaveClient
from bedolaga_grace_bridge.runtime import WRITE_CONFIRMATION, RuntimeSettings

USERS = 1_000
WORKERS = 4
SQUAD_UUID = "555271fb-9170-4587-a32f-1436383cfa94"


class FakePanel:
    def __init__(self) -> None:
        self.users: dict[str, dict[str, object]] = {}
        self.calls: defaultdict[tuple[str, str], int] = defaultdict(int)
        self.in_flight = 0
        self.max_in_flight = 0

    async def _enter(self, method: str, uuid: str) -> None:
        self.calls[(method, uuid)] += 1
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0.002)

    def _leave(self) -> None:
        self.in_flight -= 1

    def _user(self, uuid: str) -> dict[str, object]:
        return self.users.setdefault(
            uuid,
            {
                "uuid": uuid,
                "status": "DISABLED",
                "expireAt": datetime(2026, 7, 18, tzinfo=UTC).isoformat(),
                "trafficLimitBytes": 50 * 1024**3,
                "trafficLimitStrategy": "NO_RESET",
                "activeInternalSquads": [],
                "tag": "PAID",
            },
        )

    async def get_user(self, request: web.Request) -> web.Response:
        uuid = request.match_info["uuid"]
        await self._enter("GET", uuid)
        try:
            return web.json_response({"response": self._user(uuid)})
        finally:
            self._leave()

    async def patch_user(self, request: web.Request) -> web.Response:
        payload = await request.json()
        uuid = str(payload["uuid"])
        await self._enter("PATCH", uuid)
        try:
            user = self._user(uuid)
            user.update(payload)
            user["activeInternalSquads"] = [
                {"uuid": value} for value in payload.get("activeInternalSquads", [])
            ]
            return web.json_response({"response": user})
        finally:
            self._leave()

    async def reset_traffic(self, request: web.Request) -> web.Response:
        uuid = request.match_info["uuid"]
        await self._enter("RESET", uuid)
        try:
            return web.json_response({"response": self._user(uuid)})
        finally:
            self._leave()


def guarded_dsn() -> str:
    dsn = os.getenv("LOAD_TEST_DATABASE_DSN", "").strip()
    if urlparse(dsn).path.lstrip("/") != DATABASE_NAME:
        raise RuntimeError(f"Worker test разрешён только в базе {DATABASE_NAME}")
    if os.getenv("GRACE_LOAD_TEST_CONFIRM", "") != CONFIRMATION:
        raise RuntimeError(f"Требуется GRACE_LOAD_TEST_CONFIRM={CONFIRMATION}")
    return dsn


async def prepare(dsn: str) -> None:
    connection = await asyncpg.connect(dsn)
    try:
        if await connection.fetchval("SELECT current_database()") != DATABASE_NAME:
            raise RuntimeError("Имя базы после подключения не совпало")
        await connection.execute(
            "TRUNCATE grace_bridge.commands, grace_bridge.access_state, grace_bridge.events, "
            "transactions, subscriptions, tariffs, users RESTART IDENTITY CASCADE"
        )
        await connection.execute(POPULATE_TARIFFS)
        await connection.execute(POPULATE_USERS, USERS)
        await connection.execute(POPULATE_SUBSCRIPTIONS, USERS)
        await connection.execute(POPULATE_TRANSACTIONS, USERS)
    finally:
        await connection.close()


async def main() -> None:
    dsn = guarded_dsn()
    await prepare(dsn)
    panel = FakePanel()
    app = web.Application()
    app.router.add_get("/api/users/{uuid}", panel.get_user)
    app.router.add_patch("/api/users", panel.patch_user)
    app.router.add_post("/api/users/{uuid}/actions/reset-traffic", panel.reset_traffic)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 18081)
    await site.start()

    os.environ.update(
        {
            "DATABASE_DSN": dsn,
            "GRACE_MODE": "active",
            "GRACE_WRITE_ENABLED": "true",
            "GRACE_ALL_USERS_CONFIRM": WRITE_CONFIRMATION,
            "REMNAWAVE_API_URL": "http://127.0.0.1:18081",
            "REMNAWAVE_API_KEY": "isolated-worker-test",
            "GRACE_SQUAD_UUID": SQUAD_UUID,
            "GRACE_DURATION_DAYS": "7",
            "GRACE_TRAFFIC_LIMIT_BYTES": str(1024**3),
            "CANDIDATE_BATCH_SIZE": "500",
            "COMMAND_WORKERS": str(WORKERS),
            "ACTIVATION_PERCENT": "100",
        }
    )
    controller = BridgeController(RuntimeSettings.from_environment())
    await controller.open()
    started = time.perf_counter()
    try:
        enqueued = 0
        while True:
            rows = await controller.fetch_candidates()
            if not rows:
                break
            for row in rows:
                enqueued += int(await controller.enqueue(row))
        if enqueued != USERS:
            raise RuntimeError(f"Ожидалось {USERS} команд, создано {enqueued}")

        async with RemnawaveClient(controller.settings) as client:

            async def worker(number: int) -> int:
                processed = 0
                while await controller.process_one(client, f"load-{number}"):
                    processed += 1
                return processed

            per_worker = await asyncio.gather(*(worker(number) for number in range(WORKERS)))

        if controller.pool is None:
            raise RuntimeError("Пул PostgreSQL неожиданно закрыт")
        async with controller.pool.acquire() as connection:
            state = await connection.fetchrow(
                "SELECT "
                "(SELECT count(*) FROM grace_bridge.access_state WHERE state='active') AS active,"
                "(SELECT count(*) FROM grace_bridge.commands WHERE state='done') AS done,"
                "(SELECT count(*) FROM grace_bridge.commands "
                "WHERE state IN ('pending','processing')) AS stuck,"
                "(SELECT count(*) FROM grace_bridge.access_state WHERE previous_panel_state IS NULL) "
                "AS missing_snapshots"
            )
            advisory_locks = await connection.fetchval(
                "SELECT count(*) FROM pg_locks WHERE locktype='advisory' "
                "AND database=(SELECT oid FROM pg_database WHERE datname=current_database())"
            )
        if dict(state) != {"active": USERS, "done": USERS, "stuck": 0, "missing_snapshots": 0}:
            raise RuntimeError(f"Некорректное устойчивое состояние: {dict(state)}")
        if advisory_locks:
            raise RuntimeError(f"Обнаружены утёкшие advisory-lock: {advisory_locks}")
        if any(panel.calls[("PATCH", uuid)] != 1 for uuid in panel.users):
            raise RuntimeError("PATCH был выполнен не ровно один раз для каждого UUID")
        if any(panel.calls[("RESET", uuid)] != 1 for uuid in panel.users):
            raise RuntimeError("RESET был выполнен не ровно один раз для каждого UUID")
        elapsed = time.perf_counter() - started
        print(
            json.dumps(
                {
                    "database": DATABASE_NAME,
                    "external_api": False,
                    "users": USERS,
                    "workers": WORKERS,
                    "processed_per_worker": per_worker,
                    "seconds": round(elapsed, 3),
                    "activations_per_second": round(USERS / elapsed, 1),
                    "max_fake_api_concurrency": panel.max_in_flight,
                    "durable_state": dict(state),
                    "advisory_locks_after_test": advisory_locks,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        await controller.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
