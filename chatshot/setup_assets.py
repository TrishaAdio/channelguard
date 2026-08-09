"""Provision the renderer's runtime assets: Chromium and fonts.

    python -m chatshot.setup_assets

Fonts are the part people miss. Without a colour emoji font Chromium draws
emoji as monochrome boxes, and without a UI font it falls back to whatever
DejaVu variant exists, which does not look like macOS at all. Every font
installed here is SIL Open Font License, so it is safe to redistribute.

Apple's SF Pro is the genuine macOS typeface but its licence does not permit
redistribution, so it is never downloaded. If you have it locally, drop the
.otf files into the font directory below and set RenderOptions.font to
"SF Pro Text".
"""

from __future__ import annotations

import subprocess
import sys
import urllib.request
from pathlib import Path

FONT_DIR = Path.home() / ".local" / "share" / "fonts" / "chatshot"

RAW = "https://github.com/google/fonts/raw/main/ofl"
FONTS = {
    # UI text. Variable file covers weights 100-900, which the bubbles need.
    "Inter.ttf": f"{RAW}/inter/Inter%5Bopsz,wght%5D.ttf",
    # Colour emoji. Without this, emoji render as tofu.
    "NotoColorEmoji.ttf": (
        "https://github.com/googlefonts/noto-emoji/raw/main/fonts/NotoColorEmoji.ttf"
    ),
    # `code` / `pre` blocks.
    "JetBrainsMono.ttf": f"{RAW}/jetbrainsmono/JetBrainsMono%5Bwght%5D.ttf",
    # Indic names would otherwise be boxes.
    "NotoSansDevanagari.ttf": (
        f"{RAW}/notosansdevanagari/NotoSansDevanagari%5Bwdth,wght%5D.ttf"
    ),
}

#: A downloaded font must at least look like one; a captive portal or a 404
#: page would otherwise be installed as a "font" and fail silently at render.
_FONT_MAGIC = (b"\x00\x01\x00\x00", b"OTTO", b"true", b"ttcf", b"wOFF", b"wOF2")


def install_fonts(force: bool = False) -> int:
    FONT_DIR.mkdir(parents=True, exist_ok=True)
    installed = 0
    for name, url in FONTS.items():
        target = FONT_DIR / name
        if target.exists() and not force:
            print(f"  = {name} (present)")
            continue
        print(f"  + {name} ... ", end="", flush=True)
        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                payload = response.read()
        except Exception as error:  # noqa: BLE001
            print(f"FAILED ({type(error).__name__}: {error})")
            continue
        if not payload.startswith(_FONT_MAGIC):
            print(f"FAILED (not a font file, got {len(payload)} bytes)")
            continue
        target.write_bytes(payload)
        installed += 1
        print(f"ok ({len(payload) // 1024} KB)")
    return installed


def refresh_font_cache() -> None:
    try:
        subprocess.run(["fc-cache", "-f"], check=False, capture_output=True)
        print("  fontconfig cache refreshed")
    except FileNotFoundError:
        print("  fc-cache not found; install fontconfig if fonts do not appear")


def install_chromium() -> bool:
    print("Installing Chromium for Playwright ...")
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        check=False,
    )
    if result.returncode != 0:
        print("  Chromium install failed. Is playwright installed?")
        print("  pip install playwright")
        return False
    return True


def verify() -> bool:
    """Render a probe image and confirm the fonts actually resolved."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("VERIFY: playwright is not installed")
        return False

    html = (
        "<body style='margin:0'><div id='p' style=\"font-family:Inter;"
        "font-size:20px\">Proof \u20b9500 \U0001F389 \u2705</div></body>"
    )
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(args=["--no-sandbox"])
            page = browser.new_page(device_scale_factor=2)
            page.set_content(html)
            page.wait_for_function("document.fonts.ready.then(()=>true)")
            ok_ui = page.evaluate("document.fonts.check('20px Inter')")
            ok_emoji = page.evaluate(
                "document.fonts.check('20px \"Noto Color Emoji\"')"
            )
            page.locator("#p").screenshot(path="chatshot-verify.png")
            browser.close()
    except Exception as error:  # noqa: BLE001
        print(f"VERIFY: render failed ({type(error).__name__}: {error})")
        return False

    print(f"VERIFY: Inter={'ok' if ok_ui else 'MISSING'} "
          f"ColorEmoji={'ok' if ok_emoji else 'MISSING'}")
    print("VERIFY: wrote chatshot-verify.png")
    return bool(ok_ui and ok_emoji)


def main() -> int:
    print("Fonts (all SIL Open Font License):")
    install_fonts()
    refresh_font_cache()
    if not install_chromium():
        return 1
    return 0 if verify() else 1


if __name__ == "__main__":
    raise SystemExit(main())
