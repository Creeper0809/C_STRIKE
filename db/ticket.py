"""Ticket-domain helpers (PostgreSQL).

운영포털 통합을 위해 외주 MySQL 쿼리를 PostgreSQL로 포팅.
- 테이블명은 `cstrike.` 스키마 prefix 명시
- `ON DUPLICATE KEY UPDATE` → `ON CONFLICT ... DO UPDATE SET`
- `LAST_INSERT_ID` / `cursor.lastrowid` → `RETURNING` + `fetchone()`
- `CURRENT_TIMESTAMP(6)` → `now()`
- 함수 시그니처는 외주 cogs/가 import하므로 절대 유지
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from .connection import _connect, _now_kst_naive

_CATEGORY_PREFIX = {
    "inquiry": "inq",
    "declaration": "dec",
    "objection": "obj",
    "pending": "pnd",
}


def _normalize_ticket_row(row: dict[str, Any]) -> dict[str, Any]:
    # Backward-compatible aliases for existing callers.
    row["ticket_id"] = str(row.get("id") or row.get("ticket_id") or "")
    row["discord_thread_id"] = str(row.get("discord_channel_id") or row.get("discord_thread_id") or "")
    row["user_id"] = str(row.get("reporter_id") or row.get("user_id") or "")
    row["category"] = str(row.get("type") or row.get("category") or "")
    row["user_name"] = str(row.get("team_name") or row.get("user_name") or "")
    row.setdefault("ops_case_id", None)
    row.setdefault("case_description", None)
    row.setdefault("isAprove", None)
    if row.get("rejection_reason") is None:
        row["rejection_reason"] = row.get("resolution")
    return row


def _serialize_times(row: dict[str, Any]) -> dict[str, Any]:
    for key in ("resolved_at", "created_at", "updated_at"):
        value = row.get(key)
        if isinstance(value, datetime):
            row[key] = value.isoformat()
    return row


def _counter_next(counter_key: str) -> int:
    """ticket_counters의 시퀀스를 원자적으로 1 증가시키고 새 값을 반환.

    MySQL 원본: INSERT IGNORE + UPDATE LAST_INSERT_ID(seq+1) + SELECT LAST_INSERT_ID()
    PostgreSQL 포팅: INSERT ... ON CONFLICT DO UPDATE SET ... RETURNING seq_value
    (ON CONFLICT DO UPDATE는 row lock 획득 후 갱신하므로 race-free)
    """
    now = _now_kst_naive()
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cstrike.ticket_counters (counter_key, seq_value, updated_at)
                VALUES (%s, 1, %s)
                ON CONFLICT (counter_key) DO UPDATE
                   SET seq_value = cstrike.ticket_counters.seq_value + 1,
                       updated_at = EXCLUDED.updated_at
                RETURNING seq_value
                """,
                (counter_key, now),
            )
            row = cur.fetchone() or {}
        con.commit()
    finally:
        con.close()

    seq = int(row.get("seq_value") or 0)
    if seq <= 0:
        raise RuntimeError(f"Failed to allocate next sequence for key={counter_key}")
    return seq


def _category_prefix(category: str) -> str:
    key = str(category or "").strip().lower()
    return _CATEGORY_PREFIX.get(key, "PND")


def next_ticket_sequence() -> int:
    return _counter_next("ticket_number:CS-2026")


def allocate_ticket_identity(category: str) -> tuple[str, str]:
    ticket_id = str(uuid4())
    number_seq = _counter_next("ticket_number:CS-2026")
    ticket_number = f"CS-2026-{number_seq:03d}"
    return ticket_id, ticket_number


def _load_ticket_raw(ticket_id: str) -> dict[str, Any] | None:
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute("SELECT * FROM cstrike.tickets WHERE id=%s LIMIT 1", (str(ticket_id),))
            row = cur.fetchone()
    finally:
        con.close()
    return row


def assign_ticket_number_for_category(ticket_id: str, category: str) -> str:
    safe_ticket_id = str(ticket_id or "").strip()
    if not safe_ticket_id:
        raise ValueError("ticket_id is required")

    row = _load_ticket_raw(safe_ticket_id)
    if not row:
        raise ValueError(f"ticket not found: {safe_ticket_id}")

    current_number = str(row.get("ticket_number") or "").strip()
    if current_number:
        return current_number

    seq = _counter_next("ticket_number:CS-2026")
    next_number = f"CS-2026-{seq:03d}"
    now = _now_kst_naive()

    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                """
                UPDATE cstrike.tickets
                SET ticket_number=%s,
                    type=%s,
                    updated_at=%s
                WHERE id=%s
                """,
                (next_number, str(category or "").strip() or "pending", now, safe_ticket_id),
            )
        con.commit()
    finally:
        con.close()
    return next_number


