from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

import guard
import infra
import linkstore
import quickreply
from bot import db


def test_amounts_are_positive_finite_and_round_to_paise() -> None:
    for raw in ("nan", "inf", "0", "-1"):
        with pytest.raises(ValueError):
            quickreply.parse_amount(raw)

    amount = quickreply.parse_amount("1.999")
    assert str(amount) == "2.00"
    assert quickreply.fmt_inr(amount) == "₹2"


def test_bridge_deduplicates_commands_and_reservation_requests(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(linkstore, "COMMANDS", tmp_path / "commands.json")
    monkeypatch.setattr(linkstore, "REQUESTS", tmp_path / "requests.json")
    monkeypatch.setattr(linkstore, "RESULTS", tmp_path / "results.json")
    monkeypatch.setattr(linkstore, "REVOKES", tmp_path / "revokes.json")

    token = linkstore.claim_command("account:chat:message")
    assert token
    assert linkstore.claim_command("account:chat:message") is None
    assert linkstore.renew_command("account:chat:message", token)
    assert not linkstore.complete_command("account:chat:message", "stale-token")
    assert linkstore.complete_command("account:chat:message", token)
    assert linkstore.claim_command("account:chat:message") is None

    first = linkstore.request_links(
        ["cp"],
        42,
        request_key="command",
        metadata={"order_id": "ANI-CANONICAL", "amount": "25.00"},
    )
    second = linkstore.request_links(
        ["cp"],
        42,
        request_key="command",
        metadata={"order_id": "ANI-DIFFERENT", "amount": "99.00"},
    )
    assert first == second
    assert linkstore.get_request_details(first)["metadata"] == {
        "order_id": "ANI-CANONICAL",
        "amount": "25.00",
    }

    assert linkstore.put_result(
        first, [{"link": "https://t.me/+x", "title": "Group", "keyword": "cp"}]
    )
    assert linkstore.cancel_request(first, force=True)
    assert linkstore.is_request_cancelled(first)
    assert not linkstore.has_result(first)
    assert linkstore.complete_cancellation(first)

    linkstore.queue_revoke(-1001, "https://t.me/+orphan")
    assert linkstore.pending_revokes() == [
        {"chat_id": -1001, "link": "https://t.me/+orphan", "ts": pytest.approx(
            linkstore.pending_revokes()[0]["ts"]
        )}
    ]
    linkstore.complete_revoke(-1001, "https://t.me/+orphan")
    assert linkstore.pending_revokes() == []

    assert linkstore.cancel_request("aged-out", force=True)
    assert linkstore.cancelled_request_ids() == ["aged-out"]
    assert linkstore.complete_cancellation("aged-out")
    assert linkstore.cancelled_request_ids() == []


def test_order_allocation_reservation_fencing_and_reconciliation(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        await db.close()
        monkeypatch.setattr(db.config, "DB_PATH", tmp_path / "bot.db")
        await db.init()
        try:
            order_ids = await asyncio.gather(
                *(db.create_next_order("ANI", str(i), "owner", "cp") for i in range(8))
            )
            assert order_ids == [f"ANI{i:04d}" for i in range(1, 9)]

            assert await db.add_reservation(
                "request", "cp", -1001, 42, "https://t.me/+reserved"
            )
            reservation = await db.reservation_for_join(
                -1001, 42, "https://t.me/+reserved"
            )
            assert reservation is not None
            assert await db.claim_reservation_status(
                reservation["id"], {"pending"}, "approving"
            )
            assert not await db.claim_reservation_status(
                reservation["id"], {"pending"}, "expiring"
            )

            first_link = await db.add_order_link(
                "ANI0001", -1001, "https://t.me/+order"
            )
            await db.add_order_link("ANI0001", -1002, "https://t.me/+sibling")
            await db.set_order_link_joined(first_link, 42)
            await db.set_order_status("ANI0001", "joined_revoke_pending")
            pending_links = await db.pending_order_link_revocations()
            assert [row["id"] for row in pending_links] == [first_link]
            await db.set_order_link_revoked(first_link)
            await db.reconcile_order_status("ANI0001")
            assert (await db.get_order("ANI0001"))["status"] == "joined"
            assert len([row for row in await db.order_links("ANI0001") if not row["revoked"]]) == 1
        finally:
            await db.close()

    asyncio.run(scenario())


def test_database_migrates_existing_order_and_reservation_tables(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE orders (
            order_id TEXT PRIMARY KEY,
            amount TEXT NOT NULL DEFAULT '',
            account_name TEXT NOT NULL DEFAULT '',
            keyword TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open',
            created_at REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE order_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            invite_link TEXT NOT NULL,
            joined_user INTEGER,
            revoked INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_id TEXT NOT NULL,
            keyword TEXT NOT NULL DEFAULT '',
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            invite_link TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending',
            last_error TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL DEFAULT 0,
            approved_at REAL,
            revoked_at REAL,
            UNIQUE (request_id, keyword, chat_id)
        );
        """
    )
    connection.close()

    async def scenario() -> None:
        await db.close()
        monkeypatch.setattr(db.config, "DB_PATH", path)
        await db.init()
        try:
            cur = await db._db().execute("PRAGMA table_info(orders)")
            order_columns = {row["name"] for row in await cur.fetchall()}
            cur = await db._db().execute("PRAGMA table_info(order_links)")
            link_columns = {row["name"] for row in await cur.fetchall()}
            cur = await db._db().execute("PRAGMA table_info(reservations)")
            reservation_columns = {row["name"] for row in await cur.fetchall()}
            assert {"command_key", "source", "buyer_id", "request_id"} <= order_columns
            assert "buyer_removed" in link_columns
            assert "order_id" in reservation_columns
        finally:
            await db.close()

    asyncio.run(scenario())


def test_guard_unbans_every_page_without_skipping(monkeypatch) -> None:
    async def scenario() -> None:
        banned = list(range(250))
        offsets = []

        class Client:
            async def __call__(self, request):
                if type(request).__name__ == "GetParticipantsRequest":
                    offsets.append(request.offset)
                    return SimpleNamespace(
                        participants=[SimpleNamespace(peer=user) for user in banned[:100]]
                    )
                banned.remove(request.participant)
                return None

        monkeypatch.setattr(guard, "client", Client())
        guard._state["channel"] = "channel"

        async def no_sleep(*_args):
            return None

        monkeypatch.setattr(guard.asyncio, "sleep", no_sleep)
        assert await guard.unban_all() == 250
        assert banned == []
        assert set(offsets) == {0}

    asyncio.run(scenario())


def test_guard_only_deduplicates_concurrent_kicks(monkeypatch) -> None:
    async def scenario() -> None:
        calls = []
        release = asyncio.Event()

        async def fake_kick(user_id):
            calls.append(user_id)
            await release.wait()
            return True

        monkeypatch.setattr(guard, "kick", fake_kick)
        first = asyncio.create_task(guard._kick_once(42))
        await asyncio.sleep(0)
        await guard._kick_once(42)
        release.set()
        await first
        assert calls == [42]

        release.clear()
        second = asyncio.create_task(guard._kick_once(42))
        await asyncio.sleep(0)
        release.set()
        await second
        assert calls == [42, 42]

    asyncio.run(scenario())



def test_add_without_links_cancels_the_bridge_request(monkeypatch) -> None:
    async def scenario() -> None:
        quickreply._state["self_id"] = 1
        quickreply._pay = quickreply._default_pay()
        cancelled = []
        edits = []

        async def reserve(*_args, **_kwargs):
            return {
                "request_id": "timed-out-request",
                "entries": [],
                "failures": [{"keyword": "bridge", "reason": "timed out"}],
            }

        async def cancel(request_id):
            cancelled.append(request_id)

        monkeypatch.setattr(quickreply, "_reserve_links", reserve)
        monkeypatch.setattr(quickreply, "_cancel_reserved_links", cancel)

        class Event:
            raw_text = "/add 10 Bob cp"
            chat_id = 42
            id = 77
            is_private = True

            async def get_reply_message(self):
                return None

            async def edit(self, text, **_kwargs):
                edits.append(text)

            async def respond(self, text, **_kwargs):
                edits.append(text)

        await quickreply.cmd_add(Event())
        assert cancelled == ["timed-out-request"]
        assert len(edits) == 1
        assert quickreply._pay["payments"] == []

    asyncio.run(scenario())


def test_cancelled_completed_reservation_removes_buyer(tmp_path, monkeypatch) -> None:
    async def scenario() -> None:
        from bot import app

        await db.close()
        monkeypatch.setattr(db.config, "DB_PATH", tmp_path / "cancel.db")
        await db.init()
        calls = []
        try:
            assert await db.register_order(
                "ANICANCEL",
                "25.00",
                "Bob",
                "cp",
                request_id="payment",
                buyer_id=42,
            )
            assert await db.add_reservation(
                "payment",
                "cp",
                -1001,
                42,
                "https://t.me/+paid",
                "ANICANCEL",
            )
            await db.add_order_link(
                "ANICANCEL", -1001, "https://t.me/+paid"
            )
            reservation = await db.reservation_for_join(
                -1001, 42, "https://t.me/+paid"
            )
            await db.set_reservation_status(reservation["id"], "completed")

            async def ban(chat_id, user_id):
                calls.append(("ban", chat_id, user_id))

            async def unban(chat_id, user_id, **_kwargs):
                calls.append(("unban", chat_id, user_id))

            async def revoked(*_args):
                return True

            monkeypatch.setattr(app.bot, "ban_chat_member", ban)
            monkeypatch.setattr(app.bot, "unban_chat_member", unban)
            monkeypatch.setattr(app, "revoke_link", revoked)
            monkeypatch.setattr(app, "linkstore", None)
            await app._cancel_request_reservations("payment")

            current = await db.get_reservation(reservation["id"])
            assert current["status"] == "cancelled"
            assert calls == [("ban", -1001, 42), ("unban", -1001, 42)]
            order_link = (await db.order_links("ANICANCEL"))[0]
            assert order_link["revoked"] == 1
            assert order_link["buyer_removed"] == 1
            assert (await db.get_order("ANICANCEL"))["status"] == "revoked"
        finally:
            await db.close()
            await app.bot.session.close()

    asyncio.run(scenario())



def test_channel_post_failure_keeps_committed_payment_and_one_receipt(monkeypatch) -> None:
    async def scenario() -> None:
        quickreply._state["self_id"] = 1
        quickreply._pay = quickreply._default_pay()
        quickreply._pay["post_channel"] = -1001
        edits = []
        cancelled = []

        async def reserve(*_args, **_kwargs):
            return {
                "request_id": "request",
                "entries": [
                    {"link": "https://t.me/+paid", "title": "Group", "keyword": "cp"}
                ],
                "failures": [],
            }

        async def render(kind, *_args, **_kwargs):
            return (f"{kind} receipt", [])

        async def cancel(request_id):
            cancelled.append(request_id)

        class Client:
            async def send_file(self, *_args, **_kwargs):
                raise RuntimeError("channel unavailable")

        monkeypatch.setattr(quickreply, "_reserve_links", reserve)
        monkeypatch.setattr(quickreply, "_render_multi", render)
        monkeypatch.setattr(quickreply, "_cancel_reserved_links", cancel)
        monkeypatch.setattr(quickreply, "_save_pay", lambda: None)
        monkeypatch.setattr(quickreply, "client", Client())

        class Event:
            raw_text = "/add 10 Bob cp"
            chat_id = 42
            id = 78
            is_private = True

            async def get_reply_message(self):
                return SimpleNamespace(media=object(), sender_id=42)

            async def edit(self, text, **_kwargs):
                edits.append(text)

            async def respond(self, text, **_kwargs):
                edits.append(text)

        await quickreply.cmd_add(Event())
        assert len(edits) == 1
        assert edits[0].startswith("done receipt")
        assert "Order ID: ANI" in edits[0]
        assert "Amount: ₹10" in edits[0]
        assert cancelled == []
        payment = quickreply._pay["payments"][0]
        assert payment["status"] == "valid"
        assert payment["post_status"] == "failed"

    asyncio.run(scenario())



def test_add_fits_channel_caption_and_preserves_order_id(monkeypatch) -> None:
    async def scenario() -> None:
        quickreply._state["self_id"] = 1
        quickreply._pay = quickreply._default_pay()
        quickreply._pay["post_channel"] = -1001
        quickreply._pay["channel_template"] = ("x" * 1020) + " {orderid}"
        sent = []
        edits = []

        async def reserve(*_args, **_kwargs):
            return {
                "request_id": "request",
                "entries": [
                    {"link": "https://t.me/+paid", "title": "Group", "keyword": "cp"}
                ],
                "failures": [],
            }

        class Client:
            async def send_file(self, *_args, **kwargs):
                sent.append(kwargs)
                return SimpleNamespace(id=123)

        monkeypatch.setattr(quickreply, "_reserve_links", reserve)
        monkeypatch.setattr(quickreply, "_save_pay", lambda: None)
        monkeypatch.setattr(quickreply, "client", Client())

        class Event:
            raw_text = "/add 10 Bob cp"
            chat_id = 42
            id = 79
            is_private = True

            async def get_reply_message(self):
                return SimpleNamespace(media=object(), sender_id=42)

            async def edit(self, text, **_kwargs):
                edits.append(text)

            async def respond(self, text, **_kwargs):
                edits.append(text)

        await quickreply.cmd_add(Event())

        assert len(edits) == 1
        assert len(sent) == 1
        payment = quickreply._pay["payments"][0]
        caption = sent[0]["caption"]
        assert payment["status"] == "valid"
        assert payment["post_status"] == "posted"
        assert payment["channel_caption_truncated"] is True
        assert payment["order_id"] in caption
        assert quickreply._u16(caption) <= 1024
        assert all(
            entity.offset + entity.length <= quickreply._u16(caption)
            for entity in (sent[0]["formatting_entities"] or [])
        )

    asyncio.run(scenario())


def test_media_caption_limit_uses_utf16_units() -> None:
    exact = "😀" * 512
    text, entities, clipped = quickreply._fit_media_caption(exact, [])
    assert text == exact
    assert entities == []
    assert clipped is False

    text, entities, clipped = quickreply._fit_media_caption(exact + "x", [])
    assert quickreply._u16(text) == 1024
    assert entities == []
    assert clipped is True


def test_quickreply_account_lock_is_shared_across_checkouts(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(
        quickreply.tempfile, "gettempdir", lambda: str(tmp_path)
    )
    lock_path = quickreply._account_instance_lock_path(42)
    assert lock_path == tmp_path / "channelguard-quickreply-account-42.lock"
    assert quickreply._account_instance_lock_path(42) == lock_path
    assert quickreply._account_instance_lock_path(43) != lock_path

    original_handles = quickreply._instance_locks
    quickreply._instance_locks = []
    try:
        assert quickreply._acquire_single_instance_lock(lock_path)
        assert not quickreply._acquire_single_instance_lock(lock_path)
    finally:
        for handle in quickreply._instance_locks:
            handle.close()
        quickreply._instance_locks = original_handles


def test_order_id_token_aliases_render_with_valid_entities() -> None:
    alias = "｛ ORDER\u200b_ID ｝"
    amount_alias = "｛ AMOUNT ｝"
    template = f"Order: {alias} Amount: {amount_alias}"
    entity = quickreply.MessageEntityBold(
        offset=quickreply._u16("Order: "),
        length=quickreply._u16(alias),
    )
    quickreply._pay = quickreply._default_pay()
    quickreply._pay["channel_template"] = template
    quickreply._pay["channel_entities"] = quickreply._serialize_entities([entity])

    text, entities = asyncio.run(
        quickreply._render(
            "channel", quickreply.Decimal("10"), "Bob", order_id="ANIABC234"
        )
    )

    assert text == "Order: ANIABC234 Amount: ₹10"
    assert len(entities) == 1
    assert entities[0].offset == quickreply._u16("Order: ")
    assert entities[0].length == quickreply._u16("ANIABC234")


@pytest.mark.parametrize("amount_token", ["14", "14INR", "₹14", "14.00"])
def test_exact_photo_add_renders_14_and_order_id_everywhere(
    monkeypatch, amount_token
) -> None:
    async def scenario() -> None:
        template = (
            "Thanks For Paying 🎭\n"
            "We Have Successfully Received Your Payment Of {amount}\n\n"
            "Order ID : ❴ ORDER\u2060_ID ❵ 🔥\n\n"
            "Hold On ! We Are Preparing Your Links Now ♻️"
        )
        quickreply._state["self_id"] = 1
        quickreply._pay = quickreply._default_pay()
        quickreply._pay["post_channel"] = -1001
        quickreply._pay["done_template"] = template
        quickreply._pay["channel_template"] = template
        private = []
        channel = []

        class Client:
            async def send_file(self, *_args, **kwargs):
                channel.append(kwargs["caption"])
                return SimpleNamespace(id=123)

        class Event:
            raw_text = f"/add {amount_token} Amit"
            chat_id = 42
            id = 80
            is_private = True

            async def get_reply_message(self):
                return SimpleNamespace(media=object(), sender_id=42)

            async def edit(self, text, **_kwargs):
                private.append(text)

            async def respond(self, text, **_kwargs):
                private.append(text)

        monkeypatch.setattr(quickreply, "_save_pay", lambda: None)
        monkeypatch.setattr(quickreply, "client", Client())

        await quickreply.cmd_add(Event())

        assert len(private) == 1
        assert len(channel) == 1
        order_id = quickreply._pay["payments"][0]["order_id"]
        assert quickreply._pay["payments"][0]["amount"] == "14.00"
        for rendered in (private[0], channel[0]):
            assert "₹14" in rendered
            assert order_id in rendered
            assert "{amount}" not in rendered.casefold()
            assert "orderid" not in rendered.casefold()
            assert "order_id" not in rendered.casefold()

    asyncio.run(scenario())


def test_partial_group_failure_renders_values_privately_and_in_channel(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        quickreply._state["self_id"] = 1
        quickreply._pay = quickreply._default_pay()
        quickreply._pay["post_channel"] = -1001
        quickreply._pay["done_template"] = "{amount}|{orderid}|{link}"
        quickreply._pay["channel_template"] = "{amount}|{orderid}|{link}"
        edits = []
        posts = []

        async def reserve(*_args, **_kwargs):
            return {
                "request_id": "request",
                "metadata": {"order_id": "ANIFIXED"},
                "entries": [
                    {
                        "link": "https://t.me/+working",
                        "title": "Working",
                        "keyword": "ok",
                    }
                ],
                "failures": [
                    {"keyword": "bad", "reason": "group unavailable"}
                ],
            }

        class Client:
            async def send_file(self, *_args, **kwargs):
                posts.append(kwargs["caption"])
                return SimpleNamespace(id=123)

        monkeypatch.setattr(quickreply, "_reserve_links", reserve)
        monkeypatch.setattr(quickreply, "_save_pay", lambda: None)
        monkeypatch.setattr(quickreply, "client", Client())

        class Event:
            raw_text = "/add 25INR Bob ok bad"
            chat_id = 42
            id = 80
            is_private = True

            async def get_reply_message(self):
                return SimpleNamespace(media=object(), sender_id=42)

            async def edit(self, text, **_kwargs):
                edits.append(text)

            async def respond(self, text, **_kwargs):
                edits.append(text)

        await quickreply.cmd_add(Event())

        assert len(edits) == 1
        assert len(posts) == 1
        for rendered in (edits[0], posts[0]):
            assert "₹25" in rendered
            assert "ANIFIXED" in rendered
            assert "https://t.me/+working" in rendered
            assert "Unavailable: bad: group unavailable" in rendered

    asyncio.run(scenario())


def test_cancel_queues_access_cleanup_even_when_caption_edit_fails(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        quickreply._pay = quickreply._default_pay()
        quickreply._pay["payments"] = [
            {
                "amount": "25.00",
                "name": "Bob",
                "order_id": "ANIFIXED",
                "reservation_request_id": "request",
                "post_chat_id": -1001,
                "post_message_id": 10,
                "status": "valid",
                "ts": quickreply._now_ts(),
            }
        ]
        cleanup = []
        replies = []

        async def cancel(request_id):
            cleanup.append(request_id)
            return True

        class Client:
            async def edit_message(self, *_args, **_kwargs):
                raise RuntimeError("edit rejected")

            async def get_messages(self, *_args, **_kwargs):
                return SimpleNamespace(message="Original caption")

        monkeypatch.setattr(quickreply, "_cancel_reserved_links", cancel)
        monkeypatch.setattr(quickreply, "_save_pay", lambda: None)
        monkeypatch.setattr(quickreply, "client", Client())

        class Event:
            chat_id = -1001
            id = 11

            async def get_reply_message(self):
                return SimpleNamespace(
                    id=10, message="Original caption", entities=[]
                )

            async def get_input_chat(self):
                return "channel"

            async def edit(self, text, **_kwargs):
                replies.append(text)

            async def respond(self, text, **_kwargs):
                replies.append(text)

        await quickreply.cmd_cancel(Event())

        payment = quickreply._pay["payments"][0]
        assert cleanup == ["request"]
        assert payment["status"] == "cancel_pending"
        assert payment["reservation_cleanup"] == "queued"
        assert "Access cancellation is queued" in replies[0]

    asyncio.run(scenario())


def test_admin_add_is_idempotent_and_posts_partial_failure_details(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        from bot import app

        await db.close()
        monkeypatch.setattr(db.config, "DB_PATH", tmp_path / "direct-add.db")
        await db.init()
        private = []
        channel = []
        try:
            await db.upsert_group(
                -1001, "Working", "ok", "supergroup", None, None, True
            )
            await db.upsert_group(
                -1002, "Broken", "bad", "supergroup", None, None, True
            )

            async def create(chat_id):
                return "https://t.me/+working" if chat_id == -1001 else None

            async def channel_send(_chat, text, **_kwargs):
                channel.append(text)

            monkeypatch.setattr(app, "owner_only", lambda _message: True)
            monkeypatch.setattr(app, "create_single_use_link", create)
            monkeypatch.setattr(app.bot, "send_message", channel_send)
            monkeypatch.setattr(app.config, "PAYMENT_CHANNEL", "-1009")

            class Message:
                chat = SimpleNamespace(id=7)
                message_id = 99

                async def answer(self, text, **_kwargs):
                    private.append(text)

            command = SimpleNamespace(args="25 Bob all")
            await app.cmd_add(Message(), command)
            await app.cmd_add(Message(), command)

            orders = await db.all_orders()
            links = await db.order_links(orders[0]["order_id"])
            assert len(orders) == 1
            assert len(links) == 1
            assert orders[0]["order_id"] in "\n".join(private)
            assert "Amount <code>25</code>" in "\n".join(private)
            assert "https://t.me/+working" in "\n".join(private)
            assert "Unavailable" in "\n".join(private)
            assert "Broken" in "\n".join(channel)
            assert orders[0]["order_id"] in "\n".join(channel)
        finally:
            await db.close()

    asyncio.run(scenario())


def test_infra_has_fixed_services_host_lock_and_bounded_restarts(
    tmp_path,
) -> None:
    assert [spec.command[1:] for spec in infra.SERVICES] == [
        ("guard.py",),
        ("quickreply.py",),
        ("-m", "bot"),
    ]

    first = infra.InstanceLock(tmp_path / "infra.lock")
    second = infra.InstanceLock(tmp_path / "infra.lock")
    try:
        assert first.acquire()
        assert not second.acquire()
    finally:
        first.close()
        second.close()

    clock = [0.0]
    supervisor = infra.Supervisor(monotonic=lambda: clock[0])
    state = supervisor.states[0]
    for _ in range(infra.MAX_RESTARTS_PER_WINDOW + 1):
        supervisor._schedule_restart(state, 0)
        clock[0] += 0.1
    assert supervisor._stopping is True
    assert "exited too often" in supervisor._fatal
