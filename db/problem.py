"""Problem-domain helpers (PostgreSQL).

운영포털 통합을 위해 외주 MySQL 쿼리를 PostgreSQL로 포팅.
- 테이블: cstrike.problem (infra/db/init/02-discord-bot-tables.sql 에 이미 정의됨)
- `ON DUPLICATE KEY UPDATE` → `ON CONFLICT (problem_no) DO UPDATE SET`
- `CURRENT_TIMESTAMP(6)` / `DATETIME(6)` → PostgreSQL `timestamptz` + `now()`
- 백틱(`) 및 `KEY ix_...` 구문은 PostgreSQL에서 `CREATE INDEX`로 분리 (init SQL에서 처리)
- 함수 시그니처는 외주 cogs/가 import하므로 유지
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .connection import _connect, _now_kst_naive


def _normalize_row(row: dict[str, Any] | None) -> dict[str, Any] | None:
    if not row:
        return None
    payload = dict(row)
    for key in ("created_at", "updated_at", "release_at", "hint_announce_at", "release_notified_at"):
        value = payload.get(key)
        if isinstance(value, datetime):
            payload[key] = value.isoformat(sep=" ")
    return payload


def ensure_problem_table() -> None:
    """cstrike.problem 테이블은 infra/db/init/02-discord-bot-tables.sql 에서 생성.

    외주 호환 유지를 위해 함수 시그니처는 그대로 두되, 실제 DDL은 init SQL이 담당하므로 no-op.
    운영포털 부트스트랩 시점에 테이블이 존재함을 보장.
    """
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute("ALTER TABLE cstrike.problem ADD COLUMN IF NOT EXISTS release_at timestamptz NULL")
            cur.execute("ALTER TABLE cstrike.problem ADD COLUMN IF NOT EXISTS hint_announce_at timestamptz NULL")
            cur.execute("ALTER TABLE cstrike.problem ADD COLUMN IF NOT EXISTS assigned_team_id uuid NULL")
            cur.execute("ALTER TABLE cstrike.problem ADD COLUMN IF NOT EXISTS release_channel_id varchar(64) NULL")
            cur.execute("ALTER TABLE cstrike.problem ADD COLUMN IF NOT EXISTS release_notified_at timestamptz NULL")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS cstrike.problem_team_links (
                  id bigserial PRIMARY KEY,
                  problem_no varchar(64) NOT NULL
                    REFERENCES cstrike.problem(problem_no) ON DELETE CASCADE,
                  team_role_id varchar(64) NOT NULL,
                  download_url varchar(2048) NULL,
                  access_url varchar(2048) NULL,
                  created_at timestamptz NOT NULL DEFAULT now(),
                  updated_at timestamptz NOT NULL DEFAULT now(),
                  CONSTRAINT uq_problem_team_links_problem_role
                    UNIQUE (problem_no, team_role_id)
                )
                """
            )
            cur.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_problem_team_links_problem
                  ON cstrike.problem_team_links (problem_no, team_role_id)
                """
            )
        con.commit()
    finally:
        con.close()


def create_problem(
    *,
    problem_no: str,
    title: str,
    category: str = "MISC",
    score: int = 0,
    difficulty: str = "Easy",
    description: str | None = None,
) -> dict[str, Any]:
    no = str(problem_no or "").strip().upper()
    if not no:
        raise ValueError("problem_no is required")
    now = _now_kst_naive()
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cstrike.problem (
                    problem_no, title, category, score, difficulty, description,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (problem_no) DO UPDATE SET
                    title = EXCLUDED.title,
                    category = EXCLUDED.category,
                    score = EXCLUDED.score,
                    difficulty = EXCLUDED.difficulty,
                    description = EXCLUDED.description,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    no,
                    str(title or "").strip() or no,
                    str(category or "MISC").strip().upper(),
                    int(score),
                    str(difficulty or "Easy").strip() or "Easy",
                    (str(description).strip() if description is not None else None),
                    now,
                    now,
                ),
            )
        con.commit()
    finally:
        con.close()
    return get_problem_by_no(no) or {}


def list_problems(limit: int = 500, include_unreleased_bonus: bool = True) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 2000))
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                """
                SELECT *
                FROM cstrike.problem
                ORDER BY category ASC, score ASC, problem_no ASC
                LIMIT %s
                """,
                (safe_limit,),
            )
            rows = cur.fetchall() or []
    finally:
        con.close()
    return [_normalize_row(row) or {} for row in rows]


def get_problem_by_no(problem_no: str) -> dict[str, Any] | None:
    no = str(problem_no or "").strip().upper()
    if not no:
        return None
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                "SELECT * FROM cstrike.problem WHERE problem_no=%s LIMIT 1",
                (no,),
            )
            row = cur.fetchone()
    finally:
        con.close()
    return _normalize_row(row)


def list_visible_problems(*, team_id: str | None, now: datetime | None = None, limit: int = 500) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 2000))
    current = now or _now_kst_naive()
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                """
                SELECT p.*
                FROM cstrike.problem p
                WHERE p.release_at IS NULL OR p.release_at <= %s
                ORDER BY p.category ASC, p.score ASC, p.problem_no ASC
                LIMIT %s
                """,
                (current, safe_limit),
            )
            rows = cur.fetchall() or []
    finally:
        con.close()
    return [_normalize_row(row) or {} for row in rows]


def get_visible_problem_by_no(*, problem_no: str, team_id: str | None, now: datetime | None = None) -> dict[str, Any] | None:
    no = str(problem_no or "").strip().upper()
    if not no:
        return None
    current = now or _now_kst_naive()
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                """
                SELECT p.*
                FROM cstrike.problem p
                WHERE p.problem_no = %s
                  AND (p.release_at IS NULL OR p.release_at <= %s)
                LIMIT 1
                """,
                (no, current),
            )
            row = cur.fetchone()
    finally:
        con.close()
    return _normalize_row(row)


def list_problem_team_links(problem_no: str) -> list[dict[str, Any]]:
    no = str(problem_no or "").strip().upper()
    if not no:
        return []
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                """
                SELECT
                    ptl.id,
                    t.id::text AS team_id,
                    t.name AS team_name,
                    t.team_code,
                    t.discord_role_id AS team_role_id,
                    ptl.download_url,
                    ptl.access_url
                FROM cstrike.teams t
                LEFT JOIN cstrike.problem_team_links ptl
                  ON ptl.problem_no=%s
                 AND ptl.team_role_id=t.discord_role_id
                WHERE t.status <> 'disqualified'
                  AND COALESCE(t.discord_role_id, '') <> ''
                ORDER BY t.name ASC, t.team_code ASC, t.id ASC
                """,
                (no,),
            )
            rows = cur.fetchall() or []
    finally:
        con.close()
    return [dict(row) for row in rows]


def upsert_problem_team_link(
    *,
    problem_no: str,
    team_role_id: str,
    download_url: str | None = None,
    access_url: str | None = None,
) -> dict[str, Any]:
    no = str(problem_no or "").strip().upper()
    role_id = str(team_role_id or "").strip()
    if not no:
        raise ValueError("problem_no is required")
    if not role_id:
        raise ValueError("team_role_id is required")
    now = _now_kst_naive()
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                """
                INSERT INTO cstrike.problem_team_links (
                    problem_no, team_role_id, download_url, access_url, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (problem_no, team_role_id) DO UPDATE SET
                    download_url = EXCLUDED.download_url,
                    access_url = EXCLUDED.access_url,
                    updated_at = EXCLUDED.updated_at
                RETURNING *
                """,
                (
                    no,
                    role_id,
                    str(download_url).strip() if download_url is not None else None,
                    str(access_url).strip() if access_url is not None else None,
                    now,
                    now,
                ),
            )
            row = cur.fetchone()
        con.commit()
    finally:
        con.close()
    return _normalize_row(row) or {}


__all__ = [
    "ensure_problem_table",
    "create_problem",
    "list_problems",
    "list_visible_problems",
    "get_problem_by_no",
    "get_visible_problem_by_no",
    "list_problem_team_links",
    "upsert_problem_team_link",
]