def store_ticket(
    ticket_id: str,
    ticket_number: str,
    discord_thread_id: str,
    user_id: str,
    category: str,
    user_name: str | None = None,
    team_id: str | None = None,
    team_name: str | None = None,
) -> str:
    now = _now_kst_naive()
    safe_ticket_id = str(ticket_id or "").strip()
    if not safe_ticket_id:
        raise ValueError("ticket_id is required")
    safe_ticket_number = str(ticket_number or "").strip()
    if not safe_ticket_number:
        raise ValueError("ticket_number is required")
    safe_category = str(category or "").strip() or "pending"
    display_name = str(user_name or "").strip()
    safe_team_id = str(team_id or "").strip() or None
    safe_team_name = str(team_name or "").strip() or None
    title = f"[{safe_category}] {display_name}" if display_name else f"[{safe_category}] ticket"
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cstrike.tickets (
                    id, ticket_number, type, title, description,
                    team_id, team_name, reporter_type, reporter_id,
                    discord_ticket_id, discord_channel_id, status, priority,
                    assigned_to, resolution, resolved_by, resolved_at,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    ticket_number = EXCLUDED.ticket_number,
                    type = EXCLUDED.type,
                    title = EXCLUDED.title,
                    reporter_id = EXCLUDED.reporter_id,
                    discord_channel_id = EXCLUDED.discord_channel_id,
                    status = CASE WHEN cstrike.tickets.status='closed' THEN cstrike.tickets.status ELSE EXCLUDED.status END,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    safe_ticket_id,
                    safe_ticket_number,
                    safe_category,
                    title,
                    "",
                    safe_team_id,
                    safe_team_name,
                    "discord",
                    str(user_id or "").strip() or None,
                    None,
                    str(discord_thread_id or "").strip() or None,
                    "open",
                    "medium",
                    None,
                    None,
                    None,
                    None,
                    now,
                    now,
                ),
            )
        con.commit()
    finally:
        con.close()
    return safe_ticket_id


def update_ticket_category(ticket_id: str, category: str):
    now = _now_kst_naive()
    safe_category = str(category or "").strip() or "pending"
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                """
                UPDATE cstrike.tickets
                SET type=%s,
                    title='[' || %s || '] ticket',
                    updated_at=%s
                WHERE id=%s
                """,
                (safe_category, safe_category, now, str(ticket_id)),
            )
        con.commit()
    finally:
        con.close()


def update_ticket_submission(
    ticket_id: str,
    category: str,
    description: str,
    title: str | None = None,
    team_id: str | None = None,
    team_name: str | None = None,
) -> None:
    now = _now_kst_naive()
    safe_category = str(category or "").strip() or "pending"
    safe_description = str(description or "").strip()
    safe_title = str(title or "").strip() or f"[{safe_category}] ticket"
    safe_team_id = str(team_id or "").strip() or None
    safe_team_name = str(team_name or "").strip() or None
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                """
                UPDATE cstrike.tickets
                SET type=%s,
                    title=%s,
                    description=%s,
                    team_id=%s,
                    team_name=%s,
                    updated_at=%s
                WHERE id=%s
                """,
                (
                    safe_category,
                    safe_title,
                    safe_description,
                    safe_team_id,
                    safe_team_name,
                    now,
                    str(ticket_id),
                ),
            )
        con.commit()
    finally:
        con.close()


def close_ticket(ticket_id: str, status: str = "closed"):
    now = _now_kst_naive()
    target_status = str(status or "closed").strip() or "closed"
    resolved_at = now if target_status in {"closed", "resolved", "done"} else None
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                """
                UPDATE cstrike.tickets
                SET status=%s,
                    resolved_at=COALESCE(resolved_at, %s),
                    updated_at=%s
                WHERE id=%s
                """,
                (target_status, resolved_at, now, str(ticket_id)),
            )
        con.commit()
    finally:
        con.close()


