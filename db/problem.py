"""Problem-domain helpers (PostgreSQL).

운영포털 통합을 위해 외주 MySQL 쿼리를 PostgreSQL로 포팅.
- 테이블: cstrike.problem (infra/db/init/02-discord-bot-tables.sql 에 이미 정의됨)
- `ON DUPLICATE KEY UPDATE` → `ON CONFLICT (problem_no) DO UPDATE SET`
- `CURRENT_TIMESTAMP(6)` / `DATETIME(6)` → PostgreSQL `timestamptz` + `now()`
- 백틱(`) 및 `KEY ix_...` 구문은 PostgreSQL에서 `CREATE INDEX`로 분리 (init SQL에서 처리)
- 함수 시그니처는 외주 cogs/가 import하므로 유지
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
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


def _coerce_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def get_problem_visibility_state(
    *,
    competition_id: str | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not competition_id:
        return {"visible": True, "reason": None}

    current_time = now or datetime.now(timezone.utc)
    con = _connect()
    try:
        with con.cursor() as cur:
            cur.execute(
                """
                SELECT status, scheduled_start_at, scheduled_end_at
                FROM cstrike.competitions
                WHERE id = %s
                LIMIT 1
                """,
                (str(competition_id),),
            )
            row = cur.fetchone()
    finally:
        con.close()

    if not row:
        return {"visible": False, "reason": "대회 정보를 찾을 수 없습니다."}

    status = str(row.get("status") or "").strip().lower()
    scheduled_start_at = _coerce_utc(row.get("scheduled_start_at"))
    scheduled_end_at = _coerce_utc(row.get("scheduled_end_at"))

    if scheduled_start_at and current_time < scheduled_start_at:
        return {"visible": False, "reason": "대회 시작 전이라 문제 목록을 공개하지 않습니다."}
    if scheduled_end_at and current_time >= scheduled_end_at:
        return {"visible": False, "reason": "대회 시간이 종료되어 문제 목록을 공개하지 않습니다."}
    if status not in {"running", "paused"}:
        return {"visible": False, "reason": "현재 대회가 진행 중이 아니어서 문제 목록을 공개하지 않습니다."}
    return {"visible": True, "reason": None}


def list_visible_problems(
    *,
    team_id: str | None,
    competition_id: str | None = None,
    now: datetime | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    visibility = get_problem_visibility_state(competition_id=competition_id, now=now)
    if not visibility["visible"]:
        return []
    safe_limit = max(1, min(int(limit), 2000))
    catalog = _list_deployed_problem_catalog(team_id=team_id, competition_id=competition_id, limit=safe_limit)
    return [_normalize_row(row) or {} for row in catalog]


def get_visible_problem_by_no(
    *,
    problem_no: str,
    team_id: str | None,
    competition_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    visibility = get_problem_visibility_state(competition_id=competition_id, now=now)
    if not visibility["visible"]:
        return None
    no = str(problem_no or "").strip().upper()
    if not no:
        return None
    for row in _list_deployed_problem_catalog(team_id=team_id, competition_id=competition_id, limit=2000):
        if str(row.get("problem_no") or "").strip().upper() == no:
            return _normalize_row(row)
    return None


def list_problem_team_links(problem_no: str, competition_id: str | None = None) -> list[dict[str, Any]]:
    no = str(problem_no or "").strip().upper()
    if not no:
        return []
    catalog = _list_deployed_problem_catalog(team_id=None, competition_id=competition_id, limit=2000)
    problem = next(
        (item for item in catalog if str(item.get("problem_no") or "").strip().upper() == no),
        None,
    )
    if problem is None:
        return []
    service_id = str(problem.get("service_id") or "").strip()
    if not service_id:
        return []
    con = _connect()
    try:
        with con.cursor() as cur:
            params: list[object] = [service_id]
            competition_filter = ""
            if competition_id:
                competition_filter = "AND t.competition_id = %s"
                params.append(str(competition_id))
            cur.execute(
                f"""
                SELECT
                    t.id::text AS team_id,
                    t.name AS team_name,
                    t.team_code,
                    t.discord_role_id AS team_role_id,
                    NULL::text AS download_url,
                    CASE
                      WHEN ts.port = 80 THEN format('http://%%s/', ts.host_ip)
                      WHEN ts.port = 443 THEN format('https://%%s/', ts.host_ip)
                      ELSE format('http://%%s:%%s/', ts.host_ip, ts.port)
                    END AS access_url
                FROM cstrike.team_services ts
                JOIN cstrike.teams t ON t.id = ts.team_id
                WHERE ts.service_id = %s
                  AND ts.status = 'running'
                  AND t.status <> 'disqualified'
                  AND COALESCE(t.discord_role_id, '') <> ''
                  {competition_filter}
                ORDER BY t.name ASC, t.team_code ASC, t.id ASC, ts.id ASC
                """,
                tuple(params),
            )
            rows = cur.fetchall() or []
    finally:
        con.close()
    return [dict(row) for row in rows]


def _problem_prefix(category: object) -> str:
    raw = "".join(ch for ch in str(category or "MISC").upper() if ch.isalnum())
    if not raw:
        return "MISC"
    if len(raw) <= 4:
        return raw
    return raw[:4]


def _list_deployed_problem_catalog(*, team_id: str | None, competition_id: str | None, limit: int) -> list[dict[str, Any]]:
    safe_limit = max(1, min(int(limit), 2000))
    con = _connect()
    try:
        with con.cursor() as cur:
            params: list[object] = []
            where_clauses = [
                "vs.status = 'active'",
                "ts.status = 'running'",
                "t.status <> 'disqualified'",
            ]
            if competition_id:
                where_clauses.append("t.competition_id = %s")
                params.append(str(competition_id))
            if team_id:
                where_clauses.append("t.id = %s")
                params.append(str(team_id))
            query = f"""
                SELECT
                    vs.id::text AS service_id,
                    vs.name AS title,
                    vs.description,
                    vs.category,
                    vs.score,
                    vs.difficulty,
                    vs.created_at,
                    vs.updated_at,
                    MIN(t.created_at) AS first_team_created_at
                FROM cstrike.team_services ts
                JOIN cstrike.vuln_services vs ON vs.id = ts.service_id
                JOIN cstrike.teams t ON t.id = ts.team_id
                WHERE {' AND '.join(where_clauses)}
                GROUP BY vs.id, vs.name, vs.description, vs.category, vs.score, vs.difficulty, vs.created_at, vs.updated_at
                ORDER BY upper(coalesce(vs.category, 'MISC')) ASC, vs.created_at ASC, vs.id ASC
                LIMIT %s
            """
            params.append(safe_limit)
            cur.execute(query, tuple(params))
            rows = cur.fetchall() or []
    finally:
        con.close()

    counters: dict[str, int] = defaultdict(int)
    catalog: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        category = _problem_prefix(item.get("category"))
        counters[category] += 1
        item["problem_no"] = f"{category}-{counters[category]:03d}"
        catalog.append(item)
    return catalog


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
    "get_problem_visibility_state",
    "list_problem_team_links",
    "upsert_problem_team_link",
]
