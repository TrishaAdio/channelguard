"""Colour tokens.

Peer colours and avatar gradients are Telegram's own palette, so a rendered
name/avatar matches what the real client would draw for that account.
"""

from __future__ import annotations

#: Telegram peer colours, index 0-6, used for sender names and reply bars.
NAME_COLORS_LIGHT = [
    "#E15052",  # red
    "#E0802B",  # orange
    "#A05FF3",  # violet
    "#27A910",  # green
    "#27ACCE",  # cyan
    "#3391D4",  # blue
    "#DD4371",  # pink
]

NAME_COLORS_DARK = [
    "#FF8E86",
    "#FFA85C",
    "#B991FF",
    "#46BA43",
    "#40D8D0",
    "#5CAFFA",
    "#FF7FA9",
]

#: Telegram avatar gradients, same index order.
AVATAR_GRADIENTS = [
    ("#FF845E", "#D45246"),
    ("#FEBB5B", "#F68136"),
    ("#B694F9", "#6C61DF"),
    ("#A0DE7E", "#54CB68"),
    ("#53EDD6", "#28C9B7"),
    ("#72D5FD", "#2A9EF1"),
    ("#E0A2F3", "#D669ED"),
]

#: Four-point mesh wallpapers. `slate` is the default: a soft blue-grey that
#: keeps white bubbles crisp and does not fight the blue outgoing bubble the way
#: Telegram's saturated green default does.
WALLPAPERS = {
    "slate": {
        "light": ("#E9EEF5", "#CBDCEC", "#DDE7F1", "#BED2E6"),
        "dark": ("#1B2530", "#121A21", "#22303A", "#0E141A"),
    },
    "telegram": {
        "light": ("#DBDDBB", "#6BA587", "#D5D88D", "#88B884"),
        "dark": ("#1F2A32", "#141B21", "#243138", "#101619"),
    },
}

WALLPAPER_LIGHT = WALLPAPERS["slate"]["light"]
WALLPAPER_DARK = WALLPAPERS["slate"]["dark"]


def wallpaper(theme: str, preset: str = "auto") -> tuple[str, str, str, str]:
    mode = "dark" if str(theme).lower() == "dark" else "light"
    key = "slate" if preset in ("", "auto", "plain") else preset
    return WALLPAPERS.get(key, WALLPAPERS["slate"])[mode]


LIGHT = {
    "bg": "#E7EBF0",
    "bubble_in": "#FFFFFF",
    "bubble_out": "#3990EC",
    "text_in": "#000000",
    "text_out": "#FFFFFF",
    "meta_in": "rgba(0,0,0,0.38)",
    "meta_out": "rgba(255,255,255,0.72)",
    "tick": "#FFFFFF",
    "bubble_shadow": "0 1px 2px rgba(0,0,0,0.10)",
    "link_in": "#168ACD",
    "link_out": "#FFFFFF",
    "code_bg_in": "rgba(0,0,0,0.05)",
    "code_bg_out": "rgba(255,255,255,0.16)",
    "quote_bg_in": "rgba(0,0,0,0.04)",
    "quote_bg_out": "rgba(255,255,255,0.14)",
    "divider_bg": "rgba(0,0,0,0.28)",
    "divider_text": "#FFFFFF",
    "titlebar": "rgba(246,247,249,0.86)",
    "titlebar_border": "rgba(0,0,0,0.10)",
    "title_text": "#000000",
    "subtitle_text": "rgba(0,0,0,0.45)",
    "names": NAME_COLORS_LIGHT,
    "wallpaper": WALLPAPER_LIGHT,
    "window_shadow": "0 24px 70px rgba(0,0,0,0.34), 0 2px 8px rgba(0,0,0,0.18)",
}

DARK = {
    "bg": "#0E1621",
    "bubble_in": "#182533",
    "bubble_out": "#2B5278",
    "text_in": "#FFFFFF",
    "text_out": "#FFFFFF",
    "meta_in": "rgba(255,255,255,0.42)",
    "meta_out": "rgba(255,255,255,0.62)",
    "tick": "#7FCBFF",
    "bubble_shadow": "0 1px 2px rgba(0,0,0,0.22)",
    "link_in": "#62BCF9",
    "link_out": "#8FD0FF",
    "code_bg_in": "rgba(255,255,255,0.07)",
    "code_bg_out": "rgba(255,255,255,0.10)",
    "quote_bg_in": "rgba(255,255,255,0.06)",
    "quote_bg_out": "rgba(255,255,255,0.10)",
    "divider_bg": "rgba(0,0,0,0.42)",
    "divider_text": "rgba(255,255,255,0.9)",
    "titlebar": "rgba(23,33,43,0.88)",
    "titlebar_border": "rgba(255,255,255,0.08)",
    "title_text": "#FFFFFF",
    "subtitle_text": "rgba(255,255,255,0.45)",
    "names": NAME_COLORS_DARK,
    "wallpaper": WALLPAPER_DARK,
    "window_shadow": "0 24px 70px rgba(0,0,0,0.55), 0 2px 8px rgba(0,0,0,0.30)",
}


def palette(theme: str) -> dict:
    return DARK if str(theme).lower() == "dark" else LIGHT


def name_color(theme: str, index: int) -> str:
    colors = palette(theme)["names"]
    return colors[int(index) % len(colors)]


def avatar_gradient(index: int) -> tuple[str, str]:
    return AVATAR_GRADIENTS[int(index) % len(AVATAR_GRADIENTS)]
