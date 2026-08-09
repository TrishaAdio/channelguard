"""Telethon messages -> the renderer model.

This is the only module that imports Telethon. Everything it produces is plain
data, so the renderer never touches the network: avatars, photos and custom
emoji are downloaded here and inlined as data URIs before rendering starts.

Downloads are cached per Fetcher instance, so re-rendering the same chat does
not refetch avatars.
"""

from __future__ import annotations

import base64
import logging
from datetime import timezone
from typing import Any, Optional

from telethon.tl import types as tl

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

log = logging.getLogger("chatshot.fetch")

#: Telethon entity class name -> our entity type.
_ENTITY_MAP = {
    "MessageEntityBold": "bold",
    "MessageEntityItalic": "italic",
    "MessageEntityUnderline": "underline",
    "MessageEntityStrike": "strike",
    "MessageEntitySpoiler": "spoiler",
    "MessageEntityCode": "code",
    "MessageEntityPre": "pre",
    "MessageEntityBlockquote": "blockquote",
    "MessageEntityTextUrl": "text_url",
    "MessageEntityUrl": "url",
    "MessageEntityMention": "mention",
    "MessageEntityMentionName": "mention_name",
    "InputMessageEntityMentionName": "mention_name",
    "MessageEntityHashtag": "hashtag",
    "MessageEntityCashtag": "cashtag",
    "MessageEntityBotCommand": "bot_command",
    "MessageEntityEmail": "email",
    "MessageEntityPhone": "phone",
    "MessageEntityCustomEmoji": "custom_emoji",
}

#: Cap on an inlined image, before base64. Chromium copes with more, but the
#: rendered PNG grows with it and Telegram re-encodes anyway.
_MAX_INLINE_BYTES = 4 * 1024 * 1024

#: Thumbnail sizes to try, largest first, when inlining an image.
_THUMB_FALLBACKS = (-1, 2, 1, 0)


def _data_uri(payload: bytes, mime: str = "image/jpeg") -> str:
    if not payload:
        return ""
    return f"data:{mime};base64," + base64.b64encode(payload).decode("ascii")


