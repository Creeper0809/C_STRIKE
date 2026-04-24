"""Unified audit log helpers (PostgreSQL).

운영포털 통합을 위해 외주 `audit_logs` 테이블 쿼리를 운영포털 `cstrike.ops_audit_logs`로 포팅.
- 외주 id BIGINT AUTO_INCREMENT → 운영포털 id UUID (uuid.uuid4() 생성)
- 외주 다중 컬럼(log_type/event_key/ticket_id/user_id/guild_id/channel_id/status/severity/detail/context/payload_json)
  → 운영포털 `details JSONB`에 embed
- 외주 actor_id(Discord user id 문자열) → operators.id FK에 부합하지 않으므로 actor_id=NULL,
  대신 actor_name 또는 details.actor_id에 보존
- target_type/target_id UUID FK는 NULL 유지 (ticket_id는 details에 포함)
- CURRENT_TIMESTAMP(6) → now()
- 함수 시그니처는 외주 cogs/가 import하므로 절대 유지
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from psycopg2.extras import Json

from .connection import _connect, _now_kst_naive


def create_log(
    *,
    log_type: str,
    action: str,
    ticket_id: str | None = None,
    event_key: str | None = None,
    actor_id: str | None = None,
    actor_name: str | None = None,
    user_id: str | None = None,
    guild_id: str | None = None,
    channel_id: str | None = None,
    status: str | None = None,
    severity: str | None = None,
    detail: str = "",
    context: str = "",
    payload: dict[str, Any] | None = None,
    timestamp: datetime | None = None,
) -> None:
    ts = timestamp or _now_kst_naive()

    # 외주가 각 컬럼으로 쓰던 값들을 운영포털 details JSON에 embed
    details: dict[str, Any] = {
        "log_type": str(log_type or "audit").strip() or "audit",
        "event_key": str(event_key).strip() if event_key else None,
        "ticket_id": str(ticket_id).strip() if ticket_id else None,
        "actor_id": str(actor_id).strip() if actor_id else None,
        "user_id": str(user_id).strip() if user_id else (str(actor_id).strip() if actor_id else None),
        "guild_id": str(guild_id).strip() if guild_id else None,
        "channel_id": str(channel_id).strip() if channel_id else None,
        "status": str(status).strip() if status else None,
        "severity": str(severity).strip() if severity else None,
        "detail": str(detail or "").strip(),
        "context": str(context or "").strip(),
        "payload": payload if payload is not None else None,
    }
    # None 값 제거 (빈 문자열은 원본 의도상 유지)
    details = {k: v for k, v in details.items() if v is not None}

    # actor_id UUID FK에 Discord user id 문자열은 맞지 않으므로 NULL 저장.
    # actor_name은 NOT NULL 제약(운영포털 모델)이므로 빈값 방어 처리.
    safe_actor_name = str(actor_name).strip() if actor_name else (str(actor_id).strip() if actor_id else "")
    safe_action = str(action or "").strip() or "UNKNOWN"

    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cstrike.ops_audit_logs (
                    id, actor_id, actor_name, action,
                    target_type, target_id, details, ip_address, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    str(uuid.uuid4()),
                    None,
                    safe_actor_name,
                    safe_action,
                    None,
                    None,
                    Json(details),
                    None,
                    ts,
                ),
            )
        con.commit()
    finally:
        con.close()


def create_audit_log(
    ticket_id: str | None,
    actor_id: str,
    actor_name: str,
    action: str,
    detail: str = "",
    timestamp: datetime | None = None,
):
    create_log(
        log_type="audit",
        ticket_id=ticket_id,
        actor_id=actor_id,
        actor_name=actor_name,
        action=action,
        detail=detail,
        timestamp=timestamp,
    )


def create_message_log(
    actor_id: str,
    actor_name: str,
    user_id: str,
    action: str,
    channel_id: str | None = None,
    context: str = "",
    timestamp: datetime | None = None,
):
    create_log(
        log_type="message",
        actor_id=actor_id,
        actor_name=actor_name,
        user_id=user_id,
        action=action,
        channel_id=channel_id,
        context=context,
        timestamp=timestamp,
    )


__all__ = ["create_log", "create_audit_log", "create_message_log"]