def set_ticket_approval(
    ticket_id: str,
    isAprove: bool,
    rejection_reason: str | None = None,
    resolved_by: str | None = None,
):
    now = _now_kst_naive()
    normalized_reason = None if isAprove else (str(rejection_reason or "").strip() or None)
    next_status = "approved" if isAprove else "rejected"
    resolver = str(resolved_by or "").strip() or None
    if resolver is not None:
        try:
            resolver = str(UUID(resolver))
        except (TypeError, ValueError):
            resolver = None
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                """
                UPDATE cstrike.tickets
                SET status=%s,
                    resolution=%s,
                    resolved_by=%s,
                    resolved_at=%s,
                    updated_at=%s
                WHERE id=%s
                """,
                (next_status, normalized_reason, resolver, now, now, str(ticket_id)),
            )
        con.commit()
    finally:
        con.close()


def get_open_ticket_by_user(user_id: str) -> dict[str, Any] | None:
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM cstrike.tickets
                WHERE reporter_id=%s AND status='open'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (str(user_id),),
            )
            row = cur.fetchone()
    finally:
        con.close()

    if not row:
        return None
    return _serialize_times(_normalize_ticket_row(row))


def get_open_tickets_by_user(user_id: str) -> list[dict[str, Any]]:
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM cstrike.tickets
                WHERE reporter_id=%s AND status='open'
                ORDER BY created_at DESC
                """,
                (str(user_id),),
            )
            rows = cur.fetchall() or []
    finally:
        con.close()

    return [_serialize_times(_normalize_ticket_row(row)) for row in rows]


def get_open_ticket_by_thread_id(discord_thread_id: str) -> dict[str, Any] | None:
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM cstrike.tickets
                WHERE discord_channel_id=%s AND status='open'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (str(discord_thread_id),),
            )
            row = cur.fetchone()
    finally:
        con.close()

    if not row:
        return None
    return _serialize_times(_normalize_ticket_row(row))


def get_open_tickets_by_thread_id(discord_thread_id: str) -> list[dict[str, Any]]:
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM cstrike.tickets
                WHERE discord_channel_id=%s AND status='open'
                ORDER BY created_at DESC
                """,
                (str(discord_thread_id),),
            )
            rows = cur.fetchall() or []
    finally:
        con.close()
    return [_serialize_times(_normalize_ticket_row(row)) for row in rows]


def get_latest_ticket_by_thread_id(discord_thread_id: str) -> dict[str, Any] | None:
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM cstrike.tickets
                WHERE discord_channel_id=%s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (str(discord_thread_id),),
            )
            row = cur.fetchone()
    finally:
        con.close()

    if not row:
        return None
    return _serialize_times(_normalize_ticket_row(row))


def get_ticket(ticket_id: str) -> dict[str, Any] | None:
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute("SELECT * FROM cstrike.tickets WHERE id=%s", (str(ticket_id),))
            row = cur.fetchone()
    finally:
        con.close()

    if not row:
        return None
    return _serialize_times(_normalize_ticket_row(row))


def set_ticket_notifier(guild_id: str, user_id: str, user_name: str | None = None) -> None:
    now = _now_kst_naive()
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cstrike.ticket_settings (guild_id, notifier_user_id, notifier_user_name, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (guild_id) DO UPDATE SET
                    notifier_user_id = EXCLUDED.notifier_user_id,
                    notifier_user_name = EXCLUDED.notifier_user_name,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    str(guild_id),
                    str(user_id),
                    (str(user_name).strip() if user_name else None),
                    now,
                    now,
                ),
            )
        con.commit()
    finally:
        con.close()


def get_ticket_notifier(guild_id: str) -> dict[str, Any] | None:
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                "SELECT * FROM cstrike.ticket_settings WHERE guild_id=%s LIMIT 1",
                (str(guild_id),),
            )
            row = cur.fetchone()
    finally:
        con.close()
    if not row:
        return None
    return row


__all__ = [
    "next_ticket_sequence",
    "allocate_ticket_identity",
    "assign_ticket_number_for_category",
    "store_ticket",
    "update_ticket_category",
    "update_ticket_submission",
    "close_ticket",
    "set_ticket_approval",
    "get_open_ticket_by_user",
    "get_open_tickets_by_user",
    "get_open_ticket_by_thread_id",
    "get_open_tickets_by_thread_id",
    "get_latest_ticket_by_thread_id",
    "get_ticket",
    "set_ticket_notifier",
    "get_ticket_notifier",
]
