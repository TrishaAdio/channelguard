"""Conversation -> a complete standalone HTML document.

The document references no network resources: fonts come from the system
(fontconfig) and every image is already a data URI, so rendering is
deterministic and works offline. A small script at the end measures the
timestamp width to reproduce Telegram's "time sits on the last line if it fits"
behaviour, then flips `data-ready` once fonts and images have settled — that
attribute is what `render.py` waits for before screenshotting.
"""

from __future__ import annotations

from .entities import esc, esc_attr, to_html
from .group import apply_grouping, format_time, with_date_separators
from .model import Conversation, Media, Msg
from .theme import avatar_gradient, name_color, palette, wallpaper as wallpaper_for

# --------------------------------------------------------------------------
# static CSS
# --------------------------------------------------------------------------

_CSS = """
*,*::before,*::after{box-sizing:border-box}
html,body{margin:0;padding:0;background:transparent}
body{-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}

/* Opaque backdrop: Telegram re-encodes photos as JPEG, so alpha here would be
   flattened to black and the window shadow would look like a hard edge. */
#shot{display:inline-block;padding:var(--pad);background:var(--backdrop)}

#win{
  width:var(--w);border-radius:12px;overflow:hidden;position:relative;
  box-shadow:var(--winshadow);
}
#win.bare{border-radius:14px;box-shadow:none}

/* ---- macOS title bar ---- */
#bar{
  height:52px;display:flex;align-items:center;gap:10px;padding:0 14px;
  background:var(--titlebar);border-bottom:1px solid var(--titlebarborder);
  backdrop-filter:saturate(180%) blur(20px);position:relative;z-index:2;
}
.lights{display:flex;gap:8px;margin-right:6px}
.light{width:12px;height:12px;border-radius:50%}
.l1{background:#FF5F57}.l2{background:#FEBC2E}.l3{background:#28C840}
#bartitle{display:flex;flex-direction:column;justify-content:center;min-width:0}
#bartitle .t{font-size:14px;font-weight:600;color:var(--titletext);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#bartitle .s{font-size:12px;color:var(--subtitletext);margin-top:1px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}

/* ---- wallpaper ---- */
#pane{position:relative;padding:14px 16px 18px;background:var(--bg)}
#pane.wp{
  background:
    radial-gradient(70% 70% at 18% 22%, var(--wp1) 0%, transparent 60%),
    radial-gradient(70% 70% at 82% 18%, var(--wp2) 0%, transparent 60%),
    radial-gradient(70% 70% at 20% 84%, var(--wp3) 0%, transparent 60%),
    radial-gradient(80% 80% at 84% 82%, var(--wp4) 0%, transparent 62%),
    var(--wp2);
}

/* ---- date + service pills ---- */
.sep{display:flex;justify-content:center;margin:12px 0 14px}
.sep span,.svc span{
  background:var(--dividerbg);color:var(--dividertext);
  font-size:12.5px;font-weight:600;padding:4px 11px;border-radius:12px;
}
.svc{display:flex;justify-content:center;margin:10px 0}
.svc span{font-weight:500}

/* ---- rows ---- */
.row{display:flex;align-items:flex-end;gap:7px;margin-top:2px}
.row.g{margin-top:9px}
.row.out{justify-content:flex-end}
.av{width:33px;height:33px;border-radius:50%;flex:0 0 33px;overflow:hidden;
  display:flex;align-items:center;justify-content:center}
.av img{width:100%;height:100%;object-fit:cover;display:block}
.av .ini{color:#fff;font-size:13.5px;font-weight:600;letter-spacing:.2px}
.avpad{width:33px;flex:0 0 33px}

/* ---- bubbles ---- */
.bubble{
  position:relative;max-width:74%;padding:6px 10px 7px;border-radius:14px;
  font-size:15.5px;line-height:1.31;word-wrap:break-word;overflow-wrap:anywhere;
  white-space:pre-wrap;
}
.row.in  .bubble{background:var(--bin);color:var(--tin);box-shadow:var(--bshadow)}
.row.out .bubble{background:var(--bout);color:var(--tout)}
.row.in  .bubble.t{border-bottom-left-radius:4px}
.row.out .bubble.t{border-bottom-right-radius:4px}

/* tail fin */
.fin{position:absolute;bottom:0;width:9px;height:16px}
.row.in  .fin{left:-7px}
.row.out .fin{right:-7px;transform:scaleX(-1)}
.row.in  .fin path{fill:var(--bin)}
.row.out .fin path{fill:var(--bout)}

/* ---- sender name ---- */
.nm{font-size:14.5px;font-weight:600;margin-bottom:1px;display:block}

/* ---- reply / forward headers ---- */
.rep{
  display:flex;gap:7px;align-items:stretch;margin:1px 0 4px;
  border-radius:5px;padding:3px 7px 3px 0;position:relative;overflow:hidden;
}
.row.in .rep{background:var(--qbgin)}
.row.out .rep{background:var(--qbgout)}
.rep .bar{width:3px;border-radius:2px;flex:0 0 3px}
.rep .tx{min-width:0;display:flex;flex-direction:column;justify-content:center}
.rep .rn{font-size:13.5px;font-weight:600;line-height:1.25;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rep .rt{font-size:13.5px;line-height:1.25;opacity:.78;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rep .rthumb{width:32px;height:32px;border-radius:4px;object-fit:cover;flex:0 0 32px}
.fwd{font-size:13.5px;margin-bottom:2px}
.fwd b{font-weight:600}

/* ---- inline styles ---- */
.bubble b{font-weight:600}
.bubble i{font-style:italic}
.bubble s{text-decoration:line-through}
.bubble u{text-decoration:underline}
.bubble a.lnk{text-decoration:none}
.row.in .bubble a.lnk{color:var(--linkin)}
.row.out .bubble a.lnk{color:var(--linkout);text-decoration:underline}
code.mono,pre.mono{font-family:var(--mono);font-size:14px}
.row.in code.mono{background:var(--codein)}
.row.out code.mono{background:var(--codeout)}
code.mono{padding:1px 4px;border-radius:4px}
.pre{border-radius:6px;padding:6px 9px;margin:3px 0;overflow:hidden}
.row.in .pre{background:var(--codein)}
.row.out .pre{background:var(--codeout)}
.pre-lang{font-size:12px;font-weight:600;opacity:.65;margin-bottom:2px}
.pre pre{margin:0;white-space:pre-wrap}
.bq{margin:3px 0;padding:3px 9px;border-left:3px solid currentColor;
  border-radius:4px;font-size:15px}
.row.in .bq{background:var(--qbgin)}
.row.out .bq{background:var(--qbgout)}
.sp{background:currentColor;border-radius:3px;opacity:.28}
img.ce{width:1.16em;height:1.16em;vertical-align:-0.19em;object-fit:contain}

/* ---- media ---- */
/* Telegram runs a photo edge-to-edge inside the bubble. `overflow:hidden` would
   clip the tail fin, so the image carries the corner radii itself instead. */
.ph{display:block;max-width:100%;object-fit:cover}
.phmiss{display:flex;align-items:center;justify-content:center;max-width:100%;
  background:rgba(127,127,127,.22);border-radius:14px 14px 0 0}
.phmiss span{font-size:13px;opacity:.6}
.bubble.media.nocap .phmiss{border-radius:14px}
.bubble.media{padding:0}
.bubble.media .ph{border-radius:14px 14px 0 0}
.bubble.media.nocap .ph{border-radius:14px}
.bubble.media .cap{padding:5px 10px 7px}
.stk{display:block;width:170px;height:auto}
.bubble.bare{background:none!important;box-shadow:none!important;padding:0}
.vidwrap{position:relative;display:inline-block;line-height:0}
.play{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);
  width:44px;height:44px;border-radius:50%;background:rgba(0,0,0,.42);
  display:flex;align-items:center;justify-content:center}
.pill{position:absolute;left:7px;top:7px;background:rgba(0,0,0,.5);color:#fff;
  font-size:12px;font-weight:600;padding:2px 6px;border-radius:9px}
.voice{display:flex;align-items:center;gap:9px;padding:2px 2px 2px 0}
.vbtn{width:36px;height:36px;border-radius:50%;flex:0 0 36px;
  display:flex;align-items:center;justify-content:center}
.row.in .vbtn{background:var(--bout)}
.row.out .vbtn{background:rgba(255,255,255,.22)}
.wave{display:flex;align-items:center;gap:2px;height:22px}
.wave i{display:block;width:2px;border-radius:1px;background:currentColor;opacity:.55}
.wave i.on{opacity:1}
.vdur{font-size:12.5px;opacity:.7;margin-top:2px}
.doc{display:flex;align-items:center;gap:9px}
.dicon{width:38px;height:38px;border-radius:50%;flex:0 0 38px;
  display:flex;align-items:center;justify-content:center}
.row.in .dicon{background:var(--bout)}
.row.out .dicon{background:rgba(255,255,255,.22)}
.dmeta{min-width:0}
.dname{font-size:14.5px;font-weight:500;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis}
.dsize{font-size:12.5px;opacity:.65}
.wp-prev{display:flex;gap:8px;margin-top:3px;padding-left:8px;
  border-left:3px solid currentColor;border-radius:3px}
.wp-prev .wt{min-width:0}
.wp-site{font-size:13.5px;font-weight:600}
.wp-title{font-size:13.5px;font-weight:600}
.wp-desc{font-size:13.5px;opacity:.8}
.wp-thumb{width:54px;height:54px;border-radius:6px;object-fit:cover;flex:0 0 54px}

/* ---- meta (time + ticks) ---- */
.metasp{display:inline-block;height:1px}
.meta{position:absolute;right:10px;bottom:6px;display:flex;align-items:center;
  gap:3px;font-size:11.5px;line-height:1;white-space:nowrap}
.row.in .meta{color:var(--metain)}
.row.out .meta{color:var(--metaout)}
/* A caption-less photo shows the time as a pill floating over the image;
   with a caption the time sits at the end of the caption text instead. */
.bubble.media.nocap .meta{right:8px;bottom:8px;background:rgba(0,0,0,.36);
  color:#fff!important;padding:2px 6px;border-radius:9px}
.bubble.media .cap{position:relative}
.meta svg{display:block}
.edited{opacity:.75}
"""

