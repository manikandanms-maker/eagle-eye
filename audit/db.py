"""Magento (MySQL) connection — read-only, env-driven, lazy connect.

Credentials via environment (see .env.example):
  MAGENTO_DB_HOST, MAGENTO_DB_NAME, MAGENTO_DB_USER, MAGENTO_DB_PASSWORD
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Generator, Optional

# Defaults from CaratLane read-replica; override via env for other environments.
DEFAULT_HOST = "caratlaneliverds-rr.caratlane.com"
DEFAULT_DATABASE = "caratlane"
DEFAULT_USER = "caratlanelive"
DEFAULT_PORT = 3306
CONNECT_TIMEOUT = 8
READ_TIMEOUT = 45


class DatabaseConfigError(RuntimeError):
    """Raised when required DB settings are missing."""


def db_configured() -> bool:
    """True when host, database, user, and password are all set."""
    return bool(_password() and _host() and _database() and _user())


def _host() -> str:
    return os.getenv("MAGENTO_DB_HOST", DEFAULT_HOST).strip()


def _database() -> str:
    return os.getenv("MAGENTO_DB_NAME", DEFAULT_DATABASE).strip()


def _user() -> str:
    return os.getenv("MAGENTO_DB_USER", DEFAULT_USER).strip()


def _password() -> str:
    # Set MAGENTO_DB_PASSWORD in .env or export before running the server.
    return os.getenv("MAGENTO_DB_PASSWORD", "").strip()


def _port() -> int:
    raw = os.getenv("MAGENTO_DB_PORT", str(DEFAULT_PORT)).strip()
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_PORT


@contextmanager
def get_connection() -> Generator[Any, None, None]:
    """Open a read-only MySQL connection; closes on exit."""
    if not db_configured():
        raise DatabaseConfigError(
            "Magento DB not configured. Set MAGENTO_DB_PASSWORD in .env "
            f"(host={_host()}, db={_database()}, user={_user()})."
        )
    try:
        import pymysql
        from pymysql.cursors import DictCursor
    except ImportError as exc:
        raise RuntimeError(
            "pymysql is required for live Magento fetch. pip install pymysql"
        ) from exc

    conn = pymysql.connect(
        host=_host(),
        port=_port(),
        user=_user(),
        password=_password(),
        database=_database(),
        charset="utf8mb4",
        cursorclass=DictCursor,
        connect_timeout=CONNECT_TIMEOUT,
        read_timeout=READ_TIMEOUT,
        autocommit=True,
    )
    try:
        yield conn
    finally:
        conn.close()


def fetch_all(sql: str, params: Optional[dict] = None) -> list[dict]:
    """Run a SELECT and return all rows as dicts."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or {})
            return list(cur.fetchall())


def fetch_one(sql: str, params: Optional[dict] = None) -> Optional[dict]:
    rows = fetch_all(sql, params)
    return rows[0] if rows else None