def _sniff_mime(payload: bytes) -> str:
    if payload[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if payload[:3] == b"GIF":
        return "image/gif"
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def decode_waveform(data: Optional[bytes]) -> list[int]:
    """Unpack Telegram's 5-bit-per-sample voice waveform."""
    if not data:
        return []
    out: list[int] = []
    total_bits = len(data) * 8
    bit = 0
    while bit + 5 <= total_bits:
        index, shift = divmod(bit, 8)
        value = data[index] >> shift
        if shift > 3 and index + 1 < len(data):
            value |= data[index + 1] << (8 - shift)
        out.append(value & 0x1F)
        bit += 5
    return out


def _size_label(size: Optional[int]) -> str:
    total = float(size or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if total < 1024 or unit == "GB":
            return f"{total:.0f} {unit}" if unit == "B" else f"{total:.1f} {unit}"
        total /= 1024
    return ""


def color_index_for(peer: Any) -> int:
    """Telegram's per-peer colour index.

    Newer layers carry an explicit `PeerColor`; otherwise fall back to the
    id-derived index the clients have always used.
    """
    color = getattr(peer, "color", None)
    explicit = getattr(color, "color", None)
    if isinstance(explicit, int):
        return explicit % 7
    peer_id = getattr(peer, "id", 0) or 0
    return abs(int(peer_id)) % 7


def display_name(peer: Any) -> str:
    if peer is None:
        return "Unknown"
    title = getattr(peer, "title", None)
    if title:
        return str(title)
    first = (getattr(peer, "first_name", "") or "").strip()
    last = (getattr(peer, "last_name", "") or "").strip()
    full = f"{first} {last}".strip()
    if full:
        return full
    username = getattr(peer, "username", None)
    if username:
        return f"@{username}"
    return str(getattr(peer, "id", "Unknown"))


class Fetcher:
    """Builds `Conversation` objects from Telethon messages."""

    def __init__(self, client, *, tz=None) -> None:
        self.client = client
        self.tz = tz
        self._avatars: dict[int, str] = {}
        self._emoji: dict[int, str] = {}

    # -- helpers -----------------------------------------------------------
    async def _avatar(self, peer: Any) -> str:
        peer_id = int(getattr(peer, "id", 0) or 0)
        if not peer_id:
            return ""
        if peer_id in self._avatars:
            return self._avatars[peer_id]
        uri = ""
        try:
            blob = await self.client.download_profile_photo(peer, file=bytes)
            if blob:
                uri = _data_uri(blob, _sniff_mime(blob))
        except Exception as error:  # noqa: BLE001
            log.debug("avatar download failed for %s: %s", peer_id, error)
        self._avatars[peer_id] = uri
        return uri

    async def _download(self, message, *, thumb=None) -> bytes:
        try:
            blob = await self.client.download_media(message, file=bytes, thumb=thumb)
        except Exception as error:  # noqa: BLE001
            log.debug("media download failed (thumb=%s): %s", thumb, error)
            return b""
        if blob and len(blob) > _MAX_INLINE_BYTES:
            log.debug("media too large to inline (%d bytes)", len(blob))
            return b""
        return blob or b""

    async def _download_image(self, message) -> bytes:
        """Largest image that fits the inline budget.

        A high-resolution photo can exceed the budget outright; returning nothing
        would render a placeholder, so progressively smaller cached sizes are
        tried before giving up.
        """
        for thumb in _THUMB_FALLBACKS:
            blob = await self._download(message, thumb=thumb)
            if blob:
                return blob
        return b""

    def _entities(self, message) -> list[Ent]:
        out: list[Ent] = []
        for raw in getattr(message, "entities", None) or []:
            kind = _ENTITY_MAP.get(type(raw).__name__)
            if not kind:
                continue
            out.append(
                Ent(
                    type=kind,
                    offset=int(getattr(raw, "offset", 0)),
                    length=int(getattr(raw, "length", 0)),
                    url=str(getattr(raw, "url", "") or ""),
                    language=str(getattr(raw, "language", "") or ""),
                    document_id=int(getattr(raw, "document_id", 0) or 0),
                    user_id=int(getattr(raw, "user_id", 0) or 0),
                    collapsed=bool(getattr(raw, "collapsed", False)),
                )
            )
        return out

    async def _media(self, message) -> Optional[Media]:
        media = getattr(message, "media", None)
        if media is None or isinstance(media, tl.MessageMediaWebPage):
            if isinstance(media, tl.MessageMediaWebPage):
                return self._webpage(media)
            return None

        spoiler = bool(getattr(media, "spoiler", False))

        if isinstance(media, tl.MessageMediaPhoto) or getattr(message, "photo", None):
            blob = await self._download_image(message)
            width, height = self._photo_size(message)
            return Media(
                kind="photo",
                src=_data_uri(blob, _sniff_mime(blob)) if blob else "",
                w=width,
                h=height,
                spoiler=spoiler,
            )

        document = getattr(message, "document", None)
        if document is None:
            return None

        attributes = list(getattr(document, "attributes", None) or [])
        audio = next(
            (a for a in attributes if isinstance(a, tl.DocumentAttributeAudio)), None
        )
        video = next(
            (a for a in attributes if isinstance(a, tl.DocumentAttributeVideo)), None
        )
        sticker = next(
            (a for a in attributes if isinstance(a, tl.DocumentAttributeSticker)), None
        )
        filename_attr = next(
            (a for a in attributes if isinstance(a, tl.DocumentAttributeFilename)),
            None,
        )

        if audio is not None and getattr(audio, "voice", False):
            return Media(
                kind="voice",
                duration=int(getattr(audio, "duration", 0) or 0),
                waveform=decode_waveform(getattr(audio, "waveform", None)),
            )

        if sticker is not None:
            # Animated .tgs/.webm cannot go in an <img>; the cached thumbnail can.
            blob = await self._download_image(message)
            return Media(
                kind="sticker", src=_data_uri(blob, _sniff_mime(blob)) if blob else ""
            )

        if video is not None:
            blob = await self._download_image(message)
            return Media(
                kind="video",
                src=_data_uri(blob, _sniff_mime(blob)) if blob else "",
                w=int(getattr(video, "w", 0) or 0),
                h=int(getattr(video, "h", 0) or 0),
                duration=int(getattr(video, "duration", 0) or 0),
                spoiler=spoiler,
            )

        return Media(
            kind="document",
            filename=str(getattr(filename_attr, "file_name", "") or "file"),
            size_label=_size_label(getattr(document, "size", 0)),
        )

    @staticmethod
    def _photo_size(message) -> tuple[int, int]:
        photo = getattr(message, "photo", None)
        best = (0, 0)
        for size in getattr(photo, "sizes", None) or []:
            width = int(getattr(size, "w", 0) or 0)
            height = int(getattr(size, "h", 0) or 0)
            if width * height > best[0] * best[1]:
                best = (width, height)
        return best if best[0] else (1280, 960)

    @staticmethod
    def _webpage(media: tl.MessageMediaWebPage) -> Optional[Media]:
        page = getattr(media, "webpage", None)
        if not isinstance(page, tl.WebPage):
            return None
        return Media(
            kind="webpage",
            site=str(getattr(page, "site_name", "") or ""),
            title=str(getattr(page, "title", "") or ""),
            description=str(getattr(page, "description", "") or ""),
        )

    async def _reply(self, message) -> Optional[ReplyRef]:
        try:
            replied = await message.get_reply_message()
        except Exception:  # noqa: BLE001
            replied = None
        if replied is None:
            return None
        try:
            author = await replied.get_sender()
        except Exception:  # noqa: BLE001
            author = None
        from .entities import plain_preview

        preview = plain_preview(getattr(replied, "message", "") or "")
        if not preview:
            preview = _media_word(replied)
        return ReplyRef(
            name=display_name(author),
            text=preview,
            color_index=color_index_for(author),
        )

    async def _custom_emoji(self, messages) -> dict[int, str]:
        ids: list[int] = []
        for message in messages:
            for raw in getattr(message, "entities", None) or []:
                doc_id = int(getattr(raw, "document_id", 0) or 0)
                if doc_id and doc_id not in self._emoji:
                    ids.append(doc_id)
        if not ids:
            return self._emoji
        try:
            from telethon.tl.functions.messages import GetCustomEmojiDocumentsRequest

            documents = await self.client(
                GetCustomEmojiDocumentsRequest(document_id=list(dict.fromkeys(ids)))
            )
        except Exception as error:  # noqa: BLE001
            log.debug("custom emoji fetch failed: %s", error)
            for doc_id in ids:
                self._emoji[doc_id] = ""
            return self._emoji

        for document in documents or []:
            doc_id = int(getattr(document, "id", 0) or 0)
            uri = ""
            try:
                blob = await self.client.download_media(document, file=bytes, thumb=-1)
                if blob:
                    uri = _data_uri(blob, _sniff_mime(blob))
            except Exception as error:  # noqa: BLE001
                log.debug("custom emoji %s download failed: %s", doc_id, error)
            self._emoji[doc_id] = uri
        return self._emoji

    async def _read_ceiling(self, chat) -> int:
        """Highest outgoing message id the peer has read, for tick state."""
        try:
            from telethon.tl.functions.messages import GetPeerDialogsRequest

            result = await self.client(GetPeerDialogsRequest(peers=[chat]))
            for dialog in getattr(result, "dialogs", None) or []:
                return int(getattr(dialog, "read_outbox_max_id", 0) or 0)
        except Exception as error:  # noqa: BLE001
            log.debug("read state unavailable: %s", error)
        return 0

    # -- public ------------------------------------------------------------
    async def conversation(
        self,
        chat,
        messages: list,
        *,
        options: Optional[RenderOptions] = None,
    ) -> tuple[Conversation, dict[int, str]]:
        """Build a Conversation plus the custom-emoji lookup for the renderer."""
        # `or 0` here would compare int against datetime and raise as soon as
        # one message in the batch had no date, so undated ones sort by id
        # behind the dated ones instead.
        def sort_key(message):
            when = getattr(message, "date", None)
            if when is None:
                return (1, 0.0, int(getattr(message, "id", 0) or 0))
            return (0, when.timestamp(), int(getattr(message, "id", 0) or 0))

        ordered = sorted([m for m in messages if m is not None], key=sort_key)
        emoji = await self._custom_emoji(ordered)
        read_ceiling = await self._read_ceiling(chat)

        try:
            me = await self.client.get_me()
            self_id = int(getattr(me, "id", 0) or 0)
        except Exception:  # noqa: BLE001
            self_id = 0

        built: list[Msg] = []
        for message in ordered:
            try:
                author = await message.get_sender()
            except Exception:  # noqa: BLE001
                author = None

            out = bool(getattr(message, "out", False))
            sender = Sender(
                id=int(getattr(author, "id", 0) or (self_id if out else 0)),
                name=display_name(author),
                avatar=await self._avatar(author) if author is not None else "",
                color_index=color_index_for(author),
                is_self=out,
                premium=bool(getattr(author, "premium", False)),
                verified=bool(getattr(author, "verified", False)),
            )

            when = getattr(message, "date", None)
            if when is not None and self.tz is not None:
                when = when.astimezone(self.tz)
            elif when is not None and when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)

            action = getattr(message, "action", None)
            if action is not None:
                built.append(
                    Msg(
                        id=int(message.id),
                        sender=sender,
                        ts=when,
                        service=_service_text(action, sender.name),
                    )
                )
                continue

            forward = getattr(message, "forward", None)
            forward_name = ""
            if forward is not None:
                try:
                    origin = await forward.get_sender()
                    forward_name = display_name(origin) or str(
                        getattr(forward, "from_name", "") or ""
                    )
                except Exception:  # noqa: BLE001
                    forward_name = str(getattr(forward, "from_name", "") or "")

            built.append(
                Msg(
                    id=int(message.id),
                    sender=sender,
                    ts=when,
                    text=str(getattr(message, "message", "") or ""),
                    entities=self._entities(message),
                    out=out,
                    reply=await self._reply(message),
                    forward_from=forward_name,
                    media=await self._media(message),
                    edited=bool(getattr(message, "edit_date", None)),
                    read=bool(read_ceiling and int(message.id) <= read_ceiling),
                )
            )

        meta = await self.chat_meta(chat)
        conversation = Conversation(
            messages=built,
            meta=meta,
            options=options or RenderOptions(),
        )
        return conversation, emoji

    async def chat_meta(self, chat) -> ChatMeta:
        is_private = isinstance(chat, tl.User)
        subtitle = ""
        if is_private:
            online = isinstance(getattr(chat, "status", None), tl.UserStatusOnline)
            subtitle = "online" if online else "last seen recently"
        else:
            count = getattr(chat, "participants_count", None)
            if isinstance(count, int) and count:
                label = "subscribers" if getattr(chat, "broadcast", False) else "members"
                subtitle = f"{count:,} {label}"
            elif getattr(chat, "broadcast", False):
                subtitle = "channel"
            else:
                subtitle = "group"
        return ChatMeta(
            title=display_name(chat),
            subtitle=subtitle,
            avatar=await self._avatar(chat),
            color_index=color_index_for(chat),
            is_private=is_private,
        )


def _media_word(message) -> str:
    if getattr(message, "photo", None):
        return "Photo"
    if getattr(message, "voice", None):
        return "Voice message"
    if getattr(message, "video", None):
        return "Video"
    if getattr(message, "sticker", None):
        return "Sticker"
    if getattr(message, "document", None):
        return "Document"
    return ""


def _service_text(action: Any, who: str) -> str:
    name = type(action).__name__
    table = {
        "MessageActionChatJoinedByLink": f"{who} joined via invite link",
        "MessageActionChatJoinedByRequest": f"{who} joined the group",
        "MessageActionChatAddUser": f"{who} was added",
        "MessageActionChatDeleteUser": f"{who} left",
        "MessageActionPinMessage": f"{who} pinned a message",
        "MessageActionChatEditTitle": "Group name changed",
        "MessageActionChatEditPhoto": "Group photo changed",
        "MessageActionContactSignUp": f"{who} joined Telegram",
        "MessageActionScreenshotTaken": f"{who} took a screenshot",
        "MessageActionHistoryClear": "History was cleared",
    }
    return table.get(name, name.replace("MessageAction", ""))
