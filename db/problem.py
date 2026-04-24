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
    for key in ("created_at", "updated_at"):
        value = payload.get(key)
        if isinstance(value, datetime):
            payload[key] = value.isoformat(sep=" ")
    return payload


def ensure_problem_table() -> None:
    """cstrike.problem 테이블은 infra/db/init/02-discord-bot-tables.sql 에서 생성.

    외주 호환 유지를 위해 함수 시그니처는 그대로 두되, 실제 DDL은 init SQL이 담당하므로 no-op.
    운영포털 부트스트랩 시점에 테이블이 존재함을 보장.
    """
    return None


def create_problem(
    *,
    problem_no: str,
    title: str,
    category: str = "MISC",
    score: int = 0,
    difficulty: str = "Easy",
    description: str | None = None,
    access_url: str | None = None,
    download_url: str | None = None,
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
                    access_url, download_url,
                    created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (problem_no) DO UPDATE SET
                    title = EXCLUDED.title,
                    category = EXCLUDED.category,
                    score = EXCLUDED.score,
                    difficulty = EXCLUDED.difficulty,
                    description = EXCLUDED.description,
                    access_url = EXCLUDED.access_url,
                    download_url = EXCLUDED.download_url,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    no,
                    str(title or "").strip() or no,
                    str(category or "MISC").strip().upper(),
                    int(score),
                    str(difficulty or "Easy").strip() or "Easy",
                    (str(description).strip() if description is not None else None),
                    (str(access_url).strip() if access_url is not None else None),
                    (str(download_url).strip() if download_url is not None else None),
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


__all__ = [
    "ensure_problem_table",
    "create_problem",
    "list_problems",
    "get_problem_by_no",
]
