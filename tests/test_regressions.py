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
            assert await db.pending_order_link_revocations() == []
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
            assert {
                "command_key",
                "source",
                "buyer_id",
                "request_id",
                "private_sent",
                "channel_sent",
                "channel_error",
                "channel_html",
            } <= order_columns
            assert {"buyer_removed", "buyer_banned"} <= link_columns
            assert "order_id" in reservation_columns
        finally:
            await db.close()

    asyncio.run(scenario())


def test_guard_direct_join_removal_is_a_permanent_ban(monkeypatch) -> None:
    async def scenario() -> None:
        requests = []

        class Client:
            async def get_input_entity(self, user_id):
                return f"peer:{user_id}"

            async def __call__(self, request):
                requests.append(request)

        monkeypatch.setattr(guard, "client", Client())
        guard._state["channel"] = "channel"
        guard._state["self_id"] = 1
        guard._state["owner_id"] = 2
        assert await guard.kick(42, announce=False)
        assert len(requests) == 1
        assert type(requests[0]).__name__ == "EditBannedRequest"
        assert requests[0].participant == "peer:42"
        assert requests[0].banned_rights.view_messages is True

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
            "We Have Successfully Received Your Payment Of ₹1\n\n"
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
            assert "Payment Of ₹14" in rendered
            assert order_id in rendered
            assert "{amount}" not in rendered.casefold()
            assert "orderid" not in rendered.casefold()
            assert "order_id" not in rendered.casefold()
            assert "\n\nAmount: ₹14" not in rendered

    asyncio.run(scenario())


def test_payment_channel_survives_data_directory_replacement(
    monkeypatch,
) -> None:
    monkeypatch.setattr(quickreply.config, "PAYMENT_CHANNEL_RAW", "-1001234567890")

    fresh = quickreply._default_pay()
    loaded, _repairs = quickreply._normalize_pay_data(
        {
            "post_channel": None,
            "done_template": quickreply.DEFAULT_DONE,
            "channel_template": quickreply.DEFAULT_CHANNEL,
            "payments": [],
        }
    )

    assert fresh["post_channel"] == -1001234567890
    assert loaded["post_channel"] == -1001234567890


def test_setchannel_persists_runtime_and_environment(monkeypatch) -> None:
    async def scenario() -> None:
        quickreply._pay = quickreply._default_pay()
        saved = []
        replies = []
        monkeypatch.setattr(
            quickreply.config,
            "save_env",
            lambda values: saved.append(values),
        )
        monkeypatch.setattr(quickreply, "_save_pay", lambda: None)

        class Event:
            is_channel = True
            chat_id = -1009876543210

            async def get_chat(self):
                return SimpleNamespace(title="Payments")

            async def edit(self, text, **_kwargs):
                replies.append(text)

        await quickreply.cmd_setchannel(Event())

        assert quickreply._pay["post_channel"] == -1009876543210
        assert saved == [{"PAYMENT_CHANNEL": "-1009876543210"}]
        assert replies == [
            "Post channel set here: Payments (-1009876543210)"
        ]

    asyncio.run(scenario())


def test_rendered_channel_post_can_be_reused_as_a_live_template() -> None:
    template = (
        "#NEW PAYMENT RECEIVED ! 🎉\n\n"
        "AMOUNT : ₹599\n"
        "MARCO SHARE : ₹2,298.60\n"
        "RIO'S SHARE : ₹2,809.40\n"
        "ORDER ID : {orderid}\n"
        "AMOUNT CREDITED ON : airtel\n\n"
        "PAYMENT COUNT : #25\n"
        "TOTAL : ₹5,108"
    )
    quickreply._pay = quickreply._default_pay()
    quickreply._pay["channel_template"] = template

    text, _entities = asyncio.run(
        quickreply._safe_payment_output(
            "channel",
            quickreply.Decimal("14"),
            "Amit",
            "ANILIVE42",
            [],
            [],
            include_current=True,
        )
    )
    mapping = quickreply._pay_mapping(
        quickreply.Decimal("14"),
        "Amit",
        order_id="ANILIVE42",
        include_current=True,
    )

    assert "AMOUNT : ₹14" in text
    assert f"MARCO SHARE : {mapping['{marco}']}" in text
    assert f"RIO'S SHARE : {mapping['{rioshare}']}" in text
    assert "ORDER ID : ANILIVE42" in text
    assert f"PAYMENT COUNT : #{mapping['{total}']}" in text
    assert f"TOTAL : {mapping['{todaytotal}']}" in text
    assert "₹2,298.60" not in text
    assert "₹2,809.40" not in text
    assert "₹5,108" not in text


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
            assert "Unavailable: bad: group unavailable" in rendered
        assert "https://t.me/+working" in edits[0]
        assert "https://t.me/+working" not in posts[0]
        assert "1 buyer-bound link(s) sent privately" in posts[0]

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
            assert "https://t.me/+working" not in "\n".join(channel)
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


def test_payment_accounting_counts_current_once_and_channel_hides_links() -> None:
    now = quickreply._now_ts()
    quickreply._pay = quickreply._default_pay()
    quickreply._pay["payments"] = [
        {
            "amount": "10.00",
            "name": "First",
            "order_id": "ANIFIRST",
            "status": "valid",
            "ts": now,
        }
    ]
    before_commit = quickreply._pay_mapping(
        quickreply.Decimal("14"),
        "Second",
        "ANISECOND",
        include_current=True,
    )
    assert before_commit["{total}"] == "2"
    assert before_commit["{todaytotal}"] == "₹24"

    quickreply._pay["payments"].append(
        {
            "amount": "14.00",
            "name": "Second",
            "order_id": "ANISECOND",
            "status": "valid",
            "ts": now,
        }
    )
    after_commit = quickreply._required_payment_text(
        quickreply.Decimal("14"),
        "Second",
        "ANISECOND",
        [
            {
                "link": "https://t.me/+private",
                "title": "LOLsi",
                "keyword": "lol",
            }
        ],
        [],
        include_current=False,
        expose_links=False,
    )
    assert "Payments today: 2" in after_commit
    assert "Collected today: ₹24" in after_commit
    assert "https://t.me/+private" not in after_commit
    assert "LOLsi" in after_commit
    sanitized, entities = quickreply._sanitize_channel_invites(
        "Proof https://t.me/+secret", []
    )
    assert "https://t.me/+secret" not in sanitized
    assert entities == []

    long_value = "😀" * 3000
    chunks = quickreply._split_plain_text(
        long_value, header="Order ID: ANISECOND", limit=4000
    )
    assert all(quickreply._u16(chunk) <= 4000 for chunk in chunks)
    assert "".join(
        chunk.removeprefix("Order ID: ANISECOND\n") for chunk in chunks
    ) == long_value


