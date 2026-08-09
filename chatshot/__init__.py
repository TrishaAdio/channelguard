"""chatshot — render a Telegram conversation as a macOS-style screenshot.

Layers, in dependency order:

    model      plain dataclasses, no third-party imports
    entities   Telegram entities -> HTML (UTF-16 safe)
    group      sender grouping, date separators, bubble tails
    theme      colour tokens for light/dark
    html       model -> a complete standalone HTML document
    render     HTML -> PNG bytes via a pooled headless Chromium
    fetch      Telethon messages -> model  (imports Telethon)

`fetch` and `render` never import each other, so the renderer is testable from
fixtures with no Telegram account and no network.
"""

from .model import (  # noqa: F401
    ChatMeta,
    Conversation,
    Ent,
    Media,
    Msg,
    RenderOptions,
    ReplyRef,
    Sender,
)

__all__ = [
    "ChatMeta",
    "Conversation",
    "Ent",
    "Media",
    "Msg",
    "RenderOptions",
    "ReplyRef",
    "Sender",
]
