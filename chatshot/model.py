"""Renderer-facing data model.

This layer is deliberately free of both Telethon and Playwright: `fetch.py`
produces these objects and `html.py` consumes them. Keeping the seam here means
the renderer can be developed and regression-tested from fixtures with no
Telegram account and no network.

Every offset in `Ent` is a **UTF-16 code unit** offset, exactly as Telegram
sends it. Converting to Python character indices is `entities.py`'s job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

# --------------------------------------------------------------------------
# entities
# --------------------------------------------------------------------------

#: Inline styles that only affect the run of text they cover. These can be
#: freely split at run boundaries because two adjacent identical tags render
#: the same as one.
INLINE_TYPES = frozenset(
    {
        "bold",
        "italic",
        "underline",
        "strike",
        "spoiler",
        "code",
        "url",
        "text_url",
        "mention",
        "mention_name",
        "hashtag",
        "cashtag",
        "bot_command",
        "email",
        "phone",
    }
)

#: Block-level entities. These become containers and must wrap whole segments
#: rather than being split, so they are resolved before inline styling.
BLOCK_TYPES = frozenset({"pre", "blockquote"})

#: Replaced wholesale by an <img>, so its covered text is dropped.
REPLACED_TYPES = frozenset({"custom_emoji"})


@dataclass
class Ent:
    """One Telegram message entity."""

    type: str
    offset: int          # UTF-16 code units
    length: int          # UTF-16 code units
    url: str = ""        # text_url
    language: str = ""   # pre
    document_id: int = 0  # custom_emoji
    user_id: int = 0     # mention_name
    collapsed: bool = False  # blockquote

    @property
    def end(self) -> int:
        return self.offset + self.length


# --------------------------------------------------------------------------
# people
# --------------------------------------------------------------------------


@dataclass
class Sender:
    """Who wrote a message.

    `avatar` is a data URI (or empty, in which case initials on a colour
    gradient are drawn instead). `color_index` selects Telegram's per-peer
    colour, 0-6.
    """

    id: int
    name: str
    avatar: str = ""
    color_index: int = 0
    is_self: bool = False
    verified: bool = False
    premium: bool = False

    @property
    def initials(self) -> str:
        parts = [p for p in (self.name or "").split() if p]
        if not parts:
            return "?"
        if len(parts) == 1:
            return parts[0][:1].upper()
        return (parts[0][:1] + parts[-1][:1]).upper()


# --------------------------------------------------------------------------
# attachments
# --------------------------------------------------------------------------


@dataclass
class ReplyRef:
    """The quoted-reply header shown above a message."""

    name: str
    text: str = ""
    color_index: int = 0
    thumb: str = ""       # data URI of a small media preview


@dataclass
class Media:
    """A single attachment.

    kind:
      photo     - `src` data URI, `w`/`h` intrinsic size
      sticker   - `src` data URI, rendered without a bubble
      voice     - `waveform` amplitudes 0-31, `duration` seconds
      video     - `src` poster data URI, `duration` seconds
      document  - `filename`, `size_label`
      webpage   - `site`, `title`, `description`, optional `src` thumb
    """

    kind: str
    src: str = ""
    w: int = 0
    h: int = 0
    duration: int = 0
    waveform: list[int] = field(default_factory=list)
    filename: str = ""
    size_label: str = ""
    site: str = ""
    title: str = ""
    description: str = ""
    spoiler: bool = False

    @property
    def is_bubbleless(self) -> bool:
        """Stickers float on the wallpaper with no bubble behind them."""
        return self.kind == "sticker"


# --------------------------------------------------------------------------
# messages
# --------------------------------------------------------------------------


@dataclass
class Msg:
    """One rendered message."""

    id: int
    sender: Sender
    ts: datetime
    text: str = ""
    entities: list[Ent] = field(default_factory=list)
    out: bool = False              # sent by the account itself
    reply: Optional[ReplyRef] = None
    forward_from: str = ""
    media: Optional[Media] = None
    edited: bool = False
    views: Optional[int] = None
    read: bool = False             # outgoing: double tick instead of single
    service: str = ""              # non-empty => centred service pill
    author: str = ""               # channel post signature
    time_label: str = ""           # filled in by html.py from RenderOptions

    # Filled in by group.py; not set by callers.
    first_in_group: bool = True
    last_in_group: bool = True
    show_avatar: bool = True
    show_name: bool = True

    @property
    def has_text(self) -> bool:
        return bool(self.text)


@dataclass
class ChatMeta:
    """Header/window information for the rendered chat."""

    title: str = ""
    subtitle: str = ""
    avatar: str = ""
    color_index: int = 5
    show_header: bool = True
    #: A one-to-one chat shows neither sender names nor avatars, matching the
    #: real client. Group/channel renders show both.
    is_private: bool = False


@dataclass
class RenderOptions:
    """Everything that changes how the image looks."""

    theme: str = "light"           # light | dark
    width: int = 720               # CSS px of the chat column
    scale: int = 3                 # device pixel ratio
    window_chrome: bool = True     # macOS window frame + shadow
    wallpaper: bool = True
    wallpaper_preset: str = "auto"   # auto | slate | telegram | plain
    clock24: bool = False
    font: str = "Inter"            # swap to "SF Pro Text" if licensed locally
    mono_font: str = "JetBrains Mono"
    show_ticks: bool = True
    hide_sender_names: bool = False  # privacy: render names as a dash
    hide_avatars: bool = False
    padding: int = 22
    #: Backdrop behind the window shadow. Kept opaque because Telegram
    #: re-encodes photos as JPEG and would flatten alpha to black.
    backdrop: str = "auto"


@dataclass
class Conversation:
    """The complete render input."""

    messages: list[Msg] = field(default_factory=list)
    meta: ChatMeta = field(default_factory=ChatMeta)
    options: RenderOptions = field(default_factory=RenderOptions)