_FIN = (
    '<svg class="fin" viewBox="0 0 9 16" width="9" height="16">'
    '<path d="M9 16V0c0 6.2-1.1 10.3-3.6 12.8C4 14.4 2.2 15.4 0 16h9z"/>'
    "</svg>"
)

_TICK1 = (
    '<svg width="15" height="10" viewBox="0 0 15 10" fill="none">'
    '<path d="M1 5.2 4.4 8.6 10.8 1.4" stroke="currentColor" stroke-width="1.5" '
    'stroke-linecap="round" stroke-linejoin="round"/></svg>'
)
_TICK2 = (
    '<svg width="17" height="10" viewBox="0 0 17 10" fill="none">'
    '<path d="M1 5.2 4.4 8.6 10.8 1.4" stroke="currentColor" stroke-width="1.5" '
    'stroke-linecap="round" stroke-linejoin="round"/>'
    '<path d="M6.6 8.4 12.9 1.4" stroke="currentColor" stroke-width="1.5" '
    'stroke-linecap="round" stroke-linejoin="round"/></svg>'
)

_PLAY = (
    '<svg width="17" height="18" viewBox="0 0 17 18" fill="#fff">'
    '<path d="M2 1.6 15 9 2 16.4z"/></svg>'
)
_MIC = (
    '<svg width="15" height="20" viewBox="0 0 15 20" fill="#fff">'
    '<rect x="5" y="1" width="5" height="10" rx="2.5"/>'
    '<path d="M3 9a4.5 4.5 0 0 0 9 0" stroke="#fff" stroke-width="1.6" fill="none"/>'
    '<rect x="6.9" y="14" width="1.2" height="4" rx=".6"/></svg>'
)
_DOC = (
    '<svg width="17" height="19" viewBox="0 0 17 19" fill="#fff">'
    '<path d="M2 1.6h7.2L15 7v10.4H2z" opacity=".95"/></svg>'
)

