"""Team-domain helpers (PostgreSQL).

운영포털 통합:
- `cstrike.teams`, `cstrike.team_members`는 **SELECT only** (SOURCE OF TRUTH는 운영포털)
- 쓰기가 필요한 경우 운영포털 REST API(`POST /api/v1/competitions/{cid}/teams` 등)를 호출하거나
  본 모듈의 쓰기 스텁은 `NotImplementedError`로 차단
- 외주 신규 테이블(team_announcements / team_hints / team_countdown_schedules /
  team_notification_events)은 MySQL→PostgreSQL 변환 후 INSERT/UPDATE 허용
- 모든 쿼리는 `cstrike.` prefix 명시 (search_path 의존 최소화)

포팅 규칙 (MySQL → PostgreSQL):
- `ON DUPLICATE KEY UPDATE` → `ON CONFLICT (...) DO UPDATE SET`
- `INSERT IGNORE` → `INSERT ... ON CONFLICT DO NOTHING`
- `cursor.lastrowid` / `LAST_INSERT_ID()` → `INSERT ... RETURNING id` + `cur.fetchone()["id"]`
- `CURRENT_TIMESTAMP(6)` → `now()`
- 백틱(`) 제거
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .connection import _connect, _now_kst_naive


def _norm_competition_id(competition_id: str | None) -> str | None:
    value = str(competition_id or "").strip()
    return value or None


def _team_row_to_payload(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    payload = dict(row)
    payload["id"] = str(payload.get("id"))
    for key in ("registered_at", "approved_at", "created_at", "updated_at"):
        value = payload.get(key)
        if isinstance(value, datetime):
            payload[key] = value.isoformat()
    return payload


# ============================================================
# A) cstrike.teams / cstrike.team_members SELECT 전용 헬퍼
# ============================================================

def get_team_by_id(team_id: str) -> dict[str, Any] | None:
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                "SELECT * FROM cstrike.teams WHERE id=%s LIMIT 1",
                (team_id,),
            )
            row = cur.fetchone()
    finally:
        con.close()
    return _team_row_to_payload(row)


def list_teams(competition_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
    comp = _norm_competition_id(competition_id)
    safe_limit = max(1, min(int(limit), 500))
    con = _connect()
    try:
        with con.cursor() as cur:
            if comp is None:
                cur.execute(
                    """
                    SELECT
                        t.*,
                        COUNT(m.id) AS member_count
                    FROM cstrike.teams t
                    LEFT JOIN cstrike.team_members m
                      ON m.team_id=t.id AND m.status='approved'
                    WHERE t.status <> 'disqualified'
                    GROUP BY t.id
                    ORDER BY t.created_at DESC, t.id DESC
                    LIMIT %s
                    """,
                    (safe_limit,),
                )
            else:
                cur.execute(
                    """
                    SELECT
                        t.*,
                        COUNT(m.id) AS member_count
                    FROM cstrike.teams t
                    LEFT JOIN cstrike.team_members m
                      ON m.team_id=t.id AND m.status='approved'
                    WHERE t.status <> 'disqualified' AND t.competition_id=%s
                    GROUP BY t.id
                    ORDER BY t.created_at DESC, t.id DESC
                    LIMIT %s
                    """,
                    (comp, safe_limit),
                )
            rows = cur.fetchall() or []
    finally:
        con.close()
    payloads: list[dict[str, Any]] = []
    for row in rows:
        item = _team_row_to_payload(row) or {}
        item["member_count"] = int(row.get("member_count", 0) or 0)
        payloads.append(item)
    return payloads


def get_user_team(discord_user_id: str, competition_id: str | None = None) -> dict[str, Any] | None:
    user_id = str(discord_user_id or "").strip()
    comp = _norm_competition_id(competition_id)
    con = _connect()
    try:
        with con.cursor() as cur:
            if comp is None:
                cur.execute(
                    """
                    SELECT t.*, tm.role AS member_role, tm.status AS member_status
                    FROM cstrike.team_members tm
                    JOIN cstrike.teams t ON t.id=tm.team_id
                    WHERE tm.discord_user_id=%s
                      AND tm.status='approved'
                      AND t.status <> 'disqualified'
                    ORDER BY COALESCE(tm.created_at, tm.joined_at) DESC
                    LIMIT 1
                    """,
                    (user_id,),
                )
            else:
                cur.execute(
                    """
                    SELECT t.*, tm.role AS member_role, tm.status AS member_status
                    FROM cstrike.team_members tm
                    JOIN cstrike.teams t ON t.id=tm.team_id
                    WHERE tm.discord_user_id=%s
                      AND tm.status='approved'
                      AND t.status <> 'disqualified'
                      AND t.competition_id=%s
                    ORDER BY COALESCE(tm.created_at, tm.joined_at) DESC
                    LIMIT 1
                    """,
                    (user_id, comp),
                )
            row = cur.fetchone()
    finally:
        con.close()
    return _team_row_to_payload(row)


def get_team_detail(team_id: str) -> dict[str, Any] | None:
    team = get_team_by_id(team_id)
    if team is None:
        return None
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                """
                SELECT
                    tm.discord_user_id,
                    tm.discord_username,
                    tm.role,
                    tm.status,
                    0 AS personal_score,
                    tm.joined_at,
                    tm.created_at
                FROM cstrike.team_members tm
                WHERE tm.team_id=%s
                ORDER BY
                    CASE tm.role WHEN 'captain' THEN 0 ELSE 1 END,
                    CASE tm.status WHEN 'approved' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END,
                    tm.discord_user_id ASC
                """,
                (str(team_id),),
            )
            members = cur.fetchall() or []
    finally:
        con.close()

    normalized_members: list[dict[str, Any]] = []
    approved_count = 0
    for member in members:
        status = str(member.get("status", "")).lower()
        if status == "approved":
            approved_count += 1
        item = dict(member)
        for key in ("joined_at", "created_at"):
            value = item.get(key)
            if isinstance(value, datetime):
                item[key] = value.isoformat()
        normalized_members.append(item)
    team["members"] = normalized_members
    team["member_count"] = approved_count
    return team


def get_team_by_role(guild_id: str, discord_role_id: str) -> dict[str, Any] | None:
    role_id = str(discord_role_id or "").strip()
    if not role_id:
        return None
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                """
                SELECT
                    t.*
                FROM cstrike.teams t
                WHERE t.discord_role_id=%s
                  AND t.status <> 'disqualified'
                LIMIT 1
                """,
                (role_id,),
            )
            row = cur.fetchone()
    finally:
        con.close()
    return _team_row_to_payload(row)


def get_team_role_mapping(guild_id: str, team_id: str) -> dict[str, Any] | None:
    normalized_team_id = str(team_id or "").strip()
    if not normalized_team_id:
        return None
    return get_team_by_id(normalized_team_id)


# ============================================================
# B) cstrike.teams 쓰기 금지 — 운영포털 REST API 경유 필수
# ============================================================

def upsert_team_discord_role(
    *,
    team_id: str,
    guild_id: str,
    discord_role_id: str,
    discord_role_name: str | None,
    created_by_id: str | None,
    created_by_name: str | None,
) -> dict[str, Any]:
    """운영포털 SOURCE OF TRUTH 원칙: cstrike.teams.discord_role_id 쓰기는 운영포털 REST API 경유.

    외주 cogs(`cogs/team.py`)가 봇 단에서 Discord role을 생성한 뒤 DB에 기록할 때
    이 함수를 호출하지만, 운영포털 통합 환경에서는 cstrike.teams를 봇이 직접 수정하면
    운영포털 상태와 어긋나므로 금지.

    대안: 봇 → 운영포털 REST 호출로 `PATCH /api/v1/teams/{team_id}` 등을 사용하거나
          관리자 화면에서 discord_role_id를 등록.
    """
    raise NotImplementedError(
        "teams.discord_role_id 쓰기는 운영포털 REST API를 사용하세요. "
        "본 모듈은 cstrike.teams / cstrike.team_members에 대해 SELECT only."
    )


# ============================================================
# C) 외주 신규 테이블 (INSERT/UPDATE 허용, PostgreSQL 문법)
#    team_announcements / team_hints / team_countdown_schedules /
#    team_notification_events
# ============================================================

def _row_to_payload(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    payload = dict(row)
    for key in (
        "scheduled_at",
        "start_at",
        "end_at",
        "sent_at",
        "started_at",
        "ended_at",
        "created_at",
        "updated_at",
    ):
        value = payload.get(key)
        if isinstance(value, datetime):
            payload[key] = value.isoformat(sep=" ")
    return payload


_ALLOWED_SCHEDULE_TABLES = {
    "team_announcements",
    "team_hints",
    "team_countdown_schedules",
}


def _qualified_schedule_table(table: str) -> str:
    """외주가 넘기는 raw 테이블명에 cstrike. prefix를 붙이고 whitelist 검증."""
    name = str(table or "").strip()
    if name not in _ALLOWED_SCHEDULE_TABLES:
        raise ValueError(f"Unsupported schedule table: {name}")
    return f"cstrike.{name}"


def _insert_schedule(table: str, fields: dict[str, Any]) -> int:
    qualified = _qualified_schedule_table(table)
    now = _now_kst_naive()
    values = dict(fields)
    values["created_at"] = now
    values["updated_at"] = now
    columns = list(values)
    placeholders = ", ".join(["%s"] * len(columns))
    column_sql = ", ".join(columns)
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                f"INSERT INTO {qualified} ({column_sql}) VALUES ({placeholders}) RETURNING id",
                tuple(values[column] for column in columns),
            )
            inserted = cur.fetchone() or {}
            row_id = int(inserted.get("id") or 0)
        con.commit()
        if row_id <= 0:
            raise RuntimeError(f"Failed to insert into {qualified}: RETURNING id empty")
        return row_id
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def create_team_announcement(**fields: Any) -> int:
    return _insert_schedule("team_announcements", fields)


def create_team_hint(**fields: Any) -> int:
    return _insert_schedule("team_hints", fields)


def create_team_countdown_schedule(**fields: Any) -> int:
    return _insert_schedule("team_countdown_schedules", fields)


def _list_table(table: str, guild_id: str, limit: int = 50) -> list[dict[str, Any]]:
    qualified = _qualified_schedule_table(table)
    safe_limit = max(1, min(int(limit), 200))
    order_expr = (
        "COALESCE(s.start_at, s.end_at, s.created_at)"
        if table == "team_countdown_schedules"
        else "COALESCE(s.scheduled_at, s.created_at)"
    )
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                f"""
                SELECT s.*, t.name AS team_name, t.team_code
                FROM {qualified} s
                LEFT JOIN cstrike.teams t ON t.id::text=s.team_id
                WHERE s.guild_id=%s
                ORDER BY
                    CASE s.status
                        WHEN 'scheduled' THEN 0
                        WHEN 'failed' THEN 1
                        WHEN 'sent' THEN 2
                        WHEN 'deleted' THEN 3
                        ELSE 4
                    END,
                    {order_expr} ASC,
                    s.id ASC
                LIMIT %s
                """,
                (str(guild_id), safe_limit),
            )
            rows = cur.fetchall() or []
    finally:
        con.close()
    return [_row_to_payload(row) or {} for row in rows]


def list_team_announcements(guild_id: str, limit: int = 50) -> list[dict[str, Any]]:
    return _list_table("team_announcements", guild_id, limit)


def list_team_hints(guild_id: str, limit: int = 50) -> list[dict[str, Any]]:
    return _list_table("team_hints", guild_id, limit)


def list_team_countdown_schedules(guild_id: str, limit: int = 50) -> list[dict[str, Any]]:
    return _list_table("team_countdown_schedules", guild_id, limit)


def _row_by_number(table: str, guild_id: str, number: int) -> dict[str, Any] | None:
    rows = _list_table(table, guild_id, limit=200)
    idx = int(number) - 1
    if idx < 0 or idx >= len(rows):
        return None
    return rows[idx]


def get_team_announcement_by_number(guild_id: str, number: int) -> dict[str, Any] | None:
    return _row_by_number("team_announcements", guild_id, number)


def get_team_hint_by_number(guild_id: str, number: int) -> dict[str, Any] | None:
    return _row_by_number("team_hints", guild_id, number)


def get_team_countdown_by_number(guild_id: str, number: int) -> dict[str, Any] | None:
    return _row_by_number("team_countdown_schedules", guild_id, number)


def soft_delete_schedule(table: str, guild_id: str, row_id: int) -> bool:
    qualified = _qualified_schedule_table(table)
    now = _now_kst_naive()
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {qualified}
                SET status='deleted', updated_at=%s
                WHERE guild_id=%s AND id=%s AND status='scheduled'
                """,
                (now, str(guild_id), int(row_id)),
            )
            changed = cur.rowcount > 0
        con.commit()
        return changed
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def list_due_team_announcements(now: datetime, limit: int = 20) -> list[dict[str, Any]]:
    return _list_due("team_announcements", "scheduled_at", now, limit)


