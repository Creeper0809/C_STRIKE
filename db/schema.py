"""Legacy schema validation module (PostgreSQL).

외주 원본은 1,392줄 PyMySQL 기반 DDL 관리 모듈이었으나, 운영포털 통합 과정에서
테이블 생성/마이그레이션은 `infra/db/init/02-discord-bot-tables.sql`로 이관됨.
본 모듈은 이제 기동 시점에 11개 외주 테이블 존재 여부만 검증한다.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

try:
    import config
except ModuleNotFoundError as exc:
    if exc.name != "config":
        raise
    from . import config

try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except ModuleNotFoundError:
    psycopg2 = None
    RealDictCursor = None


KST = timezone(timedelta(hours=9), name="KST")
LOGGER = logging.getLogger("ops.db")


# 운영포털 cstrike 스키마 내 외주 디스코드 봇 전용 테이블 목록
DISCORD_BOT_TABLES = (
    "ticket_settings",
    "ticket_counters",
    "scheduled_announcements",
    "team_announcements",
    "team_hints",
    "team_countdown_schedules",
    "team_notification_events",
    "relay_events",
    "relay_dispatch_history",
    "cguard_user_status",
    "problem",
)


def _now_kst_naive() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_driver() -> None:
    if psycopg2 is None:
        raise RuntimeError("psycopg2 is not installed. Run: pip install psycopg2-binary")


def _connect_for_init():
    """init_db 전용 프라이빗 연결 헬퍼.

    connection.py의 `_connect`와 기능 동일하지만, 순환 import 회피를 위해
    본 모듈에서 자체적으로 psycopg2 연결을 생성한다.
    """
    _ensure_driver()
    conn = psycopg2.connect(
        host=config.POSTGRES_HOST,
        port=config.POSTGRES_PORT,
        user=config.POSTGRES_USER,
        password=config.POSTGRES_PASSWORD,
        dbname=config.POSTGRES_DATABASE,
        cursor_factory=RealDictCursor,
    )
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {config.POSTGRES_SCHEMA}, public")
    conn.commit()
    return conn


def init_db() -> None:
    """외주 테이블 존재 검증 + ticket_counters seed 보강.

    - DDL은 init SQL에서 이미 수행되므로 본 함수는 무결성 체크만 담당
    - 누락이 있더라도 exception raise 없이 WARNING 로그만 남김 (운영 기동 가능)
    """
    conn = _connect_for_init()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT table_name
                  FROM information_schema.tables
                 WHERE table_schema = %s
                   AND table_name = ANY(%s)
                """,
                (config.POSTGRES_SCHEMA, list(DISCORD_BOT_TABLES)),
            )
            rows = cur.fetchall() or []
            present = {row["table_name"] for row in rows}

        missing = [name for name in DISCORD_BOT_TABLES if name not in present]
        if missing:
            LOGGER.warning(
                "DB initialization: missing discord-bot tables in schema %s: %s",
                config.POSTGRES_SCHEMA,
                ", ".join(missing),
            )
        else:
            LOGGER.info("DB initialization: all 11 discord-bot tables present")

        # ticket_counters seed 보강 (init SQL이 이미 넣지만 외주 원본 의도 유지)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cstrike.ticket_counters (counter_key, seq_value, updated_at)
                VALUES ('ticket', 0, now())
                ON CONFLICT (counter_key) DO NOTHING
                """
            )
        conn.commit()
        LOGGER.info("DB initialization synchronized")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


__all__ = ["init_db", "DISCORD_BOT_TABLES", "KST", "LOGGER", "_now_kst_naive"]


# NOTE:
# Domain functions originally defined below were migrated into modules under
# `db/` (team/cguard/ticket/audit).  This legacy schema module now only
# validates that cstrike schema tables (populated via init SQL
# `infra/db/init/02-discord-bot-tables.sql`) are present.
