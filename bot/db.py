"""SQLite persistence for the ChannelGuard admin bot (aiosqlite).

Tables:
  groups        every chat the bot has been added to (+ its short code + link)
  templates     owner-defined post bodies keyed by keyword
  join_requests one row per pending/handled join request
  orders        one paid /add order (ANI####) with amount/account/keyword
  order_links   single-use invite link(s) minted for an order, per group
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

CREATE TABLE IF NOT EXISTS orders (
    order_id     TEXT PRIMARY KEY,
    amount       TEXT NOT NULL DEFAULT '',
    account_name TEXT NOT NULL DEFAULT '',
    keyword      TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'open',
    created_at   REAL NOT NULL DEFAULT 0,
    updated_at   REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS order_links (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    TEXT NOT NULL,
    chat_id     INTEGER NOT NULL,
    invite_link TEXT NOT NULL,
    joined_user INTEGER,
    revoked     INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS bindings (
    chat_id     INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    invite_link TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  REAL NOT NULL DEFAULT 0,
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
CREATE INDEX IF NOT EXISTS idx_ol_order ON order_links(order_id);
CREATE INDEX IF NOT EXISTS idx_ol_link ON order_links(invite_link);
CREATE INDEX IF NOT EXISTS idx_bind_link ON bindings(invite_link);
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
    """Match a query against short code (exact) or title/username (substring),
    folding fancy Unicode fonts on BOTH sides so typing "nice bro" matches a
    title like "ɴɪᴄᴇ ʙʀᴏ". Exact short-code hits come first."""
    from .utils import fold_fonts

    q = fold_fonts(query.strip()).lower()
    if not q:
        return []
    rows = await _all(
        "SELECT * FROM groups WHERE is_admin=1 ORDER BY title COLLATE NOCASE"
    )
    exact, subs = [], []
    for r in rows:
        short = fold_fonts(r["short_code"] or "").lower()
        title = fold_fonts(r["title"] or "").lower()
        uname = fold_fonts(r["username"] or "").lower()
        if short == q:
            exact.append(r)
        elif q in title or q in short or (uname and q in uname):
            subs.append(r)
    return exact + subs


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


# --- orders ---------------------------------------------------------------
async def next_order_id(prefix: str) -> str:
    """Return the next sequential id for ``prefix`` (e.g. ANI0001)."""
    rows = await _all(
        "SELECT order_id FROM orders WHERE order_id LIKE ?", (prefix + "%",)
    )
    plen = len(prefix)
    mx = 0
    for r in rows:
        suffix = r["order_id"][plen:]
        if suffix.isdigit():
            mx = max(mx, int(suffix))
    return f"{prefix}{mx + 1:04d}"


async def create_order(
    order_id: str, amount: str, account_name: str, keyword: str
) -> None:
    now = time.time()
    await _run(
        """
        INSERT INTO orders (order_id, amount, account_name, keyword, status,
                            created_at, updated_at)
        VALUES (?,?,?,?, 'open', ?, ?)
        """,
        (order_id, amount, account_name, keyword, now, now),
    )


async def add_order_link(order_id: str, chat_id: int, invite_link: str) -> int:
    cur = await _db().execute(
        """
        INSERT INTO order_links (order_id, chat_id, invite_link, created_at)
        VALUES (?,?,?,?)
        """,
        (order_id, chat_id, invite_link, time.time()),
    )
    await _db().commit()
    return cur.lastrowid


async def get_order(order_id: str) -> Optional[aiosqlite.Row]:
    row = await _one("SELECT * FROM orders WHERE order_id=?", (order_id,))
    if row is None:
        row = await _one(
            "SELECT * FROM orders WHERE UPPER(order_id)=?", (order_id.upper(),)
        )
    return row


async def order_links(order_id: str) -> list[aiosqlite.Row]:
    return await _all(
        "SELECT * FROM order_links WHERE order_id=? ORDER BY id", (order_id,)
    )


async def set_order_status(order_id: str, status: str) -> None:
    await _run(
        "UPDATE orders SET status=?, updated_at=? WHERE order_id=?",
        (status, time.time(), order_id),
    )


async def find_order_link_by_invite(invite_link: str) -> Optional[aiosqlite.Row]:
    return await _one(
        "SELECT * FROM order_links WHERE invite_link=?", (invite_link,)
    )


async def set_order_link_joined(link_id: int, user_id: int) -> None:
    await _run(
        "UPDATE order_links SET joined_user=? WHERE id=?", (user_id, link_id)
    )


async def set_order_link_revoked(link_id: int) -> None:
    await _run("UPDATE order_links SET revoked=1 WHERE id=?", (link_id,))


async def all_orders(limit: int = 20) -> list[aiosqlite.Row]:
    return await _all(
        "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (limit,)
    )


# --- bindings (one paid user reserved for one group's link) ---------------
async def add_binding(chat_id: int, user_id: int, invite_link: str) -> None:
    await _run(
        """
        INSERT INTO bindings (chat_id, user_id, invite_link, status, created_at)
        VALUES (?,?,?, 'pending', ?)
        ON CONFLICT(chat_id, user_id) DO UPDATE SET
            invite_link=excluded.invite_link,
            status='pending',
            created_at=excluded.created_at
        """,
        (chat_id, user_id, invite_link, time.time()),
    )


async def get_binding(chat_id: int, user_id: int) -> Optional[aiosqlite.Row]:
    return await _one(
        "SELECT * FROM bindings WHERE chat_id=? AND user_id=?", (chat_id, user_id)
    )


async def binding_by_link(invite_link: str) -> Optional[aiosqlite.Row]:
    return await _one(
        "SELECT * FROM bindings WHERE invite_link=? AND status='pending' "
        "ORDER BY created_at DESC",
        (invite_link,),
    )


async def set_binding_status(chat_id: int, user_id: int, status: str) -> None:
    await _run(
        "UPDATE bindings SET status=? WHERE chat_id=? AND user_id=?",
        (status, chat_id, user_id),
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
