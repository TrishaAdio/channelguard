from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import guard
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

    first = linkstore.request_links(["cp"], 42, request_key="command")
    second = linkstore.request_links(["cp"], 42, request_key="command")
    assert first == second

    assert linkstore.put_result(
        first, [{"link": "https://t.me/+x", "title": "Group", "keyword": "cp"}]
    )
    assert linkstore.cancel_request(first, force=True)
    assert linkstore.is_request_cancelled(first)
    assert not linkstore.has_result(first)

    linkstore.queue_revoke(-1001, "https://t.me/+orphan")
    assert linkstore.pending_revokes() == [
        {"chat_id": -1001, "link": "https://t.me/+orphan", "ts": pytest.approx(
            linkstore.pending_revokes()[0]["ts"]
        )}
    ]
    linkstore.complete_revoke(-1001, "https://t.me/+orphan")
    assert linkstore.pending_revokes() == []


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
            assert await db.add_reservation(
                "payment", "cp", -1001, 42, "https://t.me/+paid"
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
            await app._cancel_request_reservations("payment")

            current = await db.get_reservation(reservation["id"])
            assert current["status"] == "cancelled"
            assert calls == [("ban", -1001, 42), ("unban", -1001, 42)]
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
        assert edits == ["done receipt"]
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
    template = f"Order: {alias}"
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

    assert text == "Order: ANIABC234"
    assert len(entities) == 1
    assert entities[0].offset == quickreply._u16("Order: ")
    assert entities[0].length == quickreply._u16("ANIABC234")
