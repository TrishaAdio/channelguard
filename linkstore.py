"""Shared on-disk handoff used by the guard, bot, and quickreply userbot.

The admin bot receives batched reservation requests from ``quickreply.py`` and
publishes the generated join-request links back to it. ``guard.py`` separately
records its rotating demo link; only that guard-owned value may update the
Business ``/demo`` quick reply.
"""
from __future__ import annotations

import json
import os
import time
import unicodedata
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Callable, Optional

STORE = Path(__file__).resolve().parent / "data" / "links.json"
REQUESTS = STORE.parent / "link_requests.json"
RESULTS = STORE.parent / "link_results.json"
DEMO_LINK = STORE.parent / "demo_link.json"
COMMANDS = STORE.parent / "command_claims.json"
REVOKES = STORE.parent / "pending_revokes.json"
_MAX_ENTRIES = 500
_TTL = 3600
_COMMAND_TTL = 86400
_COMMAND_LEASE_SECONDS = 300
_LEASE_SECONDS = 120

_SMALL_CAPS = {
    "ᴀ": "a", "ʙ": "b", "ᴄ": "c", "ᴅ": "d", "ᴇ": "e", "ꜰ": "f", "ғ": "f",
    "ɢ": "g", "ʜ": "h", "ɪ": "i", "ᴊ": "j", "ᴋ": "k", "ʟ": "l", "ᴍ": "m",
    "ɴ": "n", "ᴏ": "o", "ᴘ": "p", "ǫ": "q", "ꞯ": "q", "ʀ": "r", "ꜱ": "s",
    "ᴛ": "t", "ᴜ": "u", "ᴠ": "v", "ᴡ": "w", "ʏ": "y", "ᴢ": "z",
}


def fold(text: str) -> str:
    """Fold styled Unicode text to plain lowercase for group matching."""
    if not text:
        return ""
    value = "".join(_SMALL_CAPS.get(char, char) for char in text)
    value = unicodedata.normalize("NFKD", value)
    return "".join(
        char for char in value if not unicodedata.combining(char)
    ).casefold().strip()


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    tmp.replace(path)


