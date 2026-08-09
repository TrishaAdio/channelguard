"""Message grouping.

Getting this wrong is the fastest way to make a render look like a generic
mockup rather than a real client. Telegram collapses consecutive messages from
the same author: the name shows on the first bubble of a run, and the tail plus
the avatar appear on the *last* one, so the tail visually points at the avatar.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from .model import Msg

#: Telegram keeps consecutive messages in one group for this long.
GROUP_WINDOW_SECONDS = 600


def _same_group(a: Optional[Msg], b: Optional[Msg], window: int) -> bool:
    if a is None or b is None:
        return False
    if a.service or b.service:
        return False
    if a.out != b.out:
        return False
    if a.sender.id != b.sender.id:
        return False
    if a.ts.date() != b.ts.date():
        return False
    # A sticker floats without a bubble, so it never shares a run.
    if (a.media and a.media.is_bubbleless) or (b.media and b.media.is_bubbleless):
        return False
    if abs((b.ts - a.ts).total_seconds()) > window:
        return False
    return True


def apply_grouping(
    messages: list[Msg],
    *,
    window: int = GROUP_WINDOW_SECONDS,
    is_private: bool = False,
) -> list[Msg]:
    """Annotate each message with its position in its group.

    `is_private` suppresses names and avatars: a one-to-one chat needs neither,
    which is how the real client renders a DM.
    """
    for index, message in enumerate(messages):
        previous = messages[index - 1] if index else None
        following = messages[index + 1] if index + 1 < len(messages) else None

        message.first_in_group = not _same_group(previous, message, window)
        message.last_in_group = not _same_group(message, following, window)

        incoming_group_chat = not message.out and not is_private
        message.show_name = message.first_in_group and incoming_group_chat
        message.show_avatar = message.last_in_group and not message.out
    return messages


def date_label(when: date, today: Optional[date] = None) -> str:
    """`Today` / `Yesterday` / `9 August` / `9 August 2025`."""
    today = today or datetime.now().date()
    delta = (today - when).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Yesterday"
    if when.year == today.year:
        return f"{when.day} {when.strftime('%B')}"
    return f"{when.day} {when.strftime('%B')} {when.year}"


def with_date_separators(messages: list[Msg]) -> list[tuple[str, object]]:
    """Interleave `('date', label)` markers into a `('msg', Msg)` stream."""
    out: list[tuple[str, object]] = []
    last: Optional[date] = None
    for message in messages:
        day = message.ts.date()
        if day != last:
            out.append(("date", date_label(day)))
            last = day
        out.append(("msg", message))
    return out


def format_time(ts: datetime, clock24: bool = False) -> str:
    if clock24:
        return ts.strftime("%H:%M")
    # %-I is glibc-specific; strip the pad manually so this stays portable.
    return ts.strftime("%I:%M %p").lstrip("0")
