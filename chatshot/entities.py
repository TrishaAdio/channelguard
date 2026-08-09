"""Telegram entities -> HTML.

Two properties make this the most bug-prone part of any Telegram renderer, and
both are handled explicitly here.

**Offsets are UTF-16 code units.** Telegram counts in UTF-16, Python strings
index by code point. Any character outside the BMP (every non-trivial emoji) is
two UTF-16 units but one Python character, so using raw offsets shifts every
entity after the first emoji. `to_char_offsets` converts once, up front.

**Entities overlap arbitrarily.** Bold and italic may partially overlap, a link
may cover half a bold run, and so on. Emitting tags by walking entities in
sequence produces malformed, mis-nested HTML. Instead each character gets the
*set* of styles covering it, adjacent characters with an identical set are
grouped into a run, and each run is wrapped independently. Splitting one long
`<b>` into two adjacent `<b>` runs renders identically, so correctness here
costs nothing and mis-nesting becomes structurally impossible.

Block-level entities (`pre`, `blockquote`) cannot be split that way, so they are
resolved first into containers and inline styling runs inside each one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .model import BLOCK_TYPES, INLINE_TYPES, REPLACED_TYPES, Ent

# --------------------------------------------------------------------------
# UTF-16 offset conversion
# --------------------------------------------------------------------------


def u16_len(text: str) -> int:
    """Length of `text` in UTF-16 code units, the unit Telegram counts in."""
    return len(text.encode("utf-16-le")) // 2


def _u16_to_char_table(text: str) -> list[int]:
    """Map each UTF-16 unit index to its Python character index.

    A surrogate pair contributes two entries pointing at the same character, so
    an offset that lands mid-pair (which Telegram should never send) clamps to
    the start of that character rather than corrupting the output.
    """
    table: list[int] = []
    for index, char in enumerate(text):
        table.append(index)
        if ord(char) > 0xFFFF:
            table.append(index)
    table.append(len(text))          # sentinel so an end offset resolves
    return table


def to_char_offsets(text: str, entities: list[Ent]) -> list[Ent]:
    """Return copies of `entities` with offsets/lengths in characters.

    Entities that fall outside the text, or collapse to nothing, are dropped.
    """
    table = _u16_to_char_table(text)
    limit = len(table) - 1
    converted: list[Ent] = []
    for ent in entities or []:
        start_u16 = max(0, min(int(ent.offset), limit))
        end_u16 = max(start_u16, min(int(ent.offset) + int(ent.length), limit))
        start = table[start_u16]
        end = table[end_u16]
        if end <= start and ent.type not in REPLACED_TYPES:
            continue
        converted.append(
            Ent(
                type=ent.type,
                offset=start,
                length=max(0, end - start),
                url=ent.url,
                language=ent.language,
                document_id=ent.document_id,
                user_id=ent.user_id,
                collapsed=ent.collapsed,
            )
        )
    return converted


# --------------------------------------------------------------------------
# escaping
# --------------------------------------------------------------------------


def esc(text: str) -> str:
    """Escape text content. Newlines survive; CSS uses `white-space: pre-wrap`."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def esc_attr(value: str) -> str:
    return esc(value).replace('"', "&quot;").replace("'", "&#39;")


# --------------------------------------------------------------------------
# tag emission
# --------------------------------------------------------------------------

#: Deterministic nesting order for styles that start at the same offset. Lower
#: sorts further out. Links wrap styling; `code` hugs the text.
_PRIORITY = {
    "text_url": 5,
    "url": 5,
    "mention": 5,
    "mention_name": 5,
    "hashtag": 5,
    "cashtag": 5,
    "bot_command": 5,
    "email": 5,
    "phone": 5,
    "spoiler": 10,
    "bold": 20,
    "italic": 21,
    "underline": 22,
    "strike": 23,
    "code": 40,
}

_LINKISH = frozenset(
    {
        "url",
        "mention",
        "mention_name",
        "hashtag",
        "cashtag",
        "bot_command",
        "email",
        "phone",
    }
)


def _tags(ent: Ent) -> tuple[str, str]:
    kind = ent.type
    if kind == "bold":
        return "<b>", "</b>"
    if kind == "italic":
        return "<i>", "</i>"
    if kind == "underline":
        return "<u>", "</u>"
    if kind == "strike":
        return "<s>", "</s>"
    if kind == "code":
        return '<code class="mono">', "</code>"
    if kind == "spoiler":
        return '<span class="sp">', "</span>"
    if kind == "text_url":
        return f'<a class="lnk" href="{esc_attr(ent.url)}">', "</a>"
    if kind in _LINKISH:
        return '<a class="lnk">', "</a>"
    return "", ""