@contextmanager
def _exclusive(path: Path):
    """Serialize read-modify-write operations on POSIX and Windows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    handle = open(lock_path, "a+")
    windows_lock = False
    try:
        try:
            import fcntl
        except ImportError:
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write("\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            windows_lock = True
        else:
            fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if windows_lock:
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()


@contextmanager
def _exclusive_many(paths):
    """Acquire several store locks in a stable order to avoid deadlocks."""
    with ExitStack() as stack:
        for path in sorted(set(paths), key=lambda item: str(item)):
            stack.enter_context(_exclusive(path))
        yield


def _mutate(path: Path, default, callback: Callable):
    with _exclusive(path):
        data = _read_json(path, default)
        data, result = callback(data)
        _write_json(path, data)
        return result


# --- general links --------------------------------------------------------
def _load() -> dict:
    data = _read_json(STORE, {"entries": [], "last": None})
    if not isinstance(data, dict):
        return {"entries": [], "last": None}
    data.setdefault("entries", [])
    data.setdefault("last", None)
    return data


def save_link(link: str, title: str = "", short: str = "") -> None:
    """Record a reusable/general group link; never use this for reservations."""
    if not link:
        return
    entry = {
        "link": link,
        "title": title or "",
        "short": short or "",
        "ftitle": fold(title),
        "fshort": fold(short),
        "ts": time.time(),
    }

    def update(data):
        if not isinstance(data, dict):
            data = {"entries": [], "last": None}
        ftitle = entry["ftitle"]
        entries = [
            item for item in data.get("entries", [])
            if not ftitle or item.get("ftitle") != ftitle
        ]
        entries.append(entry)
        data["entries"] = entries[-_MAX_ENTRIES:]
        data["last"] = entry
        return data, None

    _mutate(STORE, {"entries": [], "last": None}, update)


def find_link(query: str) -> Optional[str]:
    data = _load()
    q = fold(query)
    if not q:
        last = data.get("last")
        return last.get("link") if last else None
    best = None
    for entry in data.get("entries", []):
        if q in entry.get("ftitle", "") or (
            entry.get("fshort") and q in entry.get("fshort", "")
        ):
            if best is None or entry.get("ts", 0) > best.get("ts", 0):
                best = entry
    return best.get("link") if best else None


def find_entry(query: str) -> Optional[dict]:
    data = _load()
    q = fold(query)
    if not q:
        return data.get("last")
    best = None
    for entry in data.get("entries", []):
        if q in entry.get("ftitle", "") or (
            entry.get("fshort") and q in entry.get("fshort", "")
        ):
            if best is None or entry.get("ts", 0) > best.get("ts", 0):
                best = entry
    return best


# --- guard-only demo link -------------------------------------------------
def save_demo_link(link: str) -> None:
    """Record the current guard.py link before it is DMed to the owner."""
    if not link:
        return
    _mutate(
        DEMO_LINK,
        {},
        lambda _data: ({"link": link, "ts": time.time()}, None),
    )


def get_demo_link() -> Optional[str]:
    data = _read_json(DEMO_LINK, {})
    return data.get("link") if isinstance(data, dict) else None


def is_demo_link(link: str) -> bool:
    return bool(link) and link == get_demo_link()


# --- outgoing command idempotency ----------------------------------------
def claim_command(
    key: str, lease_seconds: int = _COMMAND_LEASE_SECONDS
) -> Optional[str]:
    """Claim one command and return its fencing token, or ``None``."""
    if not key:
        return None
    now = time.time()
    token = uuid.uuid4().hex

    def update(claims):
        if not isinstance(claims, dict):
            claims = {}
        claims = {
            claim_key: claim
            for claim_key, claim in claims.items()
            if now - float(claim.get("ts", 0) or 0) < _COMMAND_TTL
        }
        current = claims.get(key)
        if current:
            if current.get("status") == "completed":
                return claims, None
            if float(current.get("lease_until", 0) or 0) > now:
                return claims, None
        claims[key] = {
            "status": "processing",
            "token": token,
            "lease_until": now + max(30, int(lease_seconds)),
            "ts": now,
        }
        return claims, token

    return _mutate(COMMANDS, {}, update)


def renew_command(
    key: str, token: str, lease_seconds: int = _COMMAND_LEASE_SECONDS
) -> bool:
    now = time.time()

    def update(claims):
        if not isinstance(claims, dict):
            claims = {}
        claim = claims.get(key)
        if (
            not claim
            or claim.get("status") != "processing"
            or claim.get("token") != token
            or float(claim.get("lease_until", 0) or 0) <= now
        ):
            return claims, False
        claim["lease_until"] = now + max(30, int(lease_seconds))
        claim["ts"] = now
        return claims, True

    return bool(_mutate(COMMANDS, {}, update))


def complete_command(key: str, token: str) -> bool:
    """Complete a command only when the caller still owns its fencing token."""
    now = time.time()

    def update(claims):
        if not isinstance(claims, dict):
            claims = {}
        claim = claims.get(key)
        if (
            not claim
            or claim.get("status") != "processing"
            or claim.get("token") != token
        ):
            return claims, False
        claims[key] = {"status": "completed", "lease_until": 0, "ts": now}
        return claims, True

    return bool(_mutate(COMMANDS, {}, update))


# --- emergency invite revocation journal ---------------------------------
def queue_revoke(chat_id: int, link: str) -> str:
    """Durably retain a link when its primary database write failed."""
    key = f"{int(chat_id)}:{link}"

    def update(items):
        if not isinstance(items, dict):
            items = {}
        items[key] = {"chat_id": int(chat_id), "link": link, "ts": time.time()}
        return items, key

    return _mutate(REVOKES, {}, update)


def pending_revokes() -> list[dict]:
    items = _read_json(REVOKES, {})
    return list(items.values()) if isinstance(items, dict) else []


def complete_revoke(chat_id: int, link: str) -> None:
    key = f"{int(chat_id)}:{link}"

    def update(items):
        if not isinstance(items, dict):
            items = {}
        items.pop(key, None)
        return items, None

    _mutate(REVOKES, {}, update)


# --- reservation bridge --------------------------------------------------
def _clean_queries(queries) -> list[str]:
    if isinstance(queries, str):
        queries = [queries]
    clean = []
    seen = set()
    for value in queries or []:
        query = str(value).strip()
        key = query.casefold()
        if query and key not in seen:
            seen.add(key)
            clean.append(query)
    return clean


def request_links(queries, user_id: int, request_key: str = "") -> str:
    """Queue one batched bot request, reusing an existing caller identity."""
    clean = _clean_queries(queries)
    if not clean:
        raise ValueError("at least one group keyword is required")
    rid = uuid.uuid4().hex
    request_key = str(request_key or "").strip()
    now = time.time()

    def update(reqs):
        if not isinstance(reqs, list):
            reqs = []
        reqs = [
            req for req in reqs
            if now - float(req.get("ts", 0) or 0) < _TTL
        ]
        if request_key:
            existing = next(
                (
                    req for req in reqs
                    if req.get("request_key") == request_key
                    and req.get("status") != "cancelled"
                ),
                None,
            )
            if existing:
                return reqs, str(existing["id"])
        reqs.append({
            "id": rid,
            "request_key": request_key,
            "queries": clean,
            "user_id": int(user_id),
            "status": "pending",
            "attempts": 0,
            "lease_until": 0,
            "ts": now,
        })
        return reqs[-_MAX_ENTRIES:], rid

    return _mutate(REQUESTS, [], update)


def request_link(query: str, user_id: int) -> str:
    """Compatibility wrapper for older callers."""
    return request_links([query], user_id)


def pending_requests() -> list:
    """Read active requests without claiming them (diagnostics only)."""
    reqs = _read_json(REQUESTS, [])
    if not isinstance(reqs, list):
        return []
    return [
        req for req in reqs
        if req.get("status", "pending") not in {"completed", "cancelled"}
    ]


def claim_requests(limit: int = 10, lease_seconds: int = _LEASE_SECONDS) -> list:
    """Atomically lease work; each claim gets a unique fencing token."""
    now = time.time()

    def update(reqs):
        if not isinstance(reqs, list):
            reqs = []
        claimed = []
        kept = []
        for req in reqs:
            if now - float(req.get("ts", 0) or 0) >= _TTL:
                continue
            status = req.get("status", "pending")
            expired = float(req.get("lease_until", 0) or 0) <= now
            if (
                len(claimed) < max(1, int(limit))
                and status != "cancelled"
                and (status == "pending" or (status == "processing" and expired))
            ):
                token = uuid.uuid4().hex
                req["status"] = "processing"
                req["lease_token"] = token
                req["lease_until"] = now + max(10, int(lease_seconds))
                req["attempts"] = int(req.get("attempts", 0) or 0) + 1
                claimed.append(dict(req))
            kept.append(req)
        return kept[-_MAX_ENTRIES:], claimed

    return _mutate(REQUESTS, [], update)


def renew_request(
    rid: str, lease_token: str, lease_seconds: int = _LEASE_SECONDS
) -> bool:
    now = time.time()

    def update(reqs):
        if not isinstance(reqs, list):
            reqs = []
        renewed = False
        for req in reqs:
            if (
                req.get("id") == rid
                and req.get("status") == "processing"
                and req.get("lease_token") == lease_token
                and float(req.get("lease_until", 0) or 0) > now
            ):
                req["lease_until"] = now + max(10, int(lease_seconds))
                renewed = True
                break
        return reqs, renewed

    return bool(_mutate(REQUESTS, [], update))


def _set_request_state(
    rid: str,
    status: str,
    error: str = "",
    lease_token: str | None = None,
) -> bool:
    now = time.time()

    def update(reqs):
        if not isinstance(reqs, list):
            reqs = []
        changed = False
        for req in reqs:
            if req.get("id") != rid:
                continue
            if lease_token:
                if (
                    req.get("status") != "processing"
                    or req.get("lease_token") != lease_token
                    or float(req.get("lease_until", 0) or 0) <= now
                ):
                    break
            req["status"] = status
            req["lease_until"] = 0
            req.pop("lease_token", None)
            if error:
                req["error"] = error[:500]
            else:
                req.pop("error", None)
            changed = True
            break
        return reqs, changed

    return bool(_mutate(REQUESTS, [], update))


def complete_request(rid: str, lease_token: str | None = None) -> bool:
    return _set_request_state(rid, "completed", lease_token=lease_token)


def release_request(
    rid: str, lease_token: str, error: str = ""
) -> bool:
    return _set_request_state(rid, "pending", error, lease_token)


def cancel_request(rid: str, force: bool = False) -> bool:
    """Cancel a request; ``force`` also compensates an already returned result."""
    with _exclusive_many((REQUESTS, RESULTS)):
        reqs = _read_json(REQUESTS, [])
        results = _read_json(RESULTS, {})
        if not isinstance(results, dict):
            results = {}
        if rid in results and not force:
            return False
        if not isinstance(reqs, list):
            reqs = []
        changed = False
        for req in reqs:
            if req.get("id") != rid:
                continue
            if req.get("status") == "completed" and not force:
                break
            req["status"] = "cancelled"
            req["lease_until"] = 0
            req.pop("lease_token", None)
            changed = True
            break
        if force and rid in results:
            results.pop(rid, None)
            _write_json(RESULTS, results)
            changed = True
        if changed:
            _write_json(REQUESTS, reqs)
        return changed


def is_request_cancelled(rid: str) -> bool:
    reqs = _read_json(REQUESTS, [])
    return any(
        req.get("id") == rid and req.get("status") == "cancelled"
        for req in reqs if isinstance(req, dict)
    ) if isinstance(reqs, list) else False


def cancelled_request_ids() -> list[str]:
    """Return cancelled bridge requests for bot-side reservation cleanup."""
    reqs = _read_json(REQUESTS, [])
    if not isinstance(reqs, list):
        return []
    return [
        str(req.get("id")) for req in reqs
        if isinstance(req, dict)
        and req.get("id")
        and req.get("status") == "cancelled"
    ]


def has_result(rid: str) -> bool:
    results = _read_json(RESULTS, {})
    return isinstance(results, dict) and rid in results


def put_result(
    rid: str, entries, failures=None, lease_token: str | None = None
) -> bool:
    """Commit a result only for the current lease and an active request."""
    now = time.time()
    clean_entries = []
    for entry in entries or []:
        if isinstance(entry, dict) and entry.get("link"):
            clean_entries.append({
                "link": entry["link"],
                "title": entry.get("title", ""),
                "keyword": entry.get("keyword", ""),
            })
    clean_failures = []
    for failure in failures or []:
        if isinstance(failure, dict) and failure.get("keyword"):
            clean_failures.append({
                "keyword": str(failure["keyword"]),
                "reason": str(failure.get("reason", "failed"))[:300],
            })

    with _exclusive_many((REQUESTS, RESULTS)):
        reqs = _read_json(REQUESTS, [])
        results = _read_json(RESULTS, {})
        if not isinstance(reqs, list):
            reqs = []
        request = next((req for req in reqs if req.get("id") == rid), None)
        if lease_token:
            if (
                request is None
                or request.get("status") != "processing"
                or request.get("lease_token") != lease_token
                or float(request.get("lease_until", 0) or 0) <= now
            ):
                return False
        elif request is not None and request.get("status") == "cancelled":
            return False
        if not isinstance(results, dict):
            results = {}
        results[rid] = {
            "entries": clean_entries,
            "failures": clean_failures,
            "ts": now,
        }
        results = {
            key: value for key, value in results.items()
            if now - float(value.get("ts", 0) or 0) < _TTL
        }
        if request is not None:
            request["status"] = "completed"
            request["lease_until"] = 0
            request.pop("lease_token", None)
            request.pop("error", None)
        _write_json(RESULTS, results)
        _write_json(REQUESTS, reqs)
        return True


def get_result_details(rid: str) -> Optional[dict]:
    results = _read_json(RESULTS, {})
    if not isinstance(results, dict):
        return None
    result = results.get(rid)
    return dict(result) if isinstance(result, dict) else None


def get_result(rid: str):
    """Compatibility API returning only successful entries."""
    result = get_result_details(rid)
    return result.get("entries", []) if result is not None else None