_SCRIPT = """
(async function(){
  try{ await document.fonts.ready; }catch(e){}
  try{
    await Promise.all([...document.images].map(function(i){
      return (i.decode? i.decode(): Promise.resolve()).catch(function(){});
    }));
  }catch(e){}
  // Telegram reserves room for the timestamp inside the bubble so it sits on
  // the last text line when it fits and wraps to its own line when it does not.
  document.querySelectorAll('.bubble').forEach(function(b){
    var meta=b.querySelector('.meta');
    var sp=b.querySelector('.metasp');
    if(meta&&sp){ sp.style.width=(Math.ceil(meta.getBoundingClientRect().width)+9)+'px'; }
  });
  requestAnimationFrame(function(){
    requestAnimationFrame(function(){
      document.documentElement.setAttribute('data-ready','1');
    });
  });
})();
"""


# --------------------------------------------------------------------------
# fragments
# --------------------------------------------------------------------------


#: Stand-in shown wherever a name is suppressed.
_MASKED_NAME = "\u2014"


def _mask_name(name: str, options) -> str:
    """Apply `hide_sender_names` consistently.

    The option exists to post a proof without exposing who the other party is,
    so it has to cover every place a name appears — a bubble name, a reply
    header and a forward header all identify the same person.
    """
    return _MASKED_NAME if options.hide_sender_names else name


