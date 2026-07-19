from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

import asyncpg

from .config import BridgeConfig
from .remnawave import RemnawaveClient
from .runtime import RuntimeSettings


@dataclass(frozen=True, slots=True)
class DrainResult:
    restored: int
    skipped_paid: int
    skipped_external_change: int
    failed: int


def _maintenance_settings(config: BridgeConfig) -> RuntimeSettings:
    squad = UUID(config.grace_squad_uuid) if config.grace_squad_uuid else None
    return RuntimeSettings(
        mode="canary",
        write_enabled=True,
        database_dsn=config.database_dsn,
        remnawave_api_url=config.remnawave_api_url,
        remnawave_api_key=config.remnawave_api_key,
        grace_squad_uuid=squad,
        canary_uuid=UUID(int=0),
        activation_percent=0,
        grace_duration_days=config.grace_duration_days,
        grace_traffic_limit_bytes=config.grace_traffic_limit_bytes,
        grace_tag="GRACE_ACCESS",
        candidate_batch_size=config.candidate_batch_size,
        command_workers=min(config.command_workers, 4),
        max_command_attempts=config.max_command_attempts,
        scan_interval_seconds=60,
        reconcile_interval_seconds=300,
        health_port=8080,
        lock_namespace=1196573509,
    )


async def drain_grace(config: BridgeConfig) -> DrainResult:
    """Compensate only panel overlays that are still visibly owned by Bridge."""
    settings = _maintenance_settings(config)
    pool = await asyncpg.create_pool(config.database_dsn, min_size=1, max_size=2)
    restored = skipped_paid = skipped_external = failed = 0
    try:
        async with RemnawaveClient(settings) as client:
            async with pool.acquire() as connection:
                rows = await connection.fetch(
                    "SELECT subscription_id FROM grace_bridge.access_state "
                    "WHERE state IN ('pending_activate','active','exhausted','failed') "
                    "ORDER BY subscription_id"
                )
            for summary in rows:
                subscription_id = int(summary["subscription_id"])
                try:
                    async with pool.acquire() as connection, connection.transaction():
                        row = await connection.fetchrow(
                            "SELECT * FROM grace_bridge.access_state WHERE subscription_id=$1 FOR UPDATE",
                            subscription_id,
                        )
                        if row is None or str(row["state"]) not in {
                            "pending_activate",
                            "active",
                            "exhausted",
                            "failed",
                        }:
                            continue
                        await connection.fetchval(
                            "SELECT pg_advisory_xact_lock($1,$2)", 1196573509, int(row["user_id"])
                        )
                        has_paid = await connection.fetchval(
                            "SELECT EXISTS(SELECT 1 FROM public.subscriptions "
                            "WHERE user_id=$1 AND lower(status) IN ('active','trial') AND end_date>now())",
                            int(row["user_id"]),
                        )
                        user_uuid = str(row["remnawave_uuid"])
                        panel = await client.get_user(user_uuid)
                        if has_paid:
                            skipped_paid += 1
                        elif panel.get("tag") != settings.grace_tag:
                            skipped_external += 1
                        elif row["previous_panel_state"]:
                            snapshot = row["previous_panel_state"]
                            if isinstance(snapshot, str):
                                snapshot = json.loads(snapshot)
                            await client.restore_snapshot(user_uuid, dict(snapshot))
                            restored += 1
                        else:
                            skipped_external += 1
                        await connection.execute(
                            "UPDATE grace_bridge.access_state SET state='suppressed',"
                            "generation=generation+1,updated_at=now() WHERE subscription_id=$1",
                            subscription_id,
                        )
                        await connection.execute(
                            "UPDATE grace_bridge.commands SET state='cancelled',updated_at=now() "
                            "WHERE subscription_id=$1 AND state IN ('pending','processing')",
                            subscription_id,
                        )
                except Exception:
                    failed += 1
    finally:
        await pool.close()
    return DrainResult(restored, skipped_paid, skipped_external, failed)
