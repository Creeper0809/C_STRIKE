"""Relay event persistence helpers (PostgreSQL).

운영포털 통합을 위해 외주 MySQL 쿼리를 PostgreSQL로 포팅.
- 테이블: cstrike.relay_events, cstrike.relay_dispatch_history (infra/db/init/02-discord-bot-tables.sql 에 정의됨)
- `cursor.lastrowid` → `INSERT ... RETURNING id` + `cur.fetchone()["id"]`
- `SHOW COLUMNS FROM relay_events` 분기는 PostgreSQL init SQL이 컬럼 스키마(event_type/channel_id/payload_json/created_at)를
  고정하므로 제거하고 단일 INSERT 경로로 단순화
- `payload_json`은 init SQL 상 `text` 타입이므로 `json.dumps(...)` 문자열을 그대로 바인딩
- 함수 시그니처는 외주 cogs/가 import하므로 유지
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .connection import _connect, _now_kst_naive


def create_relay_event(
    *,
    event_type: str,
    channel_id: str,
    payload: dict[str, Any],
    created_at: datetime | None = None,
) -> int:
    ts = created_at or _now_kst_naive()
    normalized_event_type = str(event_type or "").strip() or "unknown"
    normalized_channel_id = str(channel_id or "").strip()
    payload_json = json.dumps(payload, ensure_ascii=False, default=str)

    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cstrike.relay_events (
                    event_type, channel_id, payload_json, created_at
                ) VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (
                    normalized_event_type,
                    normalized_channel_id,
                    payload_json,
                    ts,
                ),
            )
            row = cur.fetchone() or {}
            relay_event_id = int(row.get("id") or 0)
        con.commit()
        if relay_event_id <= 0:
            raise RuntimeError("Failed to insert into cstrike.relay_events: RETURNING id empty")
        return relay_event_id
    finally:
        con.close()


def create_dispatch_history(
    *,
    event_key: str | None,
    discord_channel_id: str,
    status: str = "pending",
    error_message: str | None = None,
    sent_at: datetime | None = None,
) -> int:
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cstrike.relay_dispatch_history (
                    event_key, discord_channel_id, status, error_message, sent_at, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    str(event_key).strip() if event_key else None,
                    str(discord_channel_id or "").strip(),
                    str(status or "").strip() or "pending",
                    str(error_message or "").strip() or None,
                    sent_at,
                    _now_kst_naive(),
                ),
            )
            row = cur.fetchone() or {}
            history_id = int(row.get("id") or 0)
        con.commit()
        if history_id <= 0:
            raise RuntimeError("Failed to insert into cstrike.relay_dispatch_history: RETURNING id empty")
        return history_id
    finally:
        con.close()


def mark_dispatch_result(
    *,
    dispatch_id: int,
    status: str,
    error_message: str | None = None,
    sent_at: datetime | None = None,
) -> None:
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                """
                UPDATE cstrike.relay_dispatch_history
                SET status=%s,
                    error_message=%s,
                    sent_at=%s
                WHERE id=%s
                """,
                (
                    str(status or "").strip() or "unknown",
                    str(error_message or "").strip() or None,
                    sent_at,
                    int(dispatch_id),
                ),
            )
        con.commit()
    finally:
        con.close()


__all__ = ["create_relay_event", "create_dispatch_history", "mark_dispatch_result"]