def _avatar(name: str, avatar: str, color_index: int, extra: str = "av") -> str:
    if avatar:
        return f'<div class="{extra}"><img src="{esc_attr(avatar)}" alt=""></div>'
    start, end = avatar_gradient(color_index)
    initials = esc(_initials(name))
    return (
        f'<div class="{extra}" style="background:linear-gradient(180deg,{start},{end})">'
        f'<span class="ini">{initials}</span></div>'
    )


def _initials(name: str) -> str:
    parts = [p for p in (name or "").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:1].upper()
    return (parts[0][:1] + parts[-1][:1]).upper()


def _waveform(values: list[int], out: bool) -> str:
    data = list(values or [])
    if not data:
        data = [6, 12, 20, 9, 26, 14, 30, 11, 22, 8, 17, 25, 10, 19, 7] * 3
    # Telegram sends 5-bit amplitudes; 48 bars reads well at bubble width.
    if len(data) > 48:
        step = len(data) / 48.0
        data = [data[int(i * step)] for i in range(48)]
    bars = []
    for index, value in enumerate(data):
        height = max(2, round((min(31, max(0, value)) / 31) * 20))
        state = "on" if index < len(data) * 0.55 else ""
        bars.append(f'<i class="{state}" style="height:{height}px"></i>')
    return '<div class="wave">' + "".join(bars) + "</div>"


#: Largest photo edge inside a bubble, in CSS px.
_PHOTO_MAX = 360


def _photo_box(media: Media) -> str:
    width = int(media.w) or 420
    height = int(media.h) or 300
    display = min(width, _PHOTO_MAX)
    return f"width:{display}px;aspect-ratio:{max(1, width)}/{max(1, height)}"


def _missing_image(media: Media) -> str:
    """Placeholder for media that could not be downloaded.

    Emitting `<img src="">` instead would render the browser's broken-image
    glyph, which looks like a bug in the screenshot rather than a missing file.
    """
    return (
        f'<div class="phmiss" style="{_photo_box(media)}">'
        '<span>image unavailable</span></div>'
    )


def _fmt_duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


#: Reserves room for the absolutely-positioned timestamp inside the bubble.
_SPACER = '<span class="metasp"></span>'

#: Media rows whose own last line hosts the timestamp spacer, so the time sits
#: beside the duration/size instead of forcing an empty line beneath.
_INLINE_META_KINDS = frozenset({"voice", "document"})


def _media_html(media: Media, out: bool, spacer: str = "") -> str:
    kind = media.kind
    if kind == "photo":
        # An explicit width plus aspect-ratio is required: media may be a tiny
        # placeholder or a thumbnail, and `height:auto` alone would collapse it.
        if not media.src:
            return _missing_image(media)
        return f'<img class="ph" style="{_photo_box(media)}" src="{esc_attr(media.src)}" alt="">'
    if kind == "sticker":
        return f'<img class="stk" src="{esc_attr(media.src)}" alt="">'
    if kind == "video":
        pill = (
            f'<div class="pill">{esc(_fmt_duration(media.duration))}</div>'
            if media.duration
            else ""
        )
        poster = (
            f'<img class="ph" style="{_photo_box(media)}" src="{esc_attr(media.src)}" alt="">'
            if media.src
            else _missing_image(media)
        )
        return (
            f'<div class="vidwrap">{poster}<div class="play">{_PLAY}</div>{pill}</div>'
        )
    if kind == "voice":
        return (
            '<div class="voice">'
            f'<div class="vbtn">{_MIC}</div>'
            f'<div>{_waveform(media.waveform, out)}'
            f'<div class="vdur">{esc(_fmt_duration(media.duration))}{spacer}</div></div>'
            "</div>"
        )
    if kind == "document":
        return (
            '<div class="doc">'
            f'<div class="dicon">{_DOC}</div>'
            '<div class="dmeta">'
            f'<div class="dname">{esc(media.filename or "file")}</div>'
            f'<div class="dsize">{esc(media.size_label)}{spacer}</div>'
            "</div></div>"
        )
    if kind == "webpage":
        thumb = (
            f'<img class="wp-thumb" src="{esc_attr(media.src)}" alt="">'
            if media.src
            else ""
        )
        rows = []
        if media.site:
            rows.append(f'<div class="wp-site">{esc(media.site)}</div>')
        if media.title:
            rows.append(f'<div class="wp-title">{esc(media.title)}</div>')
        if media.description:
            rows.append(f'<div class="wp-desc">{esc(media.description)}</div>')
        return (
            '<div class="wp-prev">'
            f'<div class="wt">{"".join(rows)}</div>{thumb}</div>'
        )
    return ""


