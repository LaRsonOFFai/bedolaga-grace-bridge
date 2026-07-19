#!/usr/bin/env python3
"""Изолированный нагрузочный тест SQL-сканера Grace Bridge.

Скрипт намеренно работает только с отдельной базой ``grace_bridge_loadtest``
и требует явную фразу подтверждения. Сетевых вызовов к Remnawave в нём нет.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import asyncpg

from bedolaga_grace_bridge.queries import ELIGIBLE_BATCH_SQL

DATABASE_NAME = "grace_bridge_loadtest"
CONFIRMATION = "ISOLATED_GRACE_BRIDGE_LOADTEST"

SCRATCH_SCHEMA = """
DROP SCHEMA IF EXISTS grace_bridge CASCADE;
DROP TABLE IF EXISTS transactions, subscriptions, tariffs, users CASCADE;

CREATE TABLE users (
    id integer PRIMARY KEY,
    remnawave_uuid varchar(255) UNIQUE,
    status varchar(20) NOT NULL,
    has_had_paid_subscription boolean NOT NULL,
    restriction_subscription boolean NOT NULL DEFAULT false
);
CREATE TABLE tariffs (
    id integer PRIMARY KEY,
    is_daily boolean NOT NULL DEFAULT false
);
CREATE TABLE subscriptions (
    id integer PRIMARY KEY,
    user_id integer NOT NULL REFERENCES users(id),
    remnawave_uuid varchar(255),
    status varchar(20) NOT NULL,
    start_date timestamptz NOT NULL DEFAULT now(),
    end_date timestamptz NOT NULL,
    tariff_id integer REFERENCES tariffs(id),
    is_trial boolean NOT NULL DEFAULT false
);
CREATE INDEX ix_subscriptions_user_status
    ON subscriptions (user_id, status);
CREATE TABLE transactions (
    id bigint PRIMARY KEY,
    user_id integer NOT NULL REFERENCES users(id),
    type varchar(50) NOT NULL,
    amount_kopeks integer NOT NULL,
    is_completed boolean NOT NULL
);
"""

POPULATE_TARIFFS = "INSERT INTO tariffs (id, is_daily) VALUES (1, false)"

POPULATE_USERS = """
INSERT INTO users (
    id, remnawave_uuid, status, has_had_paid_subscription, restriction_subscription
)
SELECT n,
       '00000000-0000-4000-8000-' || lpad(n::text, 12, '0'),
       'active', true, false
FROM generate_series(1, $1::integer) AS n;
"""

POPULATE_SUBSCRIPTIONS = """
INSERT INTO subscriptions (
    id, user_id, remnawave_uuid, status, end_date, tariff_id, is_trial
)
SELECT n, n,
       '00000000-0000-4000-8000-' || lpad(n::text, 12, '0'),
       'expired', now() - interval '1 day', 1, false
FROM generate_series(1, $1::integer) AS n;
"""

POPULATE_TRANSACTIONS = """
INSERT INTO transactions (id, user_id, type, amount_kopeks, is_completed)
SELECT n, n, 'subscription_payment', 10000, true
FROM generate_series(1, $1::integer) AS n;
"""


def settings() -> tuple[str, int, int, Path]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--users", type=int, default=40_000)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--schema-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "schema",
    )
    args = parser.parse_args()
    dsn = os.getenv("LOAD_TEST_DATABASE_DSN", "").strip()
    parsed = urlparse(dsn)
    if parsed.scheme not in {"postgres", "postgresql"} or parsed.path.lstrip("/") != DATABASE_NAME:
        raise RuntimeError(f"LOAD_TEST_DATABASE_DSN должен вести только в базу {DATABASE_NAME}")
    if os.getenv("GRACE_LOAD_TEST_CONFIRM", "") != CONFIRMATION:
        raise RuntimeError(f"Требуется GRACE_LOAD_TEST_CONFIRM={CONFIRMATION}")
    if not 1 <= args.users <= 100_000:
        raise RuntimeError("--users должен быть между 1 и 100000")
    if not 1 <= args.batch_size <= 5000:
        raise RuntimeError("--batch-size должен быть между 1 и 5000")
    return dsn, args.users, args.batch_size, args.schema_dir


async def main() -> None:
    dsn, users, batch_size, schema_dir = settings()
    connection = await asyncpg.connect(
        dsn,
        server_settings={"application_name": "grace-bridge-isolated-loadtest"},
    )
    try:
        database = await connection.fetchval("SELECT current_database()")
        if database != DATABASE_NAME:
            raise RuntimeError("Имя базы после подключения не совпало с защитным значением")
        started = time.perf_counter()
        await connection.execute(SCRATCH_SCHEMA)
        await connection.execute((schema_dir / "001_initial.sql").read_text(encoding="utf-8"))
        # asyncpg wraps a multi-statement string in one implicit transaction,
        # while CREATE INDEX CONCURRENTLY is forbidden there. The scratch DB
        # has no live traffic, so the load-test copy intentionally uses normal
        # index creation. Production applies the original file through psql
        # with autocommit and preserves CONCURRENTLY.
        candidate_indexes = (schema_dir / "002_candidate_indexes.sql").read_text(encoding="utf-8")
        await connection.execute(candidate_indexes.replace(" CONCURRENTLY", ""))
        await connection.execute(POPULATE_TARIFFS)
        await connection.execute(POPULATE_USERS, users)
        await connection.execute(POPULATE_SUBSCRIPTIONS, users)
        await connection.execute(POPULATE_TRANSACTIONS, users)
        await connection.execute("ANALYZE users; ANALYZE subscriptions; ANALYZE transactions;")
        prepared_seconds = time.perf_counter() - started

        scan_started = time.perf_counter()
        cursor = ""
        seen: set[str] = set()
        batches = 0
        while True:
            rows = await connection.fetch(ELIGIBLE_BATCH_SQL, cursor, None, batch_size)
            if not rows:
                break
            batches += 1
            for row in rows:
                value = str(row["remnawave_uuid"])
                if value in seen:
                    raise RuntimeError(f"Повтор UUID в keyset-сканере: {value}")
                seen.add(value)
            cursor = max(str(row["remnawave_uuid"]) for row in rows)
        scan_seconds = time.perf_counter() - scan_started
        if len(seen) != users:
            raise RuntimeError(f"Ожидалось {users} кандидатов, получено {len(seen)}")
        print(
            json.dumps(
                {
                    "database": database,
                    "external_api": False,
                    "users": users,
                    "batch_size": batch_size,
                    "batches": batches,
                    "prepare_seconds": round(prepared_seconds, 3),
                    "scan_seconds": round(scan_seconds, 3),
                    "candidates_per_second": round(users / scan_seconds, 1),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(main())
