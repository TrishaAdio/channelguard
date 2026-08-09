"""Synthetic conversations for renderer development and regression tests.

Fixtures exist so the renderer can be built and tested without a Telegram
account. The bot path always renders real fetched messages.
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta

from .model import (
    ChatMeta,
    Conversation,
    Ent,
    Media,
    Msg,
    RenderOptions,
    ReplyRef,
    Sender,
)


_OFFER = "Yes \u2014 \u20b9500 for one seat. Send the payment and I'll add you."


def _png(width: int, height: int, pixel) -> str:
    """Build a tiny truecolour PNG data URI without any imaging dependency.

    A small bitmap stretched by CSS is smoothed by the browser, so an 8x6 source
    reads as a soft gradient rather than a flat block.
    """
    import struct
    import zlib

    raw = bytearray()
    for y in range(height):
        raw.append(0)                      # filter type: none
        for x in range(width):
            red, green, blue = pixel(x / max(1, width - 1), y / max(1, height - 1))
            raw += bytes((red & 255, green & 255, blue & 255))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(
            ">I", zlib.crc32(body) & 0xFFFFFFFF
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + base64.b64encode(png).decode()


def _png_pixel(color: tuple[int, int, int]) -> str:
    return _png(1, 1, lambda _x, _y: color)


def placeholder_image(width: int = 96, height: int = 64) -> str:
    """A neutral abstract gradient, standing in for a real photo.

    Sampled at a real resolution rather than a handful of pixels: a tiny source
    upscaled by the browser produces hard-edged blobs, not a smooth image.
    """
    import math

    def pixel(x: float, y: float) -> tuple[int, int, int]:
        wave = 0.5 + 0.5 * math.sin(3.0 * x + 1.7 * y)
        return (
            int(46 + 120 * x + 40 * wave),
            int(84 + 70 * y + 50 * wave),
            int(150 + 70 * (1 - x) + 25 * y),
        )

    return _png(width, height, pixel)


def ent(text: str, kind: str, needle: str, **kwargs) -> Ent:
    """Build an entity by locating `needle`, with correct UTF-16 offsets.

    Hand-computing offsets is error-prone precisely because they are UTF-16, so
    fixtures derive them instead. This is the pattern callers should copy.
    """
    from .entities import u16_len

    index = text.index(needle)
    return Ent(kind, u16_len(text[:index]), u16_len(needle), **kwargs)


def sample_conversation(theme: str = "light") -> Conversation:
    """Exercises grouping, entities, replies, media and both tick states."""
    base = datetime(2026, 8, 9, 21, 38)
    buyer = Sender(id=42, name="Rahul Verma", color_index=5)
    me = Sender(id=1, name="Store", color_index=3, is_self=True)

    messages = [
        Msg(
            id=1,
            sender=buyer,
            ts=base,
            text="hey, is the Indian pack still available?",
        ),
        Msg(
            id=2,
            sender=buyer,
            ts=base + timedelta(seconds=20),
            text="need 1 seat \U0001F64F",
        ),
        Msg(
            id=3,
            sender=me,
            ts=base + timedelta(minutes=1),
            out=True,
            read=True,
            text=_OFFER,
            entities=[ent(_OFFER, "bold", "\u20b9500")],
        ),
        Msg(
            id=4,
            sender=buyer,
            ts=base + timedelta(minutes=3),
            text="done \u2705",
            reply=ReplyRef(name="Store", text="Yes \u2014 \u20b9500 for one seat.", color_index=3),
        ),
        Msg(
            id=5,
            sender=buyer,
            ts=base + timedelta(minutes=3, seconds=10),
            media=Media(kind="photo", src=placeholder_image(), w=420, h=280),
            text="payment screenshot",
        ),
        Msg(
            id=6,
            sender=me,
            ts=base + timedelta(minutes=4),
            out=True,
            read=True,
            text=(
                "Received \U0001F389\nOrder ANI7F3K9Q\n"
                "Use this to join, it works once:"
            ),
            entities=[Ent("code", 18, 9)],
        ),
        Msg(
            id=7,
            sender=me,
            ts=base + timedelta(minutes=4, seconds=5),
            out=True,
            read=False,
            text="Tap to reveal your link",
            entities=[Ent("spoiler", 7, 16)],   # "reveal your link"
        ),
        Msg(
            id=8,
            sender=buyer,
            ts=base + timedelta(minutes=6),
            media=Media(kind="voice", duration=7, waveform=[
                4, 9, 16, 22, 28, 19, 12, 25, 31, 17, 8, 14, 21, 27, 11,
                6, 18, 24, 30, 13, 9, 20, 26, 15, 7, 23, 29, 10, 5, 19,
            ]),
        ),
        Msg(
            id=9,
            sender=buyer,
            ts=base + timedelta(minutes=7),
            text="thanks bhai, joined \U0001F525",
        ),
    ]

    return Conversation(
        messages=messages,
        meta=ChatMeta(
            title="Rahul Verma",
            subtitle="last seen recently",
            color_index=5,
            is_private=True,
        ),
        options=RenderOptions(theme=theme, width=720),
    )


def entity_showcase(theme: str = "light") -> Conversation:
    """Every supported entity type in one bubble, for visual verification."""
    base = datetime(2026, 8, 9, 14, 5)
    dev = Sender(id=7, name="Dev Notes", color_index=2)
    text = (
        "bold, italic, underline, strike, spoiler and code\n"
        "a link plus @mention and #tag\n"
        "quoted line here\n"
        "print('hello')"
    )
    ents = [
        ent(text, "bold", "bold"),
        ent(text, "italic", "italic"),
        ent(text, "underline", "underline"),
        ent(text, "strike", "strike"),
        ent(text, "spoiler", "spoiler"),
        ent(text, "code", "code"),
        ent(text, "text_url", "link", url="https://telegram.org"),
        ent(text, "mention", "@mention"),
        ent(text, "hashtag", "#tag"),
        ent(text, "blockquote", "quoted line here"),
        ent(text, "pre", "print('hello')", language="python"),
    ]
    return Conversation(
        messages=[Msg(id=1, sender=dev, ts=base, text=text, entities=ents)],
        meta=ChatMeta(title="Dev Notes", subtitle="channel", color_index=2),
        options=RenderOptions(theme=theme, width=620),
    )
