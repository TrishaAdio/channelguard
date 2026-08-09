"""`.setproofs` / `.proof` behaviour."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import quickreply
from chatshot import ChatMeta, Conversation, Msg, RenderOptions, Sender


class Event:
    """Minimal outgoing-command event."""

    def __init__(self, text: str, chat_id: int = -1002, msg_id: int = 500,
                 reply=None, is_channel: bool = False):
        self.raw_text = text
        self.chat_id = chat_id
        self.id = msg_id
        self.is_channel = is_channel
        self.is_private = False
        self._reply = reply
        self.edits: list[str] = []

    async def get_reply_message(self):
        return self._reply

    async def get_chat(self):
        return SimpleNamespace(title="Rahul Verma", id=42)

    async def edit(self, text, **_kwargs):
        self.edits.append(text)

    async def respond(self, text, **_kwargs):
        self.edits.append(text)


def _conversation() -> Conversation:
    from datetime import datetime

    return Conversation(
        messages=[
            Msg(id=1, sender=Sender(id=42, name="Rahul Verma"), ts=datetime(2026, 8, 9, 21, 0))
        ],
        meta=ChatMeta(title="Rahul Verma", is_private=True),
        options=RenderOptions(),
    )


def _install_render_stubs(monkeypatch) -> dict:
    """Replace the renderer/fetcher so tests need no browser or network."""
    seen: dict = {}

    class FakeFetcher:
        def __init__(self, client, tz=None):
            seen["client"] = client

        async def conversation(self, chat, messages, options=None):
            seen["messages"] = list(messages)
            seen["chat"] = chat
            seen["options"] = options
            return _conversation(), {}

    async def fake_render(conversation, emoji_src=None):
        seen["rendered"] = conversation
        return b"\x89PNG\r\n\x1a\nFAKE"

    import chatshot.fetch as fetch_mod
    import chatshot.render as render_mod

    monkeypatch.setattr(fetch_mod, "Fetcher", FakeFetcher)
    monkeypatch.setattr(render_mod, "render_png", fake_render)
    return seen


class FakeClient:
    def __init__(self, messages):
        self._messages = messages
        self.sent: list[tuple] = []

    async def get_messages(self, chat_id, **kwargs):
        self._last_kwargs = kwargs
        limit = kwargs.get("limit", len(self._messages))
        if kwargs.get("reverse"):
            min_id = kwargs.get("min_id", 0)
            return [m for m in self._messages if m.id > min_id][:limit]
        max_id = kwargs.get("max_id")
        pool = [m for m in self._messages if max_id is None or m.id <= max_id]
        return list(reversed(pool))[:limit]

    async def send_file(self, target, stream, caption=None, **_kwargs):
        self.sent.append((target, stream, caption))
        return SimpleNamespace(id=999)


def _msgs(*ids):
    return [SimpleNamespace(id=i, date=i, message=f"m{i}") for i in ids]


# --------------------------------------------------------------------------
# .setproofs
# --------------------------------------------------------------------------


def test_setproofs_persists_to_env_and_runtime(monkeypatch) -> None:
    async def scenario() -> None:
        quickreply._pay = quickreply._default_pay()
        saved: list[dict] = []
        monkeypatch.setattr(quickreply.config, "save_env", lambda v: saved.append(v))
        monkeypatch.setattr(quickreply, "_save_pay", lambda: None)

        event = Event(".setproofs", chat_id=-1009876543210, is_channel=True)
        await quickreply.cmd_setproofs(event)

        assert quickreply._pay["proof_channel"] == -1009876543210
        assert saved == [{"PROOF_CHANNEL": "-1009876543210"}]
        assert "Proof channel set here" in event.edits[0]

    asyncio.run(scenario())


def test_setproofs_outside_a_channel_asks_for_a_target(monkeypatch) -> None:
    async def scenario() -> None:
        quickreply._pay = quickreply._default_pay()
        monkeypatch.setattr(quickreply, "_save_pay", lambda: None)
        event = Event(".setproofs", is_channel=False)
        await quickreply.cmd_setproofs(event)
        assert "Run .setproofs inside the target channel" in event.edits[0]
        assert quickreply._pay.get("proof_channel") is None

    asyncio.run(scenario())


def test_setproofs_warns_when_env_write_fails(monkeypatch) -> None:
    async def scenario() -> None:
        quickreply._pay = quickreply._default_pay()
        warnings: list[str] = []

        def boom(_values):
            raise OSError("read-only fs")

        monkeypatch.setattr(quickreply.config, "save_env", boom)
        monkeypatch.setattr(quickreply, "_save_pay", lambda: None)
        monkeypatch.setattr(quickreply.ui, "warn", lambda t: warnings.append(t))

        event = Event(".setproofs", chat_id=-100777, is_channel=True)
        await quickreply.cmd_setproofs(event)

        # The new target still applies for this run...
        assert quickreply._pay["proof_channel"] == -100777
        # ...but the recorded .env witness keeps the old value so a restart
        # cannot silently adopt a stale parameter.
        assert quickreply._pay["proof_channel_env"] is None
        assert warnings and "PROOF_CHANNEL" in warnings[0]
        assert "will not survive a restart" in event.edits[0]

    asyncio.run(scenario())


def test_env_parameter_moves_the_proof_target_on_restart(monkeypatch) -> None:
    monkeypatch.setattr(quickreply.config, "PROOF_CHANNEL_RAW", "-100999")
    data = {"proof_channel": -100111, "proof_channel_env": -100111}
    changed, note = quickreply._sync_env_proof_channel(data)
    assert changed
    assert data["proof_channel"] == -100999
    assert "PROOF_CHANNEL" in note


def test_failed_env_write_is_not_overridden_on_restart(monkeypatch) -> None:
    """`.env` still holds the old value, and that must not win back."""
    monkeypatch.setattr(quickreply.config, "PROOF_CHANNEL_RAW", "-100111")
    data = {"proof_channel": -100777, "proof_channel_env": -100111}
    _changed, note = quickreply._sync_env_proof_channel(data)
    assert data["proof_channel"] == -100777
    assert note == ""


# --------------------------------------------------------------------------
# .proof
# --------------------------------------------------------------------------


def test_proof_requires_a_configured_channel(monkeypatch) -> None:
    async def scenario() -> None:
        quickreply._pay = quickreply._default_pay()
        quickreply._pay["proof_channel"] = None
        event = Event(".proof")
        await quickreply.cmd_proof(event)
        assert "No proof channel set" in event.edits[0]

    asyncio.run(scenario())


def test_proof_never_captures_its_own_command_message(monkeypatch) -> None:
    """The `.proof` message is the newest in the chat; it must be excluded."""

    async def scenario() -> None:
        quickreply._pay = quickreply._default_pay()
        quickreply._pay["proof_channel"] = -100555
        seen = _install_render_stubs(monkeypatch)
        client = FakeClient(_msgs(497, 498, 499, 500))
        monkeypatch.setattr(quickreply, "client", client)

        event = Event(".proof", msg_id=500)
        await quickreply.cmd_proof(event)

        captured = [m.id for m in seen["messages"]]
        assert 500 not in captured, "the .proof command leaked into its own capture"
        assert captured == [499]
        assert client.sent and client.sent[0][0] == -100555

    asyncio.run(scenario())


def test_proof_sends_a_named_png_stream(monkeypatch) -> None:
    async def scenario() -> None:
        quickreply._pay = quickreply._default_pay()
        quickreply._pay["proof_channel"] = -100555
        _install_render_stubs(monkeypatch)
        client = FakeClient(_msgs(1, 2))
        monkeypatch.setattr(quickreply, "client", client)

        await quickreply.cmd_proof(Event(".proof", msg_id=3))

        _target, stream, caption = client.sent[0]
        assert stream.name == "proof.png"
        assert stream.getvalue().startswith(b"\x89PNG")
        assert "Rahul Verma" in caption

    asyncio.run(scenario())


def test_proof_count_is_parsed_and_clamped(monkeypatch) -> None:
    async def scenario() -> None:
        quickreply._pay = quickreply._default_pay()
        quickreply._pay["proof_channel"] = -100555
        seen = _install_render_stubs(monkeypatch)
        client = FakeClient(_msgs(*range(1, 40)))
        monkeypatch.setattr(quickreply, "client", client)

        await quickreply.cmd_proof(Event(".proof 3", msg_id=100))
        assert len(seen["messages"]) == 3

        await quickreply.cmd_proof(Event(".proof 999", msg_id=100))
        assert len(seen["messages"]) == quickreply.PROOF_MAX_MESSAGES

    asyncio.run(scenario())


def test_proof_anchors_at_a_replied_message(monkeypatch) -> None:
    async def scenario() -> None:
        quickreply._pay = quickreply._default_pay()
        quickreply._pay["proof_channel"] = -100555
        seen = _install_render_stubs(monkeypatch)
        client = FakeClient(_msgs(10, 11, 12, 13))
        monkeypatch.setattr(quickreply, "client", client)

        reply = SimpleNamespace(id=11, date=11, message="m11")
        await quickreply.cmd_proof(Event(".proof 2", msg_id=99, reply=reply))

        assert [m.id for m in seen["messages"]] == [11, 12]

    asyncio.run(scenario())


def test_proof_reports_an_empty_chat(monkeypatch) -> None:
    async def scenario() -> None:
        quickreply._pay = quickreply._default_pay()
        quickreply._pay["proof_channel"] = -100555
        _install_render_stubs(monkeypatch)
        monkeypatch.setattr(quickreply, "client", FakeClient([]))
        event = Event(".proof", msg_id=1)
        await quickreply.cmd_proof(event)
        assert "Nothing to capture" in event.edits[-1]

    asyncio.run(scenario())


def test_proof_surfaces_a_render_failure_instead_of_posting(monkeypatch) -> None:
    async def scenario() -> None:
        quickreply._pay = quickreply._default_pay()
        quickreply._pay["proof_channel"] = -100555
        _install_render_stubs(monkeypatch)

        async def boom(_conversation, emoji_src=None):
            raise RuntimeError("chromium missing")

        import chatshot.render as render_mod

        monkeypatch.setattr(render_mod, "render_png", boom)
        monkeypatch.setattr(quickreply.ui, "warn", lambda _t: None)
        client = FakeClient(_msgs(1, 2))
        monkeypatch.setattr(quickreply, "client", client)

        event = Event(".proof", msg_id=3)
        await quickreply.cmd_proof(event)

        assert client.sent == []
        assert "Render failed" in event.edits[-1]

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# dispatch + status
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        (".proof", 1),
        ("/proof", 1),
        (".proof 5", 5),
        (".PROOF 12", 12),
    ],
)
def test_proof_command_forms_are_recognised(text, expected) -> None:
    match = quickreply._PROOF_RE.match(text)
    assert match is not None
    assert int(match.group(1) or 1) == expected


@pytest.mark.parametrize("text", [".proofs", ".proof abc", "proof", ".proofx"])
def test_non_proof_text_is_not_matched(text) -> None:
    assert quickreply._PROOF_RE.match(text) is None


def test_setproofs_and_proof_do_not_collide() -> None:
    assert quickreply._SETPROOFS_RE.match(".setproofs") is not None
    assert quickreply._PROOF_RE.match(".setproofs") is None


def test_ping_reports_the_proof_channel(monkeypatch) -> None:
    quickreply._pay = quickreply._default_pay()
    quickreply._pay["post_channel"] = -100222
    quickreply._pay["post_channel_env"] = -100222
    quickreply._pay["proof_channel"] = -100333
    quickreply._pay["proof_channel_env"] = -100333
    monkeypatch.setattr(quickreply.config, "PAYMENT_CHANNEL_RAW", "-100222")
    monkeypatch.setattr(quickreply.config, "PROOF_CHANNEL_RAW", "-100333")

    text = quickreply._runtime_status_text()

    assert "Proof channel: -100333" in text
    assert "PROOF_CHANNEL in .env" not in text


def test_ping_reports_a_proof_channel_divergence(monkeypatch) -> None:
    quickreply._pay = quickreply._default_pay()
    quickreply._pay["proof_channel"] = -100777
    quickreply._pay["proof_channel_env"] = -100111   # what .env still holds
    monkeypatch.setattr(quickreply.config, "PROOF_CHANNEL_RAW", "-100777")

    text = quickreply._runtime_status_text()

    assert "Proof channel: -100777" in text
    assert "PROOF_CHANNEL in .env: -100111" in text


def test_help_documents_the_proof_commands() -> None:
    assert ".setproofs" in quickreply.HELP_TEXT
    assert ".proof" in quickreply.HELP_TEXT
