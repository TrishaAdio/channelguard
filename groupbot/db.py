"""Async SQLite storage for the group-logger bot (aiosqlite)."""
from __future__ import annotations

import os
import time
from typing import Any, Optional

import aiosqlite

import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS groups (
    chat_id     INTEGER PRIMARY KEY,
    title       TEXT NOT NULL DEFAULT '',
    short_form  TEXT NOT NULL DEFAULT '',
    username    TEXT,
    is_admin    INTEGER NOT NULL DEFAULT 0,
    invite_link TEXT,
    added_at    REAL NOT NULL DEFAULT 0,
    updated_at  REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS join_requests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    username     TEXT,
    full_name    TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    requested_at REAL NOT NULL DEFAULT 0,
    decided_at   REAL,
    owner_msg_id INTEGER,
    UNIQUE(chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS links (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id           INTEGER NOT NULL,
    invite_link       TEXT NOT NULL,
    revoke_after_join INTEGER NOT NULL DEFAULT 1,
    active            INTEGER NOT NULL DEFAULT 1,
    created_at        REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_links_link ON links(invite_link);

CREATE TABLE IF NOT EXISTS orders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   TEXT NOT NULL,
    amount     TEXT,
    account    TEXT,
    keyword    TEXT,
    user_id    INTEGER,
    chat_id    INTEGER,
    link       TEXT,
    created_at REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS removed_users (
    user_id    INTEGER PRIMARY KEY,
    username   TEXT,
    removed_at REAL NOT NULL DEFAULT 0
);
"""


def _now() -> float:
    return time.time()


async def init() -> None:
    os.makedirs(os.path.dirname(config.DB_PATH) or ".", exist_ok=True)
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.executescript(_SCHEMA)
        # Lightweight migration for databases created before owner_msg_id.
        async with db.execute("PRAGMA table_info(join_requests)") as cur:
            columns = [row[1] for row in await cur.fetchall()]
        if "owner_msg_id" not in columns:
            await db.execute("ALTER TABLE join_requests ADD COLUMN owner_msg_id INTEGER")
        await db.commit()


def _row_to_dict(cursor, row) -> dict[str, Any]:
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


async def _fetchall(sql: str, params: tuple = ()) -> list[dict]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        db.row_factory = _row_to_dict
        async with db.execute(sql, params) as cur:
            return list(await cur.fetchall())


async def _fetchone(sql: str, params: tuple = ()) -> Optional[dict]:
    rows = await _fetchall(sql, params)
    return rows[0] if rows else None


async def _execute(sql: str, params: tuple = ()) -> None:
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute(sql, params)
        await db.commit()


# ---- groups --------------------------------------------------------------
async def upsert_group(chat_id: int, title: str, short_form: str,
                       username: Optional[str], is_admin: bool) -> None:
    now = _now()
    await _execute(
        """
        INSERT INTO groups (chat_id, title, short_form, username, is_admin, added_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            title=excluded.title,
            short_form=excluded.short_form,
            username=excluded.username,
            is_admin=excluded.is_admin,
            updated_at=excluded.updated_at
        """,
        (chat_id, title, short_form, username, 1 if is_admin else 0, now, now),
    )


async def set_group_admin(chat_id: int, is_admin: bool) -> None:
    await _execute(
        "UPDATE groups SET is_admin=?, updated_at=? WHERE chat_id=?",
        (1 if is_admin else 0, _now(), chat_id),
    )


async def set_group_link(chat_id: int, invite_link: Optional[str]) -> None:
    await _execute(
        "UPDATE groups SET invite_link=?, updated_at=? WHERE chat_id=?",
        (invite_link, _now(), chat_id),
    )


async def get_group(chat_id: int) -> Optional[dict]:
    return await _fetchone("SELECT * FROM groups WHERE chat_id=?", (chat_id,))


async def list_admin_groups() -> list[dict]:
    return await _fetchall(
        "SELECT * FROM groups WHERE is_admin=1 ORDER BY title COLLATE NOCASE"
    )


async def find_groups_by_name(name: str) -> list[dict]:
    """Match admin groups by title or short form (case-insensitive contains)."""
    like = f"%{name.strip()}%"
    return await _fetchall(
        """
        SELECT * FROM groups
        WHERE is_admin=1 AND (title LIKE ? COLLATE NOCASE OR short_form LIKE ? COLLATE NOCASE)
        ORDER BY title COLLATE NOCASE
        """,
        (like, like),
    )


# ---- join requests -------------------------------------------------------
async def add_join_request(chat_id: int, user_id: int, username: Optional[str],
                           full_name: str) -> None:
    await _execute(
        """
        INSERT INTO join_requests (chat_id, user_id, username, full_name, status, requested_at)
        VALUES (?, ?, ?, ?, 'pending', ?)
        ON CONFLICT(chat_id, user_id) DO UPDATE SET
            username=excluded.username,
            full_name=excluded.full_name,
            status='pending',
            requested_at=excluded.requested_at,
            decided_at=NULL
        """,
        (chat_id, user_id, username, full_name, _now()),
    )


async def set_request_owner_msg(chat_id: int, user_id: int, owner_msg_id: int) -> None:
    await _execute(
        "UPDATE join_requests SET owner_msg_id=? WHERE chat_id=? AND user_id=?",
        (owner_msg_id, chat_id, user_id),
    )


async def set_request_status(chat_id: int, user_id: int, status: str) -> None:
    await _execute(
        "UPDATE join_requests SET status=?, decided_at=? WHERE chat_id=? AND user_id=?",
        (status, _now(), chat_id, user_id),
    )


async def pending_requests() -> list[dict]:
    return await _fetchall(
        "SELECT * FROM join_requests WHERE status='pending' ORDER BY requested_at DESC"
    )


async def find_pending_by_keyword(keyword: str) -> list[dict]:
    like = f"%{keyword.strip()}%"
    return await _fetchall(
        """
        SELECT * FROM join_requests
        WHERE status='pending'
          AND (IFNULL(username,'') LIKE ? COLLATE NOCASE
               OR IFNULL(full_name,'') LIKE ? COLLATE NOCASE
               OR CAST(user_id AS TEXT) = ?)
        ORDER BY requested_at DESC
        """,
        (like, like, keyword.strip()),
    )


async def pending_for_user(user_id: int) -> list[dict]:
    return await _fetchall(
        "SELECT * FROM join_requests WHERE status='pending' AND user_id=? ORDER BY requested_at DESC",
        (user_id,),
    )


# ---- links (for /link, revoke-after-join) --------------------------------
async def add_link(chat_id: int, invite_link: str, revoke_after_join: bool) -> None:
    await _execute(
        "INSERT INTO links (chat_id, invite_link, revoke_after_join, active, created_at) VALUES (?, ?, ?, 1, ?)",
        (chat_id, invite_link, 1 if revoke_after_join else 0, _now()),
    )


async def get_active_link(invite_link: str) -> Optional[dict]:
    return await _fetchone(
        "SELECT * FROM links WHERE invite_link=? AND active=1", (invite_link,)
    )


async def deactivate_link(invite_link: str) -> None:
    await _execute("UPDATE links SET active=0 WHERE invite_link=?", (invite_link,))


async def active_revocable_links(chat_id: int) -> list[dict]:
    return await _fetchall(
        "SELECT * FROM links WHERE chat_id=? AND active=1 AND revoke_after_join=1",
        (chat_id,),
    )


# ---- orders --------------------------------------------------------------
async def add_order(order_id: str, amount: str, account: str, keyword: str,
                    user_id: Optional[int], chat_id: Optional[int],
                    link: Optional[str]) -> None:
    await _execute(
        """
        INSERT INTO orders (order_id, amount, account, keyword, user_id, chat_id, link, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (order_id, amount, account, keyword, user_id, chat_id, link, _now()),
    )


# ---- removed users -------------------------------------------------------
async def add_removed(user_id: int, username: Optional[str]) -> None:
    await _execute(
        "INSERT INTO removed_users (user_id, username, removed_at) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, removed_at=excluded.removed_at",
        (user_id, username, _now()),
    )


async def is_removed(user_id: int) -> bool:
    return await _fetchone("SELECT 1 FROM removed_users WHERE user_id=?", (user_id,)) is not None
