"""SQLite persistence for the ChannelGuard admin bot (aiosqlite).

Four tables:
  groups        every chat the bot has been added to (+ its short code + link)
  templates     owner-defined /add entries keyed by keyword
  join_requests one row per pending/handled join request
  events        lightweight audit log (added/approved/declined/removed/...)

Every write is committed immediately; the module is safe to import before the
database file exists (``init`` creates it).
"""
from __future__ import annotations

import time
from typing import Any, Iterable, Optional

import aiosqlite

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    chat_id     INTEGER PRIMARY KEY,
    title       TEXT    NOT NULL DEFAULT '',
    short_code  TEXT    NOT NULL DEFAULT '',
    chat_type   TEXT    NOT NULL DEFAULT '',
    username    TEXT,
    invite_link TEXT,
    is_admin    INTEGER NOT NULL DEFAULT 1,
    added_at    REAL    NOT NULL DEFAULT 0,
    updated_at  REAL    NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS templates (
    keyword      TEXT PRIMARY KEY,
    amount       TEXT NOT NULL DEFAULT '',
    account_name TEXT NOT NULL DEFAULT '',
    body         TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS join_requests (
    chat_id      INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    username     TEXT,
    full_name    TEXT NOT NULL DEFAULT '',
    invite_link  TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    requested_at REAL NOT NULL DEFAULT 0,
    handled_at   REAL,
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    kind     TEXT NOT NULL,
    chat_id  INTEGER,
    user_id  INTEGER,
    detail   TEXT,
    ts       REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_groups_short ON groups(short_code);
CREATE INDEX IF NOT EXISTS idx_jr_status ON join_requests(status);
"""

_conn: Optional[aiosqlite.Connection] = None


async def init() -> None:
    """Open the connection (once) and create tables."""
    global _conn
    if _conn is not None:
        return
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _conn = await aiosqlite.connect(str(config.DB_PATH))
    _conn.row_factory = aiosqlite.Row
    await _conn.execute("PRAGMA journal_mode=WAL;")
    await _conn.execute("PRAGMA foreign_keys=ON;")
    await _conn.executescript(_SCHEMA)
    await _conn.commit()


async def close() -> None:
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


def _db() -> aiosqlite.Connection:
    if _conn is None:
        raise RuntimeError("db.init() must be awaited before using the database")
    return _conn


async def _run(sql: str, params: Iterable[Any] = ()) -> None:
    await _db().execute(sql, tuple(params))
    await _db().commit()


async def _all(sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
    cur = await _db().execute(sql, tuple(params))
    rows = await cur.fetchall()
    await cur.close()
    return list(rows)


async def _one(sql: str, params: Iterable[Any] = ()) -> Optional[aiosqlite.Row]:
    cur = await _db().execute(sql, tuple(params))
    row = await cur.fetchone()
    await cur.close()
    return row


# --- groups ---------------------------------------------------------------
async def upsert_group(
    chat_id: int,
    title: str,
    short_code: str,
    chat_type: str,
    username: Optional[str],
    invite_link: Optional[str],
    is_admin: bool,
) -> None:
    now = time.time()
    await _run(
        """
        INSERT INTO groups (chat_id, title, short_code, chat_type, username,
                            invite_link, is_admin, added_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?)
        ON CONFLICT(chat_id) DO UPDATE SET
            title=excluded.title,
            short_code=excluded.short_code,
            chat_type=excluded.chat_type,
            username=excluded.username,
            invite_link=COALESCE(excluded.invite_link, groups.invite_link),
            is_admin=excluded.is_admin,
            updated_at=excluded.updated_at
        """,
        (chat_id, title, short_code, chat_type, username, invite_link,
         int(is_admin), now, now),
    )


async def set_group_link(chat_id: int, invite_link: Optional[str]) -> None:
    await _run(
        "UPDATE groups SET invite_link=?, updated_at=? WHERE chat_id=?",
        (invite_link, time.time(), chat_id),
    )


async def set_group_admin(chat_id: int, is_admin: bool) -> None:
    await _run(
        "UPDATE groups SET is_admin=?, updated_at=? WHERE chat_id=?",
        (int(is_admin), time.time(), chat_id),
    )


async def get_group(chat_id: int) -> Optional[aiosqlite.Row]:
    return await _one("SELECT * FROM groups WHERE chat_id=?", (chat_id,))


async def all_groups(admin_only: bool = False) -> list[aiosqlite.Row]:
    sql = "SELECT * FROM groups"
    if admin_only:
        sql += " WHERE is_admin=1"
    sql += " ORDER BY title COLLATE NOCASE"
    return await _all(sql)


async def find_groups(query: str) -> list[aiosqlite.Row]:
    """Match a query against short code (exact, case-insensitive) or title/
    username (substring). Ordered so exact short-code hits come first."""
    q = query.strip().lower()
    like = f"%{q}%"
    return await _all(
        """
        SELECT *,
               (LOWER(short_code)=? ) AS exact_short
        FROM groups
        WHERE is_admin=1 AND (
              LOWER(short_code)=?
           OR LOWER(title) LIKE ?
           OR LOWER(COALESCE(username,'')) LIKE ?
        )
        ORDER BY exact_short DESC, title COLLATE NOCASE
        """,
        (q, q, like, like),
    )


async def remove_group(chat_id: int) -> None:
    await _run("DELETE FROM groups WHERE chat_id=?", (chat_id,))


# --- templates ------------------------------------------------------------
async def upsert_template(
    keyword: str, amount: str, account_name: str, body: str
) -> None:
    await _run(
        """
        INSERT INTO templates (keyword, amount, account_name, body, created_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT(keyword) DO UPDATE SET
            amount=excluded.amount,
            account_name=excluded.account_name,
            body=excluded.body
        """,
        (keyword.lower(), amount, account_name, body, time.time()),
    )


async def get_template(keyword: str) -> Optional[aiosqlite.Row]:
    return await _one(
        "SELECT * FROM templates WHERE keyword=?", (keyword.lower(),)
    )


async def all_templates() -> list[aiosqlite.Row]:
    return await _all("SELECT * FROM templates ORDER BY keyword")


async def remove_template(keyword: str) -> bool:
    cur = await _db().execute(
        "DELETE FROM templates WHERE keyword=?", (keyword.lower(),)
    )
    await _db().commit()
    return cur.rowcount > 0


# --- join requests --------------------------------------------------------
async def add_join_request(
    chat_id: int,
    user_id: int,
    username: Optional[str],
    full_name: str,
    invite_link: Optional[str],
) -> None:
    await _run(
        """
        INSERT INTO join_requests
            (chat_id, user_id, username, full_name, invite_link, status,
             requested_at)
        VALUES (?,?,?,?,?, 'pending', ?)
        ON CONFLICT(chat_id, user_id) DO UPDATE SET
            username=excluded.username,
            full_name=excluded.full_name,
            invite_link=excluded.invite_link,
            status='pending',
            requested_at=excluded.requested_at,
            handled_at=NULL
        """,
        (chat_id, user_id, username, full_name, invite_link, time.time()),
    )


async def set_join_status(chat_id: int, user_id: int, status: str) -> None:
    await _run(
        "UPDATE join_requests SET status=?, handled_at=? WHERE chat_id=? AND user_id=?",
        (status, time.time(), chat_id, user_id),
    )


async def pending_requests(user_id: Optional[int] = None) -> list[aiosqlite.Row]:
    if user_id is None:
        return await _all(
            "SELECT * FROM join_requests WHERE status='pending' ORDER BY requested_at"
        )
    return await _all(
        "SELECT * FROM join_requests WHERE status='pending' AND user_id=? "
        "ORDER BY requested_at",
        (user_id,),
    )


async def find_pending_by_username(username: str) -> list[aiosqlite.Row]:
    uname = username.lstrip("@").lower()
    return await _all(
        "SELECT * FROM join_requests WHERE status='pending' "
        "AND LOWER(COALESCE(username,''))=?",
        (uname,),
    )


# --- events ---------------------------------------------------------------
async def log_event(
    kind: str,
    chat_id: Optional[int] = None,
    user_id: Optional[int] = None,
    detail: str = "",
) -> None:
    await _run(
        "INSERT INTO events (kind, chat_id, user_id, detail, ts) VALUES (?,?,?,?,?)",
        (kind, chat_id, user_id, detail, time.time()),
    )
