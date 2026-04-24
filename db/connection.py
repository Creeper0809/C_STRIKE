"""Connection and bootstrap helpers (PostgreSQL).

운영포털 통합을 위해 외주 원본 MySQL 드라이버(PyMySQL)를 PostgreSQL(psycopg2)로 교체.
모든 쿼리는 `cstrike` 스키마를 기본 search_path로 사용하므로 외주 코드의 테이블명 변경 최소화.
"""

from __future__ import annotations

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

from .schema import init_db

KST = timezone(timedelta(hours=9), name="KST")


def _now_kst_naive() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_driver() -> None:
    if psycopg2 is None:
        raise RuntimeError("psycopg2 is not installed. Run: pip install psycopg2-binary")


def _connect(include_database: bool = True):
    """PostgreSQL 연결 반환.

    외주 원본은 autocommit=False, DictCursor 전제. psycopg2에선 cursor_factory=RealDictCursor로 동등 기능 제공.
    include_database 인자는 외주 호환용이며 PostgreSQL은 DB 생성을 init SQL이 처리하므로 항상 True로 동작.
    """
    _ensure_driver()
    kwargs = {
        "host": config.POSTGRES_HOST,
        "port": config.POSTGRES_PORT,
        "user": config.POSTGRES_USER,
        "password": config.POSTGRES_PASSWORD,
        "dbname": config.POSTGRES_DATABASE,
        "cursor_factory": RealDictCursor,
    }
    conn = psycopg2.connect(**kwargs)
    conn.autocommit = False
    # 외주 쿼리가 스키마 없이 테이블명만 쓰므로 search_path 지정
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {config.POSTGRES_SCHEMA}, public")
    conn.commit()
    return conn


class _CompatCursor:
    """PyMySQL 호환 cursor. 외주 코드가 `%s` 파라미터 스타일을 사용해 psycopg2와 동일하므로 wrapping 불필요.

    외주가 lastrowid 접근하는 경우를 대비해 최소 호환층만 유지. 현재는 placeholder.
    """


__all__ = ["_connect", "_now_kst_naive", "init_db"]
