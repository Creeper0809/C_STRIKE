"""C-GUARD status helpers (PostgreSQL).

운영포털 통합을 위해 외주 MySQL 쿼리를 PostgreSQL로 포팅.
- 테이블: cstrike.cguard_user_status (infra/db/init/02-discord-bot-tables.sql 에 정의됨)
- `ON DUPLICATE KEY UPDATE` → `ON CONFLICT (discord_user_id) DO UPDATE SET`
- 모든 쿼리는 `cstrike.` prefix 명시 (search_path 의존 최소화)
- 함수 시그니처는 외주 cogs/가 import하므로 유지
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .audit import create_log
from .connection import _connect, _now_kst_naive


def set_cguard_user_status(
    discord_user_id: str,
    status: str,
    *,
    reason: str | None = None,
    source: str | None = "cguard",
    event_time: datetime | None = None,
) -> None:
    user_id = str(discord_user_id or "").strip()
    normalized_status = str(status or "").strip().lower()
    if not user_id:
        raise ValueError("discord_user_id is required")
    if normalized_status not in {"ok", "on", "enabled", "blocked", "off", "disabled"}:
        raise ValueError("status must be one of ok, on, enabled, blocked, off, disabled")

    now = _now_kst_naive()
    event_at = event_time or now
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cstrike.cguard_user_status (
                    discord_user_id, status, reason, source, last_event_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (discord_user_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    reason = EXCLUDED.reason,
                    source = EXCLUDED.source,
                    last_event_at = EXCLUDED.last_event_at,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    user_id,
                    normalized_status,
                    reason,
                    source,
                    event_at,
                    now,
                ),
            )
        con.commit()
    finally:
        con.close()


def get_cguard_user_status(discord_user_id: str) -> dict[str, Any] | None:
    user_id = str(discord_user_id or "").strip()
    if not user_id:
        return None

    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                """
                SELECT discord_user_id, status, reason, source, last_event_at, updated_at
                FROM cstrike.cguard_user_status
                WHERE discord_user_id=%s
                LIMIT 1
                """,
                (user_id,),
            )
            row = cur.fetchone()
    finally:
        con.close()
    return dict(row) if row else None


def is_cguard_user_blocked(discord_user_id: str) -> bool:
    row = get_cguard_user_status(discord_user_id)
    if not row:
        return False
    return str(row.get("status", "")).strip().lower() in {"blocked", "off", "disabled"}


def create_cguard_log(
    *,
    ip: str,
    nickname: str,
    event_time: datetime,
    discord_user_id: str | None = None,
) -> int:
    create_log(
        log_type="cguard_ai",
        action="CGUARD_AI_DETECTED",
        user_id=str(discord_user_id).strip() if discord_user_id else None,
        detail=f"ip={str(ip).strip()}; nickname={str(nickname).strip()}",
        payload={
            "ip": str(ip).strip(),
            "nickname": str(nickname).strip(),
            "discord_user_id": str(discord_user_id).strip() if discord_user_id else None,
            "time": event_time.isoformat(sep=" ", timespec="seconds"),
        },
        timestamp=event_time,
    )
    return 1


def create_cguard_onoff_log(
    *,
    discord_user_id: str,
    nickname: str,
    onoff: str,
    event_time: datetime,
    ip: str = "-",
) -> int:
    create_log(
        log_type="cguard_onoff",
        action="CGUARD_ONOFF_RECEIVED",
        user_id=str(discord_user_id).strip(),
        detail=f"onoff={str(onoff).strip().lower()}; ip={str(ip).strip() or '-'}; nickname={str(nickname).strip()}",
        payload={
            "discord_user_id": str(discord_user_id).strip(),
            "nickname": str(nickname).strip(),
            "onoff": str(onoff).strip().lower(),
            "ip": str(ip).strip() or "-",
            "time": event_time.isoformat(sep=" ", timespec="seconds"),
        },
        timestamp=event_time,
    )
    return 1


__all__ = [
    "set_cguard_user_status",
    "get_cguard_user_status",
    "is_cguard_user_blocked",
    "create_cguard_log",
    "create_cguard_onoff_log",
]
