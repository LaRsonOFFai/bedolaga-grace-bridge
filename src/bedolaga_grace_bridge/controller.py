from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import signal
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import asyncpg
from aiohttp import web

from .queries import ELIGIBLE_BATCH_SQL, ELIGIBLE_BY_ID_SQL
from .remnawave import RemnawaveClient, panel_matches_grace, panel_snapshot
from .runtime import RuntimeSettings

LOGGER = logging.getLogger("bedolaga-grace-bridge")


def _subject(value: str) -> str:
    return hashlib.sha256(value.lower().encode()).hexdigest()[:12]


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        for key in ("event", "mode", "count", "subject", "command", "generation", "attempts"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    os_level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    root.setLevel(os_level)
    LOGGER.setLevel(os_level)


def _selected_for_rollout(user_uuid: str, percent: int) -> bool:
    if percent >= 100:
        return True
    return UUID(user_uuid).int % 100 < percent


async def _finish_cleanup(awaitable: Any) -> None:
    """Complete cleanup before propagating cancellation to the caller."""
    cleanup = asyncio.create_task(awaitable)
    try:
        await asyncio.shield(cleanup)
    except asyncio.CancelledError:
        await cleanup
        raise


class BridgeController:
    def __init__(self, settings: RuntimeSettings) -> None:
        self.settings = settings
        self.pool: asyncpg.Pool | None = None
        self.stop = asyncio.Event()
        self.instance_id = uuid4().hex[:12]
        self.cursor = ""
        self.reconcile_cursor = 0
        self.metrics: dict[str, int | str | None] = {
            "candidate_count": 0,
            "commands_done": 0,
            "commands_failed": 0,
            "reconciled": 0,
            "last_scan_at": None,
        }

    async def open(self) -> None:
        if self.settings.mode == "disabled":
            return
        self.pool = await asyncpg.create_pool(
            self.settings.database_dsn,
            min_size=1,
            max_size=self.settings.command_workers + 3,
            command_timeout=60,
        )

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None

    async def _lock_user(self, connection: asyncpg.Connection, user_id: int) -> None:
        await connection.fetchval(
            "SELECT pg_advisory_xact_lock($1, $2)", self.settings.lock_namespace, int(user_id)
        )

    async def _lock_user_session(self, connection: asyncpg.Connection, user_id: int) -> None:
        await connection.fetchval(
            "SELECT pg_advisory_lock($1, $2)", self.settings.lock_namespace, int(user_id)
        )

    async def _unlock_user_session(self, connection: asyncpg.Connection, user_id: int) -> None:
        await connection.fetchval(
            "SELECT pg_advisory_unlock($1, $2)", self.settings.lock_namespace, int(user_id)
        )

    async def fetch_candidates(self) -> list[asyncpg.Record]:
        if not self.pool:
            return []
        canary = self.settings.canary_uuid if self.settings.mode == "canary" else None
        async with self.pool.acquire() as connection, connection.transaction(readonly=True):
            rows = await connection.fetch(
                ELIGIBLE_BATCH_SQL,
                self.cursor,
                canary,
                self.settings.candidate_batch_size,
            )
        self.metrics["candidate_count"] = len(rows)
        self.metrics["last_scan_at"] = datetime.now(UTC).isoformat()
        self.cursor = max((str(row["remnawave_uuid"]) for row in rows), default="")
        return rows

    def _allowed(self, user_uuid: str) -> bool:
        if self.settings.mode == "canary":
            return self.settings.canary_uuid is not None and str(self.settings.canary_uuid) == user_uuid
        if self.settings.mode == "active":
            return _selected_for_rollout(user_uuid, self.settings.activation_percent)
        return False

    async def enqueue(self, row: asyncpg.Record) -> bool:
        if not self.pool or not self.settings.mutation_enabled:
            return False
        subscription_id = int(row["subscription_id"])
        async with self.pool.acquire() as connection, connection.transaction():
            current = await connection.fetchrow(ELIGIBLE_BY_ID_SQL, subscription_id)
            if current is None:
                return False
            user_id = int(current["user_id"])
            user_uuid = str(current["remnawave_uuid"])
            if not self._allowed(user_uuid):
                return False
            await self._lock_user(connection, user_id)
            current = await connection.fetchrow(ELIGIBLE_BY_ID_SQL, subscription_id)
            if current is None:
                return False
            state = await connection.fetchrow(
                "SELECT state, generation FROM grace_bridge.access_state WHERE subscription_id=$1 FOR UPDATE",
                subscription_id,
            )
            if state and str(state["state"]) != "inactive":
                return False
            other = await connection.fetchval(
                "SELECT state FROM grace_bridge.access_state "
                "WHERE user_id=$1 AND subscription_id<>$2 AND state<>'inactive' "
                "ORDER BY subscription_id FOR UPDATE LIMIT 1",
                user_id,
                subscription_id,
            )
            if other is not None:
                return False
            generation = (int(state["generation"]) if state else 0) + 1
            command_id = uuid4()
            expires_at = datetime.now(UTC) + timedelta(days=self.settings.grace_duration_days)
            await connection.execute(
                """
                INSERT INTO grace_bridge.access_state (
                    subscription_id, user_id, remnawave_uuid, state, generation,
                    expires_at, traffic_limit_bytes, grace_squad_uuid, last_error, updated_at
                ) VALUES ($1,$2,$3,'pending_activate',$4,$5,$6,$7,NULL,now())
                ON CONFLICT (subscription_id) DO UPDATE SET
                    user_id=EXCLUDED.user_id, remnawave_uuid=EXCLUDED.remnawave_uuid,
                    state='pending_activate', generation=EXCLUDED.generation,
                    expires_at=EXCLUDED.expires_at,
                    traffic_limit_bytes=EXCLUDED.traffic_limit_bytes,
                    grace_squad_uuid=EXCLUDED.grace_squad_uuid,
                    started_at=NULL, previous_panel_state=NULL,
                    last_verified_panel_state=NULL, last_error=NULL, updated_at=now()
                """,
                subscription_id,
                user_id,
                user_uuid,
                generation,
                expires_at,
                self.settings.grace_traffic_limit_bytes,
                self.settings.grace_squad_uuid,
            )
            await connection.execute(
                """
                INSERT INTO grace_bridge.commands (
                    id, subscription_id, user_id, generation, action, state, next_attempt_at
                ) VALUES ($1,$2,$3,$4,'activate','pending',now())
                ON CONFLICT (subscription_id,generation,action) DO NOTHING
                """,
                command_id,
                subscription_id,
                user_id,
                generation,
            )
            return True

    async def process_one(self, client: RemnawaveClient, worker_id: str) -> bool:
        if not self.pool or not self.settings.mutation_enabled:
            return False
        claim_worker = f"{self.instance_id}:{worker_id}"
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                command = await connection.fetchrow(
                    """
                    SELECT id, subscription_id, user_id, generation, attempts
                    FROM grace_bridge.commands
                    WHERE (state='pending' AND next_attempt_at<=now())
                       OR (state='processing' AND claimed_at<now()-interval '2 minutes')
                    ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1
                    """
                )
                if command is None:
                    return False
                command_id = command["id"]
                subscription_id = int(command["subscription_id"])
                user_id = int(command["user_id"])
                generation = int(command["generation"])
                attempts = int(command["attempts"]) + 1
                await connection.execute(
                    "UPDATE grace_bridge.commands SET state='processing',attempts=$2,claimed_at=now(),"
                    "worker_id=$3,updated_at=now() WHERE id=$1",
                    command_id,
                    attempts,
                    claim_worker,
                )

            session_locked = False
            user_uuid = ""
            try:
                # A session lock survives the short SQL transactions below and
                # coordinates the complete external API operation with the
                # transaction-level lock used by patched Bedolaga.
                await self._lock_user_session(connection, user_id)
                session_locked = True
                async with connection.transaction():
                    owned = await connection.fetchrow(
                        "SELECT state,worker_id,generation FROM grace_bridge.commands WHERE id=$1 FOR UPDATE",
                        command_id,
                    )
                    access = await connection.fetchrow(
                        "SELECT * FROM grace_bridge.access_state WHERE subscription_id=$1 FOR UPDATE",
                        subscription_id,
                    )
                    if (
                        owned is None
                        or str(owned["state"]) != "processing"
                        or str(owned["worker_id"]) != claim_worker
                        or int(owned["generation"]) != generation
                        or access is None
                        or int(access["generation"]) != generation
                        or str(access["state"]) != "pending_activate"
                    ):
                        return True
                    candidate = await connection.fetchrow(ELIGIBLE_BY_ID_SQL, subscription_id)
                    if candidate is None or not self._allowed(str(access["remnawave_uuid"])):
                        await connection.execute(
                            "UPDATE grace_bridge.access_state SET state='inactive',"
                            "generation=generation+1,updated_at=now() WHERE subscription_id=$1",
                            subscription_id,
                        )
                        await connection.execute(
                            "UPDATE grace_bridge.commands SET state='stale',updated_at=now() WHERE id=$1",
                            command_id,
                        )
                        return True
                    user_uuid = str(access["remnawave_uuid"])
                    expires_at = access["expires_at"]
                    traffic_limit_bytes = int(access["traffic_limit_bytes"])
                    grace_squad_uuid = str(access["grace_squad_uuid"])
                    is_retry_after_snapshot = access["previous_panel_state"] is not None

                before = await client.get_user(user_uuid)
                async with connection.transaction():
                    result = await connection.execute(
                        "UPDATE grace_bridge.access_state "
                        "SET previous_panel_state=COALESCE(previous_panel_state,$2::jsonb),updated_at=now() "
                        "WHERE subscription_id=$1 AND generation=$3 AND state='pending_activate'",
                        subscription_id,
                        json.dumps(panel_snapshot(before)),
                        generation,
                    )
                    if result != "UPDATE 1":
                        raise RuntimeError("Grace command lost ownership before panel mutation")

                if is_retry_after_snapshot and panel_matches_grace(
                    before,
                    expires_at=expires_at,
                    traffic_limit_bytes=traffic_limit_bytes,
                    squad_uuid=grace_squad_uuid,
                    tag=self.settings.grace_tag,
                ):
                    after = before
                else:
                    after = await client.apply_grace(
                        user_uuid,
                        expires_at,
                        traffic_limit_bytes,
                        grace_squad_uuid,
                        self.settings.grace_tag,
                        reset_traffic=not is_retry_after_snapshot,
                    )
                if not panel_matches_grace(
                    after,
                    expires_at=expires_at,
                    traffic_limit_bytes=traffic_limit_bytes,
                    squad_uuid=grace_squad_uuid,
                    tag=self.settings.grace_tag,
                ):
                    raise RuntimeError("Remnawave read-after-write verification failed")
                async with connection.transaction():
                    access_result = await connection.execute(
                        """
                        UPDATE grace_bridge.access_state
                        SET state='active',started_at=COALESCE(started_at,now()),
                            last_verified_panel_state=$2::jsonb,last_error=NULL,updated_at=now()
                        WHERE subscription_id=$1 AND generation=$3 AND state='pending_activate'
                        """,
                        subscription_id,
                        json.dumps(panel_snapshot(after)),
                        generation,
                    )
                    command_result = await connection.execute(
                        "UPDATE grace_bridge.commands SET state='done',last_error=NULL,updated_at=now() "
                        "WHERE id=$1 AND state='processing' AND worker_id=$2",
                        command_id,
                        claim_worker,
                    )
                    if access_result != "UPDATE 1" or command_result != "UPDATE 1":
                        raise RuntimeError("Grace command lost ownership during finalization")
                self.metrics["commands_done"] = int(self.metrics["commands_done"] or 0) + 1
                LOGGER.info(
                    "Grace activation applied",
                    extra={
                        "event": "activation_applied",
                        "mode": self.settings.mode,
                        "subject": _subject(user_uuid),
                        "command": str(command_id),
                        "generation": generation,
                    },
                )
            except Exception as error:
                terminal = attempts >= self.settings.max_command_attempts
                delay = min(3600, 15 * 2 ** min(attempts - 1, 8))
                async with connection.transaction():
                    await connection.execute(
                        "UPDATE grace_bridge.commands SET state=$3,last_error=$4,"
                        "next_attempt_at=now()+make_interval(secs=>$5),updated_at=now() "
                        "WHERE id=$1 AND state='processing' AND worker_id=$2",
                        command_id,
                        claim_worker,
                        "failed" if terminal else "pending",
                        str(error)[:2000],
                        delay,
                    )
                    await connection.execute(
                        "UPDATE grace_bridge.access_state SET state=$2,last_error=$3,updated_at=now() "
                        "WHERE subscription_id=$1 AND generation=$4 AND state='pending_activate'",
                        subscription_id,
                        "failed" if terminal else "pending_activate",
                        str(error)[:2000],
                        generation,
                    )
                self.metrics["commands_failed"] = int(self.metrics["commands_failed"] or 0) + 1
                LOGGER.warning(
                    "Grace activation failed",
                    extra={
                        "event": "activation_failed",
                        "mode": self.settings.mode,
                        "subject": _subject(user_uuid) if user_uuid else None,
                        "command": str(command_id),
                        "attempts": attempts,
                    },
                )
            finally:
                if session_locked:
                    # Guarantees that the session lock is released before the
                    # pooled connection can be handed to another worker.
                    await _finish_cleanup(self._unlock_user_session(connection, user_id))
            return True

    async def reconcile_one(self, client: RemnawaveClient, subscription_id: int) -> None:
        if not self.pool:
            return
        async with self.pool.acquire() as connection, connection.transaction():
            access = await connection.fetchrow(
                "SELECT * FROM grace_bridge.access_state WHERE subscription_id=$1 FOR UPDATE",
                subscription_id,
            )
            if access is None or str(access["state"]) not in {"pending_activate", "active"}:
                return
            await self._lock_user(connection, int(access["user_id"]))
            candidate = await connection.fetchrow(ELIGIBLE_BY_ID_SQL, subscription_id)
            if candidate is None:
                await connection.execute(
                    """
                    UPDATE grace_bridge.access_state
                    SET state='inactive',generation=generation+1,updated_at=now()
                    WHERE subscription_id=$1
                    """,
                    subscription_id,
                )
                await connection.execute(
                    "UPDATE grace_bridge.commands SET state='stale',updated_at=now() "
                    "WHERE subscription_id=$1 AND state IN ('pending','processing')",
                    subscription_id,
                )
                return
            if access["expires_at"] <= datetime.now(UTC):
                await connection.execute(
                    """
                    UPDATE grace_bridge.access_state
                    SET state='exhausted',updated_at=now()
                    WHERE subscription_id=$1
                    """,
                    subscription_id,
                )
                return
            if str(access["state"]) != "active":
                return
            user_uuid = str(access["remnawave_uuid"])
            panel = await client.get_user(user_uuid)
            if str(panel.get("status", "")).upper() == "LIMITED":
                await connection.execute(
                    """
                    UPDATE grace_bridge.access_state
                    SET state='exhausted',updated_at=now()
                    WHERE subscription_id=$1
                    """,
                    subscription_id,
                )
                return
            if not panel_matches_grace(
                panel,
                expires_at=access["expires_at"],
                traffic_limit_bytes=int(access["traffic_limit_bytes"]),
                squad_uuid=str(access["grace_squad_uuid"]),
                tag=self.settings.grace_tag,
            ):
                verified = await client.apply_grace(
                    user_uuid,
                    access["expires_at"],
                    int(access["traffic_limit_bytes"]),
                    str(access["grace_squad_uuid"]),
                    self.settings.grace_tag,
                    reset_traffic=False,
                )
                if not panel_matches_grace(
                    verified,
                    expires_at=access["expires_at"],
                    traffic_limit_bytes=int(access["traffic_limit_bytes"]),
                    squad_uuid=str(access["grace_squad_uuid"]),
                    tag=self.settings.grace_tag,
                ):
                    raise RuntimeError("Reconciliation verification failed")
            self.metrics["reconciled"] = int(self.metrics["reconciled"] or 0) + 1

    async def _scan_loop(self) -> None:
        while not self.stop.is_set():
            try:
                rows = await self.fetch_candidates()
                LOGGER.info(
                    "Grace candidates observed",
                    extra={"event": "scan", "mode": self.settings.mode, "count": len(rows)},
                )
                if self.settings.mutation_enabled:
                    for row in rows:
                        await self.enqueue(row)
                if rows:
                    # Continue keyset pagination immediately. The configured
                    # interval is between complete scans, not between pages.
                    await asyncio.sleep(0)
                    continue
                self.cursor = ""
            except Exception:
                LOGGER.exception(
                    "Grace scan failed", extra={"event": "scan_failed", "mode": self.settings.mode}
                )
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=self.settings.scan_interval_seconds)
            except TimeoutError:
                pass

    async def _worker_loop(self, client: RemnawaveClient, number: int) -> None:
        worker_id = f"worker-{number}"
        while not self.stop.is_set():
            try:
                processed = await self.process_one(client, worker_id)
            except Exception:
                LOGGER.exception(
                    "Command worker failed", extra={"event": "worker_failed", "mode": self.settings.mode}
                )
                processed = False
            if not processed:
                try:
                    await asyncio.wait_for(self.stop.wait(), timeout=1)
                except TimeoutError:
                    pass

    async def _reconcile_loop(self, client: RemnawaveClient) -> None:
        while not self.stop.is_set():
            try:
                if self.pool and self.settings.mutation_enabled:
                    async with self.pool.acquire() as connection:
                        rows = await connection.fetch(
                            "SELECT subscription_id FROM grace_bridge.access_state "
                            "WHERE state IN ('pending_activate','active') AND subscription_id>$1 "
                            "ORDER BY subscription_id LIMIT 100",
                            self.reconcile_cursor,
                        )
                    if not rows:
                        self.reconcile_cursor = 0
                    else:
                        for row in rows:
                            await self.reconcile_one(client, int(row["subscription_id"]))
                        self.reconcile_cursor = max(int(row["subscription_id"]) for row in rows)
            except Exception:
                LOGGER.exception(
                    "Reconciliation failed", extra={"event": "reconcile_failed", "mode": self.settings.mode}
                )
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=self.settings.reconcile_interval_seconds)
            except TimeoutError:
                pass

    async def health(self, _request: web.Request) -> web.Response:
        database_ok = self.settings.mode == "disabled"
        if self.pool:
            try:
                async with self.pool.acquire() as connection:
                    database_ok = bool(await connection.fetchval("SELECT 1"))
            except Exception:
                database_ok = False
        status = 200 if database_ok else 503
        return web.json_response(
            {"status": "ok" if status == 200 else "degraded", "mode": self.settings.mode, **self.metrics},
            status=status,
        )

    async def run(self) -> None:
        await self.open()
        app = web.Application()
        app.router.add_get("/health", self.health)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.settings.health_port)
        await site.start()
        tasks: list[asyncio.Task[Any]] = []
        if self.settings.mode != "disabled":
            tasks.append(asyncio.create_task(self._scan_loop()))
        if self.settings.mutation_enabled:
            client = await RemnawaveClient(self.settings).__aenter__()
            tasks.extend(
                asyncio.create_task(self._worker_loop(client, number))
                for number in range(self.settings.command_workers)
            )
            tasks.append(asyncio.create_task(self._reconcile_loop(client)))
        else:
            client = None
        try:
            await self.stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if client:
                await client.__aexit__(None, None, None)
            await runner.cleanup()
            await self.close()


async def async_main() -> None:
    configure_logging()
    settings = RuntimeSettings.from_environment()
    controller = BridgeController(settings)
    loop = asyncio.get_running_loop()
    for event in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(event, controller.stop.set)
    await controller.run()


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