def list_due_team_hints(now: datetime, limit: int = 20) -> list[dict[str, Any]]:
    return _list_due("team_hints", "scheduled_at", now, limit)


def _list_due(table: str, column: str, now: datetime, limit: int) -> list[dict[str, Any]]:
    qualified = _qualified_schedule_table(table)
    # 컬럼명은 whitelist에서만 허용 (SQL 인젝션 방지)
    if column not in {"scheduled_at", "start_at", "end_at"}:
        raise ValueError(f"Unsupported due column: {column}")
    safe_limit = max(1, min(int(limit), 100))
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                f"""
                SELECT s.*, t.name AS team_name, t.team_code
                FROM {qualified} s
                LEFT JOIN cstrike.teams t ON t.id::text=s.team_id
                WHERE s.status='scheduled'
                  AND s.{column} IS NOT NULL
                  AND s.{column} <= %s
                ORDER BY s.{column} ASC, s.id ASC
                LIMIT %s
                """,
                (now, safe_limit),
            )
            rows = cur.fetchall() or []
    finally:
        con.close()
    return [_row_to_payload(row) or {} for row in rows]


def list_due_team_countdowns(now: datetime, limit: int = 20) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 100))
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                """
                SELECT s.*, t.name AS team_name, t.team_code
                FROM cstrike.team_countdown_schedules s
                LEFT JOIN cstrike.teams t ON t.id::text=s.team_id
                WHERE s.status='scheduled'
                  AND (
                    (s.start_at IS NOT NULL AND s.started_at IS NULL AND s.start_at <= %s)
                    OR
                    (s.end_at IS NOT NULL AND s.ended_at IS NULL AND s.end_at <= %s)
                  )
                ORDER BY COALESCE(s.start_at, s.end_at) ASC, s.id ASC
                LIMIT %s
                """,
                (now, now, safe_limit),
            )
            rows = cur.fetchall() or []
    finally:
        con.close()
    return [_row_to_payload(row) or {} for row in rows]