def _meta_html(message: Msg, show_ticks: bool) -> str:
    bits = []
    if message.views is not None:
        bits.append(f"<span>{message.views}</span>")
    if message.edited:
        bits.append('<span class="edited">edited</span>')
    bits.append(f"<span>{esc(message.time_label)}</span>")
    if message.out and show_ticks:
        bits.append(_TICK2 if message.read else _TICK1)
    return '<span class="meta">' + "".join(bits) + "</span>"


def _bubble(message: Msg, options, theme: str, emoji_src) -> str:
    body: list[str] = []

    if message.show_name:
        color = name_color(theme, message.sender.color_index)
        body.append(
            f'<span class="nm" style="color:{color}">'
            f'{esc(_mask_name(message.sender.name, options))}</span>'
        )

    if message.forward_from:
        body.append(
            '<div class="fwd">Forwarded from '
            f'<b>{esc(_mask_name(message.forward_from, options))}</b></div>'
        )

    if message.reply is not None:
        color = name_color(theme, message.reply.color_index)
        thumb = (
            f'<img class="rthumb" src="{esc_attr(message.reply.thumb)}" alt="">'
            if message.reply.thumb
            else ""
        )
        body.append(
            f'<div class="rep"><div class="bar" style="background:{color}"></div>'
            f"{thumb}"
            '<div class="tx">'
            f'<div class="rn" style="color:{color}">'
            f'{esc(_mask_name(message.reply.name, options))}</div>'
            f'<div class="rt">{esc(message.reply.text)}</div>'
            "</div></div>"
        )

    media = message.media
    kind = media.kind if media is not None else ""
    is_photo_like = kind in {"photo", "video"}
    text_html = (
        to_html(message.text, message.entities, emoji_src=emoji_src)
        if message.has_text
        else ""
    )

    if is_photo_like:
        # A caption-less photo shows the time as a pill over the image, so no
        # spacer is needed; with a caption the time joins the caption's last line.
        body.append(_media_html(media, message.out))
        if text_html:
            body.append(f'<div class="cap">{text_html}{_SPACER}</div>')
    elif kind in _INLINE_META_KINDS and not text_html:
        body.append(_media_html(media, message.out, spacer=_SPACER))
    else:
        if media is not None:
            body.append(_media_html(media, message.out))
        if text_html:
            body.append(text_html)
        body.append(_SPACER)

    body.append(_meta_html(message, options.show_ticks))

    classes = ["bubble"]
    if message.last_in_group:
        classes.append("t")
    if is_photo_like:
        classes.append("media")
        if not message.has_text:
            classes.append("nocap")
    if message.media and message.media.is_bubbleless:
        classes.append("bare")

    fin = _FIN if message.last_in_group and not (
        message.media and message.media.is_bubbleless
    ) else ""
    return f'<div class="{" ".join(classes)}">{"".join(body)}{fin}</div>'


def _row(message: Msg, options, theme: str, emoji_src) -> str:
    if message.service:
        return f'<div class="svc"><span>{esc(message.service)}</span></div>'

    side = "out" if message.out else "in"
    gap = " g" if message.first_in_group else ""
    parts: list[str] = []

    if not message.out and not options.hide_avatars:
        if message.show_avatar:
            parts.append(
                _avatar(
                    message.sender.name,
                    message.sender.avatar,
                    message.sender.color_index,
                )
            )
        else:
            parts.append('<div class="avpad"></div>')

    parts.append(_bubble(message, options, theme, emoji_src))
    return f'<div class="row {side}{gap}">{"".join(parts)}</div>'