def _wrap(inner: str, active: tuple[int, ...], order: list[Ent]) -> str:
    if not active or not inner:
        return inner
    opens: list[str] = []
    closes: list[str] = []
    for index in active:
        open_tag, close_tag = _tags(order[index])
        if not open_tag:
            continue
        opens.append(open_tag)
        closes.append(close_tag)
    return "".join(opens) + inner + "".join(reversed(closes))


# --------------------------------------------------------------------------
# segmentation
# --------------------------------------------------------------------------


@dataclass
class _Seg:
    start: int
    end: int
    ent: Optional[Ent]


def _segments(length: int, blocks: list[Ent]) -> list[_Seg]:
    """Split [0, length) into block containers and the plain gaps between."""
    ordered = sorted(blocks, key=lambda e: (e.offset, -e.length))
    out: list[_Seg] = []
    pos = 0
    for block in ordered:
        start, end = max(pos, block.offset), block.end
        if end <= pos:
            continue                      # fully swallowed by an earlier block
        if start > pos:
            out.append(_Seg(pos, start, None))
        out.append(_Seg(start, end, block))
        pos = end
    if pos < length:
        out.append(_Seg(pos, length, None))
    return out or [_Seg(0, length, None)]


# --------------------------------------------------------------------------
# public entry point
# --------------------------------------------------------------------------

EmojiResolver = Callable[[int], str]


def to_html(
    text: str,
    entities: Optional[list[Ent]] = None,
    *,
    emoji_src: Optional[EmojiResolver] = None,
) -> str:
    """Render `text` + `entities` to inline HTML.

    `emoji_src` maps a custom-emoji document id to an image URI. When it returns
    empty the original placeholder glyph is kept, so a premium emoji degrades to
    its fallback character instead of vanishing.
    """
    if not text:
        return ""

    converted = to_char_offsets(text, entities or [])
    blocks = [e for e in converted if e.type in BLOCK_TYPES]
    inline = [e for e in converted if e.type in INLINE_TYPES]
    replaced = [e for e in converted if e.type in REPLACED_TYPES]

    segments = _segments(len(text), blocks)
    parts: list[str] = []
    for index, seg in enumerate(segments):
        start, end = seg.start, seg.end
        if seg.ent is None:
            # A block container already introduces its own vertical break, so the
            # newline that separated it in the source text would render as an
            # extra empty line. Telegram does not show that gap.
            if index and segments[index - 1].ent is not None:
                while start < end and text[start] == "\n":
                    start += 1
            if index + 1 < len(segments) and segments[index + 1].ent is not None:
                while end > start and text[end - 1] == "\n":
                    end -= 1
            if end <= start:
                continue
        inner = _inline_html(text, start, end, inline, replaced, emoji_src)
        if seg.ent is None:
            parts.append(inner)
        elif seg.ent.type == "pre":
            language = esc(seg.ent.language or "")
            label = f'<div class="pre-lang">{language}</div>' if language else ""
            parts.append(f'<div class="pre">{label}<pre class="mono">{inner}</pre></div>')
        else:
            collapsed = " collapsed" if seg.ent.collapsed else ""
            parts.append(f'<blockquote class="bq{collapsed}">{inner}</blockquote>')
    return "".join(parts)


def _inline_html(
    text: str,
    start: int,
    end: int,
    inline: list[Ent],
    replaced: list[Ent],
    emoji_src: Optional[EmojiResolver],
) -> str:
    if end <= start:
        return ""

    order = sorted(
        inline,
        key=lambda e: (e.offset, -e.length, _PRIORITY.get(e.type, 50), e.type),
    )
    # Per-character style sets. Quadratic in (chars x entities), which is
    # irrelevant at message scale and keeps the logic obviously correct.
    active_at: list[tuple[int, ...]] = []
    for position in range(start, end):
        active_at.append(
            tuple(
                index
                for index, ent in enumerate(order)
                if ent.offset <= position < ent.end
            )
        )

    replaced_at = {e.offset: e for e in replaced}

    out: list[str] = []
    buffer: list[str] = []
    current: Optional[tuple[int, ...]] = None
    position = start
    while position < end:
        active = active_at[position - start]
        ent = replaced_at.get(position)
        if ent is not None:
            span = max(1, ent.length)
            source = emoji_src(ent.document_id) if emoji_src else ""
            if source:
                atom = f'<img class="ce" src="{esc_attr(source)}" alt="">'
            else:
                atom = esc(text[position:min(end, position + span)])
        else:
            span = 1
            atom = esc(text[position])

        if active != current:
            if buffer:
                out.append(_wrap("".join(buffer), current or (), order))
            buffer = []
            current = active
        buffer.append(atom)
        position += span

    if buffer:
        out.append(_wrap("".join(buffer), current or (), order))
    return "".join(out)


def plain_preview(text: str, limit: int = 80) -> str:
    """One-line snippet for reply headers."""
    flat = " ".join((text or "").split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "\u2026"
