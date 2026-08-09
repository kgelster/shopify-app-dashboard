import os
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def connect() -> psycopg.Connection:
    # TimeZone is pinned because ten aggregates in stats.py bucket months with
    # date_trunc('month', <timestamptz>), which resolves in the *connection's*
    # session timezone. Nothing set it before: production inherited UTC from the
    # server while a developer machine inherited America/Chicago from the shell,
    # so the same row could land in different months in dev and in prod, and a
    # test written locally could pass on data that buckets differently live.
    # Measured against production on 2026-08-08: its session was already UTC, so
    # this changes no number there and only removes the dev/test drift.
    return psycopg.connect(
        os.environ["DATABASE_URL"], autocommit=True, options="-c TimeZone=UTC"
    )


def run_migrations(conn: psycopg.Connection) -> None:
    conn.execute("""
        create table if not exists schema_migrations (
            filename text primary key,
            applied_at timestamptz default now()
        )
    """)
    applied = {
        row[0] for row in conn.execute("select filename from schema_migrations").fetchall()
    }
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in applied:
            continue
        conn.execute(path.read_text())
        conn.execute(
            "insert into schema_migrations (filename) values (%s)", (path.name,)
        )