# --------------------------------------------------------------------------
# document
# --------------------------------------------------------------------------


def build_html(conversation: Conversation, *, emoji_src=None) -> str:
    options = conversation.options
    theme = options.theme
    colors = palette(theme)
    wallpaper = wallpaper_for(theme, options.wallpaper_preset)

    # Applied here rather than by callers so a render can never miss it: without
    # grouping every message gets its own name and avatar, which is the single
    # most obvious tell that an image is not a real client screenshot.
    apply_grouping(conversation.messages, is_private=conversation.meta.is_private)

    for message in conversation.messages:
        message.time_label = format_time(message.ts, options.clock24)

    backdrop = options.backdrop
    if not backdrop or backdrop == "auto":
        backdrop = (
            "linear-gradient(145deg,#0B0F14,#1A2027)"
            if theme == "dark"
            else "linear-gradient(145deg,#EDF1F6,#DCE3EC)"
        )

    root = f"""
:root{{
  --w:{int(options.width)}px;
  --pad:{int(options.padding)}px;
  --backdrop:{backdrop};
  --font:'{options.font}';
  --mono:'{options.mono_font}';
  --bg:{colors["bg"]};
  --bin:{colors["bubble_in"]};
  --bout:{colors["bubble_out"]};
  --tin:{colors["text_in"]};
  --tout:{colors["text_out"]};
  --metain:{colors["meta_in"]};
  --metaout:{colors["meta_out"]};
  --bshadow:{colors["bubble_shadow"]};
  --linkin:{colors["link_in"]};
  --linkout:{colors["link_out"]};
  --codein:{colors["code_bg_in"]};
  --codeout:{colors["code_bg_out"]};
  --qbgin:{colors["quote_bg_in"]};
  --qbgout:{colors["quote_bg_out"]};
  --dividerbg:{colors["divider_bg"]};
  --dividertext:{colors["divider_text"]};
  --titlebar:{colors["titlebar"]};
  --titlebarborder:{colors["titlebar_border"]};
  --titletext:{colors["title_text"]};
  --subtitletext:{colors["subtitle_text"]};
  --winshadow:{colors["window_shadow"]};
  --wp1:{wallpaper[0]};--wp2:{wallpaper[1]};--wp3:{wallpaper[2]};--wp4:{wallpaper[3]};
}}
body{{font-family:var(--font),'Noto Sans Devanagari','Noto Color Emoji',sans-serif}}
"""

    stream: list[str] = []
    for kind, item in with_date_separators(conversation.messages):
        if kind == "date":
            stream.append(f'<div class="sep"><span>{esc(str(item))}</span></div>')
        else:
            stream.append(_row(item, options, theme, emoji_src))

    meta = conversation.meta
    bar = ""
    if options.window_chrome and meta.show_header:
        # In a one-to-one chat the window title *is* the other party's name and
        # the header avatar is their photo, so anonymising has to cover both or
        # hide_sender_names/hide_avatars would leak exactly what they suppress.
        title = meta.title
        if options.hide_sender_names and meta.is_private:
            title = "Private chat"
        anonymous = options.hide_avatars or options.hide_sender_names
        subtitle = (
            f'<span class="s">{esc(meta.subtitle)}</span>' if meta.subtitle else ""
        )
        header_avatar = ""
        if title and not anonymous:
            header_avatar = _avatar(title, meta.avatar, meta.color_index)
        bar = (
            '<div id="bar">'
            '<div class="lights"><div class="light l1"></div>'
            '<div class="light l2"></div><div class="light l3"></div></div>'
            + header_avatar
            + '<div id="bartitle">'
            f'<span class="t">{esc(title)}</span>{subtitle}</div>'
            "</div>"
        )

    win_class = "" if options.window_chrome else " bare"
    # "plain" is a documented preset, so it has to actually mean a flat
    # background rather than quietly falling back to the mesh.
    use_mesh = options.wallpaper and options.wallpaper_preset != "plain"
    pane_class = "pane wp" if use_mesh else "pane"

    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<style>{root}{_CSS}</style></head><body>"
        f'<div id="shot"><div id="win" class="{win_class.strip()}">'
        f'{bar}<div id="pane" class="{pane_class}">{"".join(stream)}</div>'
        "</div></div>"
        f"<script>{_SCRIPT}</script></body></html>"
    )