def mark_schedule_sent(table: str, row_id: int) -> None:
    qualified = _qualified_schedule_table(table)
    now = _now_kst_naive()
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {qualified}
                SET status='sent', sent_at=%s, updated_at=%s, error_message=NULL
                WHERE id=%s
                """,
                (now, now, int(row_id)),
            )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def mark_schedule_failed(table: str, row_id: int, error_message: str) -> None:
    qualified = _qualified_schedule_table(table)
    now = _now_kst_naive()
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {qualified}
                SET status='failed', error_message=%s, updated_at=%s
                WHERE id=%s
                """,
                (str(error_message or "")[:2000], now, int(row_id)),
            )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def mark_countdown_started(row_id: int) -> None:
    now = _now_kst_naive()
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                """
                UPDATE cstrike.team_countdown_schedules
                SET started_at=%s,
                    status=CASE WHEN end_at IS NULL THEN 'sent' ELSE status END,
                    updated_at=%s
                WHERE id=%s AND started_at IS NULL
                """,
                (now, now, int(row_id)),
            )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def mark_countdown_ended(row_id: int) -> None:
    now = _now_kst_naive()
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                """
                UPDATE cstrike.team_countdown_schedules
                SET ended_at=%s,
                    status='sent',
                    updated_at=%s,
                    error_message=NULL
                WHERE id=%s AND ended_at IS NULL
                """,
                (now, now, int(row_id)),
            )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def create_team_notification_event(
    *,
    event_key: str,
    guild_id: str,
    team_id: str | None,
    discord_role_id: str,
    title: str,
    description: str,
    severity: str | None,
    status: str,
    error_message: str | None = None,
) -> tuple[bool, dict[str, Any]]:
    """event_key 유니크 제약을 이용한 중복 방지 INSERT.

    MySQL 원본: `INSERT IGNORE` → PostgreSQL: `ON CONFLICT (event_key) DO NOTHING`.
    생성 여부는 `cur.rowcount`로 판단 (INSERT가 실제로 row를 남겼으면 1).
    """
    now = _now_kst_naive()
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cstrike.team_notification_events (
                    event_key, guild_id, team_id, discord_role_id, title,
                    description, severity, status, error_message, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (event_key) DO NOTHING
                """,
                (
                    str(event_key),
                    str(guild_id),
                    str(team_id or "").strip() or None,
                    str(discord_role_id),
                    str(title),
                    str(description),
                    str(severity or "").strip() or None,
                    str(status),
                    error_message,
                    now,
                ),
            )
            created = cur.rowcount > 0
            cur.execute(
                "SELECT * FROM cstrike.team_notification_events WHERE event_key=%s LIMIT 1",
                (str(event_key),),
            )
            row = cur.fetchone() or {}
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
    return created, _row_to_payload(row) or {}


def update_team_notification_event(event_key: str, *, status: str, error_message: str | None = None) -> None:
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                """
                UPDATE cstrike.team_notification_events
                SET status=%s, error_message=%s
                WHERE event_key=%s
                """,
                (str(status), error_message, str(event_key)),
            )
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


__all__ = [
    "get_team_by_id",
    "get_team_by_role",
    "get_team_role_mapping",
    "list_teams",
    "get_user_team",
    "get_team_detail",
    "upsert_team_discord_role",
    "create_team_announcement",
    "create_team_hint",
    "create_team_countdown_schedule",
    "list_team_announcements",
    "list_team_hints",
    "list_team_countdown_schedules",
    "get_team_announcement_by_number",
    "get_team_hint_by_number",
    "get_team_countdown_by_number",
    "soft_delete_schedule",
    "list_due_team_announcements",
    "list_due_team_hints",
    "list_due_team_countdowns",
    "mark_schedule_sent",
    "mark_schedule_failed",
    "mark_countdown_started",
    "mark_countdown_ended",
    "create_team_notification_event",
    "update_team_notification_event",
]