def test_failed_order_cleanup_deletes_only_truly_empty_orders(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        await db.close()
        monkeypatch.setattr(db.config, "DB_PATH", tmp_path / "cleanup.db")
        await db.init()
        try:
            assert await db.register_order(
                "ANICLEAN", "14", "Amit", "lol"
            )
            assert await db.delete_order_if_empty("ANICLEAN")
            assert await db.get_order("ANICLEAN") is None
            assert await db.order_links("ANICLEAN") == []

            assert await db.register_order(
                "ANIAUDIT", "14", "Amit", "lol"
            )
            link_id = await db.add_order_link(
                "ANIAUDIT", -1001, "https://t.me/+cleanup"
            )
            await db.set_order_link_revoked(link_id)
            assert not await db.delete_order_if_empty("ANIAUDIT")
            assert await db.get_order("ANIAUDIT") is not None
        finally:
            await db.close()

    asyncio.run(scenario())


def test_payment_post_reconciles_ambiguous_upload_without_duplicate(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        quickreply._pay = quickreply._default_pay()
        quickreply._pay["post_channel"] = -1001
        payment = {
            "amount": "14.00",
            "name": "Amit",
            "order_id": "ANIRECON",
            "status": "valid",
            "ts": quickreply._now_ts(),
            "post_chat_id": -1001,
            "post_status": "posting",
            "source_chat_id": 42,
            "source_message_id": 9,
        }
        quickreply._pay["payments"] = [payment]
        uploads = []

        class Client:
            async def iter_messages(self, *_args, **_kwargs):
                yield SimpleNamespace(
                    id=777, message="Order ANIRECON", media=object()
                )

            async def send_file(self, *_args, **_kwargs):
                uploads.append(True)

        monkeypatch.setattr(quickreply, "client", Client())
        monkeypatch.setattr(quickreply, "_save_pay", lambda: None)

        assert await quickreply._post_payment_to_channel(payment)
        assert uploads == []
        assert payment["post_status"] == "posted"
        assert payment["post_message_id"] == 777

    asyncio.run(scenario())


def test_add_save_failure_cancels_every_reserved_link(monkeypatch) -> None:
    async def scenario() -> None:
        quickreply._state["self_id"] = 1
        quickreply._pay = quickreply._default_pay()
        cancelled = []
        replies = []

        async def reserve(*_args, **_kwargs):
            return {
                "request_id": "durable-request",
                "entries": [
                    {
                        "link": "https://t.me/+reserved",
                        "title": "LOLsi",
                        "keyword": "lol",
                    }
                ],
                "failures": [],
            }

        async def cancel(request_id):
            cancelled.append(request_id)
            return True

        def fail_save():
            raise OSError("disk full")

        monkeypatch.setattr(quickreply, "_reserve_links", reserve)
        monkeypatch.setattr(quickreply, "_cancel_reserved_links", cancel)
        monkeypatch.setattr(quickreply, "_save_pay", fail_save)

        class Event:
            raw_text = "/add 14 Amit lol"
            chat_id = 42
            id = 81
            is_private = True

            async def get_reply_message(self):
                return None

            async def edit(self, text, **_kwargs):
                replies.append(text)

            async def respond(self, text, **_kwargs):
                replies.append(text)

        await quickreply.cmd_add(Event())

        assert cancelled == ["durable-request"]
        assert quickreply._pay["payments"] == []
        assert "could not be saved" in replies[0]

    asyncio.run(scenario())


def test_group_lookup_prefers_title_words_and_keeps_safe_typo_fallback(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        from bot import app

        await db.close()
        monkeypatch.setattr(db.config, "DB_PATH", tmp_path / "fuzzy.db")
        await db.init()
        try:
            await db.upsert_group(
                -1001, "LOLsi", "Llsi", "supergroup", "lolsi", None, True
            )
            groups, reason = await app._groups_for_keyword("Lolsia")
            assert reason == ""
            assert [group["title"] for group in groups] == ["LOLsi"]

            groups, reason = await app._groups_for_keyword("lolsa")
            assert reason == ""
            assert [group["title"] for group in groups] == ["LOLsi"]

            await db.upsert_group(
                -1002,
                "sʟɪᴍɪᴘ ~ 💗",
                "Indian",
                "supergroup",
                None,
                None,
                True,
            )
            await db.upsert_group(
                -1003,
                "ɪɴᴅɪᴀɴ ᴄᴜᴄ~ 💗",
                "Ic",
                "supergroup",
                None,
                None,
                True,
            )
            await db.upsert_group(
                -1004,
                "ɪɴᴅɪᴀɴ ᴠɪᴘ ᴘᴠᴛ ~ 💗",
                "Iv",
                "supergroup",
                None,
                None,
                True,
            )
            await db.upsert_group(
                -1005,
                "ᴊᴀᴡɴ ~ 💗",
                "Jwn",
                "supergroup",
                None,
                None,
                True,
            )

            expected_indian = {
                "ɪɴᴅɪᴀɴ ᴄᴜᴄ~ 💗",
                "ɪɴᴅɪᴀɴ ᴠɪᴘ ᴘᴠᴛ ~ 💗",
            }
            for query in ("in", "ind", "indi", "indian"):
                groups, reason = await app._groups_for_keyword(query)
                assert reason == ""
                assert {group["title"] for group in groups} == expected_indian

            # Generated short codes stay available as an exact legacy lookup,
            # but cannot pollute stronger human-readable title matches.
            groups, reason = await app._groups_for_keyword("Llsi")
            assert reason == ""
            assert [group["title"] for group in groups] == ["LOLsi"]

            await db.upsert_group(
                -1006, "LOLsa", "Llsa", "supergroup", "lolsa", None, True
            )
            groups, reason = await app._groups_for_keyword("lolsz")
            assert groups == []
            assert "ambiguous" in reason
        finally:
            await db.close()

    asyncio.run(scenario())


def test_quickreply_group_search_uses_the_same_title_prefix_tier(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        dialogs = [
            SimpleNamespace(
                is_group=True,
                is_channel=False,
                name=title,
                entity=SimpleNamespace(username=None),
            )
            for title in (
                "sʟɪᴍɪᴘ ~ 💗",
                "ɪɴᴅɪᴀɴ ᴄᴜᴄ~ 💗",
                "ɪɴᴅɪᴀɴ ᴠɪᴘ ᴘᴠᴛ ~ 💗",
                "ᴊᴀᴡɴ ~ 💗",
            )
        ]

        class Client:
            async def iter_dialogs(self):
                for dialog in dialogs:
                    yield dialog

        monkeypatch.setattr(quickreply, "client", Client())
        matches = await quickreply._find_groups("indi")
        assert {title for _entity, title in matches} == {
            "ɪɴᴅɪᴀɴ ᴄᴜᴄ~ 💗",
            "ɪɴᴅɪᴀɴ ᴠɪᴘ ᴘᴠᴛ ~ 💗",
        }

    asyncio.run(scenario())


def test_remove_order_permanently_bans_joined_buyers_without_unban(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        from bot import app

        await db.close()
        monkeypatch.setattr(db.config, "DB_PATH", tmp_path / "remove.db")
        await db.init()
        calls = []
        answers = []
        try:
            assert await db.register_order(
                "ANIREMOVE", "14", "Amit", "all"
            )
            first = await db.add_order_link(
                "ANIREMOVE", -1001, "https://t.me/+one"
            )
            second = await db.add_order_link(
                "ANIREMOVE", -1002, "https://t.me/+two"
            )
            await db.set_order_link_joined(first, 41)
            await db.set_order_link_joined(second, 42)

            async def ban(chat_id, user_id):
                calls.append(("ban", chat_id, user_id))

            async def unban(chat_id, user_id, **_kwargs):
                calls.append(("unban", chat_id, user_id))

            async def revoke(*_args):
                return True

            monkeypatch.setattr(app, "owner_only", lambda _message: True)
            monkeypatch.setattr(app.bot, "ban_chat_member", ban)
            monkeypatch.setattr(app.bot, "unban_chat_member", unban)
            monkeypatch.setattr(app, "revoke_link", revoke)
            monkeypatch.setattr(app, "linkstore", None)

            class Message:
                async def answer(self, text, **_kwargs):
                    answers.append(text)

            await app.cmd_remove(
                Message(), SimpleNamespace(args="aniremove")
            )
            links = await db.order_links("ANIREMOVE")
            assert calls == [
                ("ban", -1001, 41),
                ("ban", -1002, 42),
            ]
            assert all(row["buyer_banned"] for row in links)
            assert all(row["revoked"] for row in links)
            assert (await db.get_order("ANIREMOVE"))["status"] == "removed"
            assert "permanently banned" in answers[0]
        finally:
            await db.close()

    asyncio.run(scenario())


def test_direct_and_wrong_buyer_joins_are_permanently_banned(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        from bot import app

        await db.close()
        monkeypatch.setattr(db.config, "DB_PATH", tmp_path / "joins.db")
        await db.init()
        banned = []
        notices = []
        try:
            async def ban(chat_id, user_id):
                banned.append((chat_id, user_id))

            async def notify(text, **_kwargs):
                notices.append(text)

            async def revoke(*_args):
                return True

            monkeypatch.setattr(app.bot, "ban_chat_member", ban)
            monkeypatch.setattr(app, "tell_owner", notify)
            monkeypatch.setattr(app, "revoke_link", revoke)
            monkeypatch.setattr(app, "_bot_user_id", 999)

            direct = SimpleNamespace(
                old_chat_member=SimpleNamespace(
                    status=app.ChatMemberStatus.LEFT
                ),
                new_chat_member=SimpleNamespace(
                    status=app.ChatMemberStatus.MEMBER,
                    user=SimpleNamespace(
                        id=51, username=None, full_name="Direct"
                    ),
                ),
                invite_link=None,
                chat=SimpleNamespace(id=-1001, title="LOLsi"),
            )
            await app.on_chat_member(direct)

            assert await db.register_order(
                "ANIBOUND",
                "14",
                "Amit",
                "lol",
                buyer_id=42,
            )
            link_id = await db.add_order_link(
                "ANIBOUND", -1001, "https://t.me/+bound"
            )
            wrong = SimpleNamespace(
                old_chat_member=SimpleNamespace(
                    status=app.ChatMemberStatus.LEFT
                ),
                new_chat_member=SimpleNamespace(
                    status=app.ChatMemberStatus.MEMBER,
                    user=SimpleNamespace(
                        id=52, username=None, full_name="Wrong"
                    ),
                ),
                invite_link=SimpleNamespace(
                    invite_link="https://t.me/+bound"
                ),
                chat=SimpleNamespace(id=-1001, title="LOLsi"),
            )
            await app.on_chat_member(wrong)

            assert banned == [(-1001, 51), (-1001, 52)]
            assert (await db.order_links("ANIBOUND"))[0]["joined_user"] is None
            assert (await db.order_links("ANIBOUND"))[0]["revoked"] == 1
            assert (await db.get_order("ANIBOUND"))["status"] == "compromised"
            assert len(notices) == 2
            ban_rows = await db._all(
                "SELECT * FROM member_bans ORDER BY user_id"
            )
            assert [row["status"] for row in ban_rows] == [
                "completed",
                "completed",
            ]
            assert link_id
        finally:
            await db.close()

    asyncio.run(scenario())


def test_approved_exact_join_request_is_not_banned(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        from bot import app

        await db.close()
        monkeypatch.setattr(db.config, "DB_PATH", tmp_path / "approved.db")
        await db.init()
        banned = []
        try:
            await db.add_join_request(
                -1001,
                61,
                None,
                "Approved",
                "https://t.me/+approved",
            )
            await db.set_join_status(-1001, 61, "approved")

            async def ban(*args):
                banned.append(args)

            monkeypatch.setattr(app.bot, "ban_chat_member", ban)
            monkeypatch.setattr(app, "_bot_user_id", 999)
            update = SimpleNamespace(
                old_chat_member=SimpleNamespace(
                    status=app.ChatMemberStatus.LEFT
                ),
                new_chat_member=SimpleNamespace(
                    status=app.ChatMemberStatus.MEMBER,
                    user=SimpleNamespace(
                        id=61, username=None, full_name="Approved"
                    ),
                ),
                invite_link=SimpleNamespace(
                    invite_link="https://t.me/+approved"
                ),
                chat=SimpleNamespace(id=-1001, title="LOLsi"),
            )
            await app.on_chat_member(update)
            assert banned == []
        finally:
            await db.close()

    asyncio.run(scenario())


def test_exact_amount_detection_and_utf16_html_splitting() -> None:
    from bot import app

    group = {"title": "LOLsi"}
    rendered = app._ensure_order_render(
        "Order ANI1 Amount 14 Account Amit https://t.me/+one",
        order_id="ANI1",
        amount="1",
        account="Amit",
        group=group,
        link="https://t.me/+one",
    )
    assert "Amount <code>1</code>" in rendered

    chunks = []

    async def scenario() -> None:
        await app._send_html_blocks(
            lambda text: _collect(chunks, text), ["😀" * 2100]
        )

    async def _collect(target, text):
        target.append(text)

    asyncio.run(scenario())
    assert len(chunks) == 2
    assert all(app._telegram_units(chunk) <= 4000 for chunk in chunks)
    assert "".join(chunks) == "😀" * 2100



def test_edited_payment_channel_parameter_overrides_stale_runtime_target(
    monkeypatch,
) -> None:
    monkeypatch.setattr(quickreply.config, "PAYMENT_CHANNEL_RAW", "-1002")

    # The operator edited PAYMENT_CHANNEL by hand: the declared parameter wins,
    # otherwise .ping keeps reporting a channel nothing is delivered to.
    moved, note = quickreply._sync_env_post_channel(
        {"post_channel": -1001, "post_channel_env": -1001}
    )
    assert moved is True
    assert "-1002" in note

    # A `.setchannel` whose .env write failed recorded the value .env still
    # holds, so the stale parameter must not drag deliveries back.
    monkeypatch.setattr(quickreply.config, "PAYMENT_CHANNEL_RAW", "-1001")
    kept = {"post_channel": -1002, "post_channel_env": -1001}
    changed, note = quickreply._sync_env_post_channel(kept)
    assert kept["post_channel"] == -1002
    assert (changed, note) == (False, "")


async def _noop_recovery(**_kwargs) -> None:
    return None


def test_setchannel_accepts_a_named_target_and_requeues_pending_posts(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        quickreply._pay = quickreply._default_pay()
        quickreply._pay["post_channel"] = -1001
        stranded = {
            "amount": "14.00",
            "name": "Amit",
            "order_id": "ANIWAITING",
            "status": "valid",
            "ts": quickreply._now_ts(),
            "source_chat_id": 42,
            "source_message_id": 9,
            "post_status": "not_configured",
            "post_failure_notified": True,
            "next_post_retry": quickreply._now_ts() + 600,
        }
        quickreply._pay["payments"] = [stranded]
        saved, replies = [], []

        class Client:
            async def get_entity(self, value):
                assert value == -1009876543210
                return SimpleNamespace(title="Payments", id=9876543210)

        monkeypatch.setattr(quickreply, "client", Client())
        monkeypatch.setattr(quickreply, "_save_pay", lambda: None)
        monkeypatch.setattr(
            quickreply.config, "save_env", lambda values: saved.append(values)
        )
        monkeypatch.setattr(
            quickreply, "_recover_pending_payments", _noop_recovery
        )

        class Event:
            raw_text = ".setchannel -1009876543210"
            is_channel = False

            async def edit(self, text, **_kwargs):
                replies.append(text)

        await quickreply.cmd_setchannel(Event())

        assert quickreply._pay["post_channel"] == -1009876543210
        assert quickreply._pay["post_channel_env"] == -1009876543210
        assert saved == [{"PAYMENT_CHANNEL": "-1009876543210"}]
        assert "Payments (-1009876543210)" in replies[0]
        assert "Re-queued 1 pending delivery(s)." in replies[0]
        assert stranded["post_status"] == "queued"
        assert stranded["next_post_retry"] == 0
        assert "post_failure_notified" not in stranded

    asyncio.run(scenario())


def test_pending_payment_follows_the_current_channel_without_duplicating(
    monkeypatch,
) -> None:
    async def scenario() -> None:
        quickreply._pay = quickreply._default_pay()
        quickreply._pay["post_channel"] = -1002
        monkeypatch.setattr(quickreply, "_save_pay", lambda: None)

        def record(order_id, post_status):
            return {
                "amount": "14.00",
                "name": "Amit",
                "order_id": order_id,
                "status": "valid",
                "ts": quickreply._now_ts(),
                # Recorded while the old channel was configured.
                "post_chat_id": -1001,
                "post_status": post_status,
                "source_chat_id": 42,
                "source_message_id": 9,
            }

        uploads = []

        class MovedClient:
            async def get_messages(self, *_args, **_kwargs):
                return SimpleNamespace(media=object())

            async def send_file(self, target, **_kwargs):
                uploads.append(target)
                return SimpleNamespace(id=555)

        monkeypatch.setattr(quickreply, "client", MovedClient())
        queued = record("ANIQUEUED", "queued")
        assert await quickreply._post_payment_to_channel(queued)
        assert uploads == [-1002]
        assert queued["post_chat_id"] == -1002

        class RetryClient(MovedClient):
            async def iter_messages(self, chat_id, *_args, **_kwargs):
                # The earlier attempt did land - in the previous channel.
                if chat_id == -1001:
                    yield SimpleNamespace(
                        id=777, message="Order ANIRETRY", media=object()
                    )

        monkeypatch.setattr(quickreply, "client", RetryClient())
        retried = record("ANIRETRY", "failed")
        assert await quickreply._post_payment_to_channel(retried)
        assert uploads == [-1002]
        assert retried["post_chat_id"] == -1001
        assert retried["post_message_id"] == 777

    asyncio.run(scenario())


def test_unban_lifts_every_group_ban_and_hands_off_to_the_bot(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        from telethon.tl.functions.channels import EditBannedRequest
        from telethon.tl.types import PeerChannel
        from telethon.utils import get_peer_id

        monkeypatch.setattr(linkstore, "UNBANS", tmp_path / "unbans.json")
        monkeypatch.setattr(quickreply.config, "CHANNEL_RAW", "-1000000000003")
        quickreply._pay = quickreply._default_pay()
        quickreply._pay["payments"] = [
            {
                "amount": "14.00",
                "name": "Amit",
                "order_id": "ANIBANNED",
                "status": "valid",
                "ts": quickreply._now_ts(),
                "payer_id": 42,
                "join_status": "removed",
            }
        ]
        monkeypatch.setattr(quickreply, "_save_pay", lambda: None)
        requests, replies = [], []

        banned_in = {
            get_peer_id(PeerChannel(1)): 42,   # banned, unban expected
            get_peer_id(PeerChannel(2)): 7,    # someone else is banned here
            get_peer_id(PeerChannel(3)): 42,   # guard.py's channel
        }
        scanned = []

        class Client:
            async def iter_dialogs(self):
                for index, name in ((1, "Alpha"), (2, "Beta"), (3, "Guarded")):
                    yield SimpleNamespace(
                        entity=PeerChannel(index),
                        name=name,
                        is_group=True,
                        is_channel=True,
                    )

            async def get_permissions(self, chat, user):
                if get_peer_id(chat) == get_peer_id(PeerChannel(1)):
                    # An id this account has no cached entity for.
                    raise ValueError("Could not find the input entity")
                return SimpleNamespace(is_banned=banned_in.get(
                    get_peer_id(chat)) == user)

            async def iter_participants(self, chat, filter=None):
                scanned.append(get_peer_id(chat))
                member = banned_in.get(get_peer_id(chat))
                if member is not None:
                    yield SimpleNamespace(id=member)

            async def __call__(self, request):
                requests.append(request)

        monkeypatch.setattr(quickreply, "client", Client())

        class Event:
            raw_text = ".unban 42"

            async def edit(self, text, **_kwargs):
                replies.append(text)

        await quickreply.cmd_unban(Event())

        # Beta bans somebody else, and the guarded channel is guard.py's job.
        assert len(requests) == 1
        assert isinstance(requests[0], EditBannedRequest)
        assert requests[0].banned_rights.view_messages is False
        assert "Unbanned 42 in 1 of 3 chat(s)." in replies[0]
        assert "Alpha" in replies[0]
        assert "Skipped, guard.py enforces it: Guarded" in replies[0]
        # Only the unresolvable id needed the (potentially huge) ban list.
        assert scanned == [get_peer_id(PeerChannel(1))]
        assert quickreply._pay["payments"][0]["join_status"] == "unbanned"
        assert linkstore.pending_unbans() == [
            {"user_id": 42, "ts": pytest.approx(
                linkstore.pending_unbans()[0]["ts"]
            )}
        ]

    asyncio.run(scenario())


def test_unban_journal_stops_the_bot_from_re_banning(
    tmp_path, monkeypatch
) -> None:
    async def scenario() -> None:
        from bot import app

        await db.close()
        monkeypatch.setattr(db.config, "DB_PATH", tmp_path / "unban.db")
        monkeypatch.setattr(linkstore, "UNBANS", tmp_path / "unbans.json")
        await db.init()
        unbanned, notices = [], []
        try:
            await db.upsert_group(
                -1001, "Alpha", "alpha", "supergroup", None, None, True
            )
            assert await db.register_order("ANIBAN", "14", "Amit", "all")
            link_id = await db.add_order_link(
                "ANIBAN", -1001, "https://t.me/+banned"
            )
            await db.set_order_link_joined(link_id, 42)
            await db.queue_member_ban(-1001, 42, "order removed")
            assert len(await db.pending_member_bans()) == 1

            async def unban(chat_id, user_id, **_kwargs):
                unbanned.append((chat_id, user_id))

            async def notify(text, **_kwargs):
                notices.append(text)

            monkeypatch.setattr(app.bot, "unban_chat_member", unban)
            monkeypatch.setattr(app, "tell_owner", notify)

            linkstore.queue_unban(42)
            await app._drain_unban_journal()

            assert unbanned == [(-1001, 42)]
            assert await db.pending_member_bans() == []
            assert (await db.order_links("ANIBAN"))[0]["buyer_banned"] == 1
            assert linkstore.pending_unbans() == []
            assert notices and "42" in notices[0]
        finally:
            await db.close()
            await app.bot.session.close()

    asyncio.run(scenario())



def test_approved_join_request_without_an_invite_link_is_not_banned(
    tmp_path, monkeypatch
) -> None:
    """A join request carries no invite link in a public group / plain approval.

    The enforcement gate used to require an invite link on both the stored
    request and the chat_member update, so the owner pressing Approve was
    followed by a permanent ban of the user they had just approved.
    """

    async def scenario() -> None:
        from bot import app

        await db.close()
        monkeypatch.setattr(db.config, "DB_PATH", tmp_path / "nolink.db")
        await db.init()
        banned = []
        notices = []
        try:
            await db.add_join_request(-1001, 71, None, "NoLink", None)
            await db.set_join_status(-1001, 71, "approved")

            async def ban(*args):
                banned.append(args)

            async def notify(text, **_kwargs):
                notices.append(text)

            monkeypatch.setattr(app.bot, "ban_chat_member", ban)
            monkeypatch.setattr(app, "tell_owner", notify)
            monkeypatch.setattr(app, "_bot_user_id", 999)

            await app.on_chat_member(
                SimpleNamespace(
                    old_chat_member=SimpleNamespace(
                        status=app.ChatMemberStatus.LEFT
                    ),
                    new_chat_member=SimpleNamespace(
                        status=app.ChatMemberStatus.MEMBER,
                        user=SimpleNamespace(
                            id=71, username=None, full_name="NoLink"
                        ),
                    ),
                    invite_link=None,
                    chat=SimpleNamespace(id=-1001, title="Public"),
                )
            )

            assert banned == []
            assert notices == []
            assert await db._all("SELECT * FROM member_bans") == []
        finally:
            await db.close()

    asyncio.run(scenario())


def test_revoked_order_join_is_removed_without_a_lasting_ban(
    tmp_path, monkeypatch
) -> None:
    """/revoke promises no lasting ban, including for a late joiner.

    A buyer racing a /revoke used to be permanently banned, and any journalled
    ban survived the kick+unban so the retry loop re-applied it.
    """

    async def scenario() -> None:
        from bot import app

        await db.close()
        monkeypatch.setattr(db.config, "DB_PATH", tmp_path / "revokejoin.db")
        await db.init()
        calls = []
        try:
            assert await db.register_order(
                "ANIREVJOIN", "14", "Amit", "lol", buyer_id=42
            )
            await db.add_order_link(
                "ANIREVJOIN", -1001, "https://t.me/+revoking"
            )
            await db.set_order_status("ANIREVJOIN", "revoke_pending")
            # An earlier enforcement pass had already journalled a ban.
            await db.queue_member_ban(-1001, 42, "earlier enforcement")

            async def ban(chat_id, user_id):
                calls.append(("ban", chat_id, user_id))

            async def unban(chat_id, user_id, **_kwargs):
                calls.append(("unban", chat_id, user_id))

            async def revoke(*_args):
                return True

            monkeypatch.setattr(app.bot, "ban_chat_member", ban)
            monkeypatch.setattr(app.bot, "unban_chat_member", unban)
            monkeypatch.setattr(app, "revoke_link", revoke)
            monkeypatch.setattr(app, "tell_owner", lambda *_a, **_k: _noop())
            monkeypatch.setattr(app, "_bot_user_id", 999)

            await app.on_chat_member(
                SimpleNamespace(
                    old_chat_member=SimpleNamespace(
                        status=app.ChatMemberStatus.LEFT
                    ),
                    new_chat_member=SimpleNamespace(
                        status=app.ChatMemberStatus.MEMBER,
                        user=SimpleNamespace(
                            id=42, username=None, full_name="Buyer"
                        ),
                    ),
                    invite_link=SimpleNamespace(
                        invite_link="https://t.me/+revoking"
                    ),
                    chat=SimpleNamespace(id=-1001, title="LOLsi"),
                )
            )

            # Kicked, then explicitly unbanned - never left banned.
            assert calls == [("ban", -1001, 42), ("unban", -1001, 42)]
            link = (await db.order_links("ANIREVJOIN"))[0]
            assert link["joined_user"] == 42
            assert link["buyer_removed"] == 1
            assert link["revoked"] == 1
            # A revoked order behaving as designed is not a compromise.
            assert (await db.get_order("ANIREVJOIN"))["status"] != "compromised"
            # The journalled ban must stop being enforced.
            rows = await db._all("SELECT * FROM member_bans")
            assert [row["status"] for row in rows] == ["cancelled"]
            assert await db.pending_member_bans() == []
        finally:
            await db.close()

    asyncio.run(scenario())


async def _noop() -> None:
    return None


def test_remove_user_releases_journalled_bans(tmp_path, monkeypatch) -> None:
    """/remove <user> removes without a lasting ban, so the journal must clear."""

    async def scenario() -> None:
        from bot import app

        await db.close()
        monkeypatch.setattr(db.config, "DB_PATH", tmp_path / "removeuser.db")
        await db.init()
        calls = []
        answers = []
        try:
            await db.upsert_group(
                -1001, "LOLsi", "Ls", "supergroup", None,
                "https://t.me/+general", is_admin=True,
            )
            await db.queue_member_ban(-1001, 84, "unauthorized join")

            async def ban(chat_id, user_id):
                calls.append(("ban", chat_id, user_id))

            async def unban(chat_id, user_id, **_kwargs):
                calls.append(("unban", chat_id, user_id))

            monkeypatch.setattr(app.bot, "ban_chat_member", ban)
            monkeypatch.setattr(app.bot, "unban_chat_member", unban)

            class Message:
                async def answer(self, text, **_kwargs):
                    answers.append(text)

            await app._remove_user(Message(), "84")

            assert calls == [("ban", -1001, 84), ("unban", -1001, 84)]
            assert "removed from 1 group(s)" in answers[0]
            assert await db.pending_member_bans() == []
        finally:
            await db.close()

    asyncio.run(scenario())


def test_short_code_survives_a_demotion(tmp_path, monkeypatch) -> None:
    """Demotion upserts an empty short code; a published code must not vanish."""

    async def scenario() -> None:
        await db.close()
        monkeypatch.setattr(db.config, "DB_PATH", tmp_path / "codes.db")
        await db.init()
        try:
            await db.upsert_group(
                -1001, "Lom And Som Op", "Lm", "supergroup", None,
                "https://t.me/+kept", is_admin=True,
            )
            # The bot is demoted to a plain member.
            await db.upsert_group(
                -1001, "Lom And Som Op", "", "supergroup", None, None,
                is_admin=False,
            )
            row = await db.get_group(-1001)
            assert row["short_code"] == "Lm"
            assert row["invite_link"] == "https://t.me/+kept"
            assert row["is_admin"] == 0
        finally:
            await db.close()

    asyncio.run(scenario())


def test_expiry_never_abandons_a_pending_cleanup(tmp_path, monkeypatch) -> None:
    """One seat expiring must not cancel the cleanup another seat is owed.

    pending_order_link_revocations only drives *_pending orders, so overwriting
    the status with 'partially_expired' silently stopped every retry.
    """

    async def scenario() -> None:
        await db.close()
        monkeypatch.setattr(db.config, "DB_PATH", tmp_path / "expiry.db")
        await db.init()
        try:
            assert await db.register_order("ANIEXP", "14", "Amit", "lol")
            await db.add_order_link("ANIEXP", -1001, "https://t.me/+open")
            await db.set_order_status("ANIEXP", "revoke_pending")

            await db.reconcile_order_expiry("ANIEXP")

            assert (await db.get_order("ANIEXP"))["status"] == "revoke_pending"
            pending = await db.pending_order_link_revocations()
            assert [row["order_id"] for row in pending] == ["ANIEXP"]

            # An order that is not being cleaned up still reports expiry.
            assert await db.register_order("ANIEXP2", "14", "Amit", "lol")
            await db.add_order_link("ANIEXP2", -1001, "https://t.me/+open2")
            await db.reconcile_order_expiry("ANIEXP2")
            assert (
                await db.get_order("ANIEXP2")
            )["status"] == "partially_expired"
        finally:
            await db.close()

    asyncio.run(scenario())


def test_stuck_transient_reservations_are_recovered(
    tmp_path, monkeypatch
) -> None:
    """A crash while 'expiring'/'cancelling' left a live invite link forever."""

    async def scenario() -> None:
        import time as _time

        await db.close()
        monkeypatch.setattr(db.config, "DB_PATH", tmp_path / "stuck.db")
        await db.init()
        try:
            assert await db.add_reservation(
                "req", "cp", -1001, 42, "https://t.me/+expiring", "ANISTUCK"
            )
            assert await db.add_reservation(
                "req", "lp", -1002, 43, "https://t.me/+cancelling", "ANISTUCK"
            )
            rows = await db.reservations_for_request("req")
            expiring, cancelling = rows[0], rows[1]
            assert await db.claim_reservation_status(
                expiring["id"], {"pending"}, "expiring"
            )
            assert await db.claim_reservation_status(
                cancelling["id"], {"pending"}, "cancelling"
            )

            # Fresh claims are still owned by a live worker.
            assert await db.pending_reservation_revocations(
                _time.time() - 300
            ) == []

            stale = await db.pending_reservation_revocations(_time.time() + 60)
            assert sorted(row["status"] for row in stale) == [
                "cancelling",
                "expiring",
            ]
        finally:
            await db.close()

    asyncio.run(scenario())


def test_order_prefix_wildcards_do_not_inflate_the_sequence(
    tmp_path, monkeypatch
) -> None:
    """`_` and `%` are LIKE wildcards and matched foreign order ids."""

    async def scenario() -> None:
        await db.close()
        monkeypatch.setattr(db.config, "DB_PATH", tmp_path / "prefix.db")
        await db.init()
        try:
            # A different prefix that only matches if `_` stays a wildcard.
            assert await db.register_order("AXI0007", "1", "x", "k")
            first = await db.create_next_order(
                "A_I", "14", "Amit", "lol", command_key="one"
            )
            second = await db.create_next_order(
                "A_I", "14", "Amit", "lol", command_key="two"
            )
            assert first == "A_I0001"
            assert second == "A_I0002"
        finally:
            await db.close()

    asyncio.run(scenario())


def test_scheme_less_invite_links_are_scrubbed_from_channel_output() -> None:
    """Telegram renders a bare `t.me/+hash` as a working invite."""
    from bot import app

    text, entities = quickreply._sanitize_channel_invites(
        "Buyer link: t.me/+secrethash and t.me/joinchat/OtherHash", []
    )
    assert "secrethash" not in text
    assert "OtherHash" not in text
    assert entities == []

    scrubbed = app._PRIVATE_INVITE_RE.sub(
        "[private link delivered in DM]", "join t.me/+botsidehash now"
    )
    assert "botsidehash" not in scrubbed

    # Entity-only (hidden) scheme-less invites are dropped too.
    _text, safe = quickreply._sanitize_channel_invites(
        "tap here",
        [SimpleNamespace(offset=0, length=3, url="t.me/+hiddenhash")],
    )
    assert safe == []


def test_oversized_caption_keeps_the_body_when_amount_appears_early() -> None:
    """Pulling `keep` back to any complete occurrence destroyed the caption."""
    order_id = "ANI7F3K9Q"
    amount = "\u20b9500"
    body = "x" * 1300
    text = f"Rahul paid {amount}\n{body}\nOrder: {order_id}"

    clipped, _entities, changed = quickreply._fit_media_caption(
        text, [], preserve=(order_id, amount)
    )

    assert changed
    assert quickreply._u16(clipped) <= 1024
    assert order_id in clipped
    assert amount in clipped
    # The receipt body must survive, not be cut down to the first few chars.
    assert clipped.count("x") > 900


def test_link_preview_is_not_treated_as_a_payment_image(monkeypatch) -> None:
    """A MessageMediaWebPage can never be uploaded with send_file()."""

    async def scenario() -> None:
        quickreply._state["self_id"] = 1
        quickreply._pay = quickreply._default_pay()
        monkeypatch.setattr(quickreply, "_save_pay", lambda: None)
        edits = []

        preview = quickreply.MessageMediaWebPage(webpage=SimpleNamespace())

        class Event:
            raw_text = "/add 10 Bob"
            chat_id = 42
            id = 77
            is_private = True

            async def get_reply_message(self):
                return SimpleNamespace(
                    media=preview, chat_id=42, id=5, sender_id=42
                )

            async def edit(self, text, **_kwargs):
                edits.append(text)

            async def respond(self, text, **_kwargs):
                edits.append(text)

        await quickreply.cmd_add(Event())

        assert quickreply._pay["payments"] == []
        assert "reply to an image" in edits[0]

    asyncio.run(scenario())


def test_unreadable_pay_file_is_never_overwritten(tmp_path, monkeypatch) -> None:
    """A read failure used to replace the payment log with an empty store."""
    # A directory exists() but cannot be read, standing in for an I/O failure.
    unreadable = tmp_path / "pay.json"
    unreadable.mkdir()
    warnings = []
    monkeypatch.setattr(quickreply.config, "PAY_FILE", unreadable)
    monkeypatch.setattr(quickreply.config, "PAYMENT_CHANNEL_RAW", "-1001")
    monkeypatch.setattr(quickreply.ui, "warn", lambda text: warnings.append(text))

    data = quickreply._load_pay()

    assert data["payments"] == []
    # The declared upload target is still recovered.
    assert data["post_channel"] == -1001
    # Nothing was written and no bogus recovery backup was produced.
    assert unreadable.is_dir()
    assert list(tmp_path.glob("pay.recovery-*.json")) == []
    assert warnings and "could not be read" in warnings[0]


def test_ping_reports_env_divergence_after_a_failed_write(monkeypatch) -> None:
    """save_env refreshes the live value first, so .env needs its own witness."""
    quickreply._pay = quickreply._default_pay()
    quickreply._pay["post_channel"] = -100222
    # What .env still holds after the write failed.
    quickreply._pay["post_channel_env"] = -100111
    # save_env already refreshed the in-process parameter to the new target.
    monkeypatch.setattr(quickreply.config, "PAYMENT_CHANNEL_RAW", "-100222")

    text = quickreply._runtime_status_text()

    assert "Payment channel: -100222" in text
    assert "PAYMENT_CHANNEL in .env: -100111" in text


def test_add_outside_a_private_chat_delivers_links_privately(
    monkeypatch,
) -> None:
    """Editing the /add message in a group would publish buyer-bound links."""

    async def scenario() -> None:
        quickreply._state["self_id"] = 1
        quickreply._pay = quickreply._default_pay()
        monkeypatch.setattr(quickreply, "_save_pay", lambda: None)
        edits = []
        sends = []

        async def reserve(*_args, **_kwargs):
            return {
                "request_id": "req",
                "entries": [
                    {
                        "link": "https://t.me/+paidlink",
                        "title": "Group",
                        "keyword": "cp",
                    }
                ],
                "failures": [],
                "metadata": {},
            }

        async def render(kind, *_args, **_kwargs):
            return (f"{kind}: https://t.me/+paidlink", [])

        class Client:
            async def send_message(self, peer, body, **_kwargs):
                sends.append((peer, body))
                return SimpleNamespace(id=len(sends))

        monkeypatch.setattr(quickreply, "_reserve_links", reserve)
        monkeypatch.setattr(quickreply, "_safe_payment_output", render)
        monkeypatch.setattr(quickreply, "client", Client())

        class Event:
            raw_text = "/add 10 Bob cp"
            chat_id = -1002
            id = 77
            is_private = False

            async def get_reply_message(self):
                return SimpleNamespace(
                    sender_id=42, media=None, chat_id=-1002, id=5
                )

            async def edit(self, text, **_kwargs):
                edits.append(text)

            async def respond(self, text, **_kwargs):
                edits.append(text)

        await quickreply.cmd_add(Event())

        # The buyer got the link in their DM...
        assert sends and sends[0][0] == 42
        assert "https://t.me/+paidlink" in sends[0][1]
        # ...and the group never saw it.
        assert edits
        assert "t.me/+paidlink" not in "".join(edits)
        assert quickreply._pay["payments"][0]["status"] == "valid"

    asyncio.run(scenario())


def test_channel_authored_reply_is_rejected_as_the_payer(monkeypatch) -> None:
    """A channel post's sender_id is the channel, which cannot own a link."""

    async def scenario() -> None:
        quickreply._state["self_id"] = 1
        quickreply._pay = quickreply._default_pay()
        monkeypatch.setattr(quickreply, "_save_pay", lambda: None)
        edits = []
        reserved = []

        async def reserve(*args, **kwargs):
            reserved.append((args, kwargs))
            return {"request_id": "", "entries": [], "failures": []}

        monkeypatch.setattr(quickreply, "_reserve_links", reserve)

        class Event:
            raw_text = "/add 10 Bob cp"
            chat_id = -1001234
            id = 77
            is_private = False

            async def get_reply_message(self):
                return SimpleNamespace(
                    sender_id=-1001234, media=None, chat_id=-1001234, id=5
                )

            async def edit(self, text, **_kwargs):
                edits.append(text)

            async def respond(self, text, **_kwargs):
                edits.append(text)

        await quickreply.cmd_add(Event())

        assert reserved == []
        assert quickreply._pay["payments"] == []
        assert "channel, not a user" in edits[0]

    asyncio.run(scenario())


def test_cancellation_does_not_escalate_a_revoked_sibling_order(
    tmp_path, monkeypatch
) -> None:
    """Permanence belongs to each order, not to the shared payment request."""

    async def scenario() -> None:
        from bot import app

        await db.close()
        monkeypatch.setattr(db.config, "DB_PATH", tmp_path / "sibling.db")
        await db.init()
        calls = []
        try:
            assert await db.register_order(
                "ANIGONE", "14", "Amit", "lol",
                request_id="payment", buyer_id=42,
            )
            assert await db.register_order(
                "ANIKEEP", "14", "Amit", "lol",
                request_id="payment", buyer_id=43,
            )
            for order_id, chat_id, user_id, link in (
                ("ANIGONE", -1001, 42, "https://t.me/+gone"),
                ("ANIKEEP", -1002, 43, "https://t.me/+keep"),
            ):
                assert await db.add_reservation(
                    "payment", order_id, chat_id, user_id, link, order_id
                )
                await db.add_order_link(order_id, chat_id, link)
                row = await db.reservation_for_join(chat_id, user_id, link)
                await db.set_reservation_status(row["id"], "completed")
            # Only this order was permanently removed.
            await db.set_order_status("ANIGONE", "remove_pending")

            async def ban(chat_id, user_id):
                calls.append(("ban", chat_id, user_id))

            async def unban(chat_id, user_id, **_kwargs):
                calls.append(("unban", chat_id, user_id))

            async def revoke(*_args):
                return True

            monkeypatch.setattr(app.bot, "ban_chat_member", ban)
            monkeypatch.setattr(app.bot, "unban_chat_member", unban)
            monkeypatch.setattr(app, "revoke_link", revoke)
            monkeypatch.setattr(app, "linkstore", None)

            await app._cancel_request_reservations("payment")

            # The removed order's buyer stays banned; the sibling is only kicked.
            assert ("ban", -1001, 42) in calls
            assert ("unban", -1001, 42) not in calls
            assert ("ban", -1002, 43) in calls
            assert ("unban", -1002, 43) in calls

            gone_link = (await db.order_links("ANIGONE"))[0]
            keep_link = (await db.order_links("ANIKEEP"))[0]
            assert gone_link["buyer_banned"] == 1
            assert keep_link["buyer_removed"] == 1
            assert keep_link["buyer_banned"] == 0
            assert (await db.get_order("ANIKEEP"))["status"] != "remove_pending"
        finally:
            await db.close()
            await app.bot.session.close()

    asyncio.run(scenario())



def test_rotation_replaces_every_raw_invite_link() -> None:
    """A post with two raw invite links kept the second, revoked one live."""
    text = "Main: https://t.me/+oldone backup: https://t.me/+oldtwo done"
    bold = SimpleNamespace(offset=quickreply._u16("Main: "), length=0)
    bold.length = quickreply._u16("https://t.me/+oldone")
    tail = SimpleNamespace(
        offset=quickreply._u16(
            "Main: https://t.me/+oldone backup: https://t.me/+oldtwo "
        ),
        length=quickreply._u16("done"),
    )

    result = quickreply._swap_link(text, [bold, tail], "https://t.me/+fresh")

    assert result is not None
    new_text, entities = result
    assert "oldone" not in new_text
    assert "oldtwo" not in new_text
    assert new_text.count("https://t.me/+fresh") == 2
    # The trailing entity still points at "done" after both replacements.
    assert (
        new_text.encode("utf-16-le")[
            entities[1].offset * 2:(entities[1].offset + entities[1].length) * 2
        ].decode("utf-16-le")
        == "done"
    )


def test_plain_text_template_is_not_reread_from_the_source_post(
    monkeypatch,
) -> None:
    """An edited source post must not silently rewrite a saved template."""

    async def scenario() -> None:
        quickreply._pay = quickreply._default_pay()
        quickreply._pay["done_template"] = "Saved: {amount}"
        quickreply._pay.pop("done_entities", None)
        quickreply._pay["done_ref"] = {"chat_id": 1, "message_id": 2}

        class Client:
            async def get_messages(self, _chat_id, ids=None):
                # The operator later edited the source post.
                return SimpleNamespace(message="Edited later", entities=[])

        monkeypatch.setattr(quickreply, "client", Client())
        monkeypatch.setattr(quickreply.ui, "warn", lambda *_a, **_k: None)

        text, entities = await quickreply._resolve_template("done")

        assert text == "Saved: {amount}"
        assert entities == []

    asyncio.run(scenario())


def test_unban_checks_the_ban_list_when_not_a_participant(monkeypatch) -> None:
    """A removed-and-banned user is not a participant but is still banned."""

    async def scenario() -> None:
        scanned = []

        class Client:
            async def get_permissions(self, _chat, _user_id):
                raise quickreply.UserNotParticipantError(request=None)

        async def banned_participant(chat, user_id):
            scanned.append((chat, user_id))
            return SimpleNamespace(id=user_id)

        monkeypatch.setattr(quickreply, "client", Client())
        monkeypatch.setattr(
            quickreply, "_banned_participant", banned_participant
        )

        is_banned, target = await quickreply._ban_state(-1001, 42)

        assert scanned == [(-1001, 42)]
        assert is_banned is True
        assert getattr(target, "id", None) == 42

    asyncio.run(scenario())


def test_remove_with_a_bare_id_does_not_delete_a_template(
    tmp_path, monkeypatch
) -> None:
    """Probing templates first meant /remove <id> could delete one."""

    async def scenario() -> None:
        from bot import app

        await db.close()
        monkeypatch.setattr(db.config, "DB_PATH", tmp_path / "removeorder.db")
        await db.init()
        answers = []
        try:
            # A template whose keyword happens to look like a user id.
            await db.upsert_template("12345", "14", "Amit", "body")
            await db.upsert_group(
                -1001, "LOLsi", "Ls", "supergroup", None,
                "https://t.me/+general", is_admin=True,
            )

            async def ban(*_args, **_kwargs):
                return None

            async def unban(*_args, **_kwargs):
                return None

            monkeypatch.setattr(app.bot, "ban_chat_member", ban)
            monkeypatch.setattr(app.bot, "unban_chat_member", unban)

            class Message:
                from_user = SimpleNamespace(id=app.config.OWNER_ID)
                chat = SimpleNamespace(type=app.ChatType.PRIVATE)

                async def answer(self, text, **_kwargs):
                    answers.append(text)

            await app.cmd_remove(
                Message(), SimpleNamespace(args="12345")
            )

            # The user was removed and the template survived.
            assert "removed from 1 group(s)" in answers[0]
            surviving = await db.get_template("12345")
            assert surviving is not None
            assert surviving["body"] == "body"
        finally:
            await db.close()

    asyncio.run(scenario())
