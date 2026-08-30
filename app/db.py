"""Database access for SQLite and Postgres (Supabase).

Everything above this module writes one dialect of SQL: `?` placeholders,
`CURRENT_TIMESTAMP`, and SMALLINT 0/1 for booleans. This module translates for
whichever backend is configured, so no caller knows or cares which one is live.

Backend selection: `KALVID_DB_BACKEND=postgres|sqlite`, defaulting to postgres when
SUPABASE_DB_URL is set to something real.
"""
from __future__ import annotations

import contextlib
import re
import sqlite3
import threading
from pathlib import Path

from .config import settings

_local = threading.local()
_init_lock = threading.Lock()
_initialised = False

BACKEND = settings.db_backend          # 'sqlite' | 'postgres'


class ConfigError(Exception):
    pass


# ---------------------------------------------------------------- connections

def _configure_sqlite(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Background renders write while the dashboard reads; WAL lets those overlap.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")


def _connect():
    if BACKEND == "postgres":
        import psycopg
        from psycopg.rows import dict_row

        if not settings.postgres_dsn:
            raise ConfigError(
                "KALVID_DB_BACKEND=postgres but SUPABASE_DB_URL is unset or still a "
                "placeholder. Put the connection string from Supabase → Project "
                "Settings → Database into .env."
            )
        # prepare_threshold=None disables psycopg's automatic prepared statements.
        # Supabase's transaction pooler (port 6543) multiplexes connections and cannot
        # keep server-side prepared statements alive between transactions — leaving
        # this on produces intermittent "prepared statement does not exist" errors
        # that only appear under load.
        return psycopg.connect(
            settings.postgres_dsn, row_factory=dict_row, autocommit=False,
            connect_timeout=15, prepare_threshold=None,
        )

    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path, timeout=10.0)
    _configure_sqlite(conn)
    return conn


def get_conn():
    """One connection per thread — neither driver's connections are thread-safe."""
    conn = getattr(_local, "conn", None)
    if conn is not None and getattr(conn, "closed", 0):
        conn = None                                  # psycopg drops idle connections
    if conn is None:
        conn = _connect()
        _local.conn = conn
    return conn


def close_conn() -> None:
    conn = getattr(_local, "conn", None)
    if conn is not None:
        with contextlib.suppress(Exception):
            conn.close()
        _local.conn = None


# ---------------------------------------------------------------- SQL dialect

_PLACEHOLDER = re.compile(r"\?")


def translate(sql: str) -> str:
    """`?` placeholders are the house style; psycopg wants `%s`."""
    if BACKEND != "postgres":
        return sql
    # Guard the literal `%` that psycopg would otherwise treat as a format marker.
    return _PLACEHOLDER.sub("%s", sql.replace("%", "%%")) if "%" in sql \
        else _PLACEHOLDER.sub("%s", sql)


def _cursor(conn, sql: str, params: tuple):
    return conn.execute(translate(sql), params)


# ---------------------------------------------------------------- queries

def query(sql: str, params: tuple = ()) -> list:
    return _cursor(get_conn(), sql, params).fetchall()


def query_one(sql: str, params: tuple = ()):
    return _cursor(get_conn(), sql, params).fetchone()


def execute(sql: str, params: tuple = ()):
    conn = get_conn()
    cur = _cursor(conn, sql, params)
    conn.commit()
    return cur


def insert(sql: str, params: tuple = ()) -> int:
    """INSERT returning the new id, on either backend."""
    conn = get_conn()
    if BACKEND == "postgres":
        cur = _cursor(conn, sql.rstrip().rstrip(";") + " RETURNING id", params)
        row = cur.fetchone()
        conn.commit()
        return row["id"]
    cur = _cursor(conn, sql, params)
    conn.commit()
    return cur.lastrowid


@contextlib.contextmanager
def exclusive(lock_key: int):
    """Serialise a read-then-write against other writers.

    The budget guard reads current spend and then reserves against it; without this
    two concurrent approvals could both read "under the cap" and both proceed.

    SQLite : BEGIN IMMEDIATE takes the database write lock for the transaction.
    Postgres: a transaction-scoped advisory lock keyed on the client id, which
              serialises reservations per client without blocking unrelated work.
    """
    conn = get_conn()
    if BACKEND == "postgres":
        conn.execute("BEGIN")
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key,))
    else:
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise


def executescript(script: str) -> None:
    conn = get_conn()
    if BACKEND == "postgres":
        conn.execute(script)
    else:
        conn.executescript(script)
    conn.commit()


# ---------------------------------------------------------------- schema

def schema_sql() -> str:
    name = "schema_pg.sql" if BACKEND == "postgres" else "schema.sql"
    return (Path(__file__).parent / name).read_text()


def init_db() -> None:
    global _initialised
    with _init_lock:
        if _initialised:
            return
        executescript(schema_sql())
        _initialised = True


def reset_initialised() -> None:
    """Used by the migration tool after switching backend."""
    global _initialised
    _initialised = False
