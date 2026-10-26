"""`.qrlast` / `.qrtop` / `.qrorder`: reorder the live quick replies."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from telethon.tl import types as T

import quickreply
from test_quickreply_extras import Event

# order as in the user's screenshot (top -> bottom)
LIVE = ["w", "category", "forgein", "qr3", "qr2", "fix", "payments", "qr", "all", "demo", "screen", "no"]


class FakeClient:
    def __init__(self, names, apply=True):
        self.names = list(names)
        self.reordered = None
        self.apply = apply   # whether Telegram "stores" the new order

    async def __call__(self, request):
        name = type(request).__name__
        if name == "GetQuickRepliesRequest":
            return T.messages.QuickReplies(
                quick_replies=[T.QuickReply(shortcut_id=100 + i, shortcut=n, top_message=0, count=1)
                               for i, n in enumerate(self.names)],
                messages=[], chats=[], users=[])
        if name == "ReorderQuickRepliesRequest":
            id_to_name = {100 + i: n for i, n in enumerate(self.names)}
            self.reordered = [id_to_name[i] for i in request.order]
            if self.apply:
                self.names = list(self.reordered)   # server stores it; re-read matches
            return True
        raise NotImplementedError(name)


def _plan(action, wanted, current=LIVE):
    return quickreply._plan_qr_order(current, action, wanted)


def test_plan_qrlast_moves_named_to_bottom_in_order():
    out = _plan(".qrlast", ["category", "forgein"])
    assert out[-2:] == ["category", "forgein"]
    assert out == ["w", "qr3", "qr2", "fix", "payments", "qr", "all", "demo", "screen", "no",
                   "category", "forgein"]


def test_plan_qrtop_moves_named_to_front():
    assert _plan(".qrtop", ["demo"])[0] == "demo"
    assert "demo" not in _plan(".qrtop", ["demo"])[1:]


def test_qrfirst_is_the_same_as_qrtop():
    assert _plan(".qrfirst", ["category"]) == _plan(".qrtop", ["category"])
    assert _plan(".qrfirst", ["category"])[0] == "category"   # first in the / popup


def test_qrfirst_command_puts_name_at_top():
    client, event = _run_cmd(".qrfirst category")
    assert client.reordered[0] == "category"
    assert "1. /category" in event.edits[-1] and "saved on Telegram" in event.edits[-1]


def test_plan_qrorder_sets_exact_leading_order_keeps_rest():
    out = _plan(".qrorder", ["payments", "all"])
    assert out[:2] == ["payments", "all"]
    assert out[2:] == [n for n in LIVE if n not in ("payments", "all")]


def test_plan_ignores_unknown_and_dedupes():
    out = _plan(".qrlast", ["category", "nope", "category"])
    assert out.count("category") == 1 and out[-1] == "category" and "nope" not in out


def _run_cmd(text, names=LIVE, apply=True):
    client = FakeClient(names, apply=apply)
    quickreply.client = client
    quickreply._state["self_id"] = 1
    event = Event(text, chat_id=1)
    asyncio.run(quickreply.cmd_qr_reorder(event, text.split()[0]))
    return client, event


def test_qrlast_command_reorders_live_list_and_reports():
    client, event = _run_cmd(".qrlast category forgein")
    assert client.reordered[-2:] == ["category", "forgein"]
    assert "saved on Telegram" in event.edits[-1]
    assert "12. /forgein" in event.edits[-1]                   # verified last


def test_reports_when_telegram_does_not_store_the_order():
    client, event = _run_cmd(".qrlast category forgein", apply=False)
    assert client.reordered is not None                        # we did send it
    assert "did not store" in event.edits[-1]
    assert client.names[:3] == ["w", "category", "forgein"]    # unchanged server order


def test_no_args_shows_current_order_without_reordering():
    client, event = _run_cmd(".qrlast")
    assert client.reordered is None
    assert "Current quick-reply order" in event.edits[-1] and "1. /w" in event.edits[-1]


def test_unknown_only_reports_unchanged():
    client, event = _run_cmd(".qrlast doesnotexist")
    assert client.reordered is None and "unchanged" in event.edits[-1].lower()


def test_slash_prefixes_are_accepted():
    client, _event = _run_cmd(".qrlast /category /forgein")
    assert client.reordered[-2:] == ["category", "forgein"]
    assert SimpleNamespace  # import kept for readability
