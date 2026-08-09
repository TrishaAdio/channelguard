"""Regenerate the README example images.

    python -m chatshot.make_example

Run this after changing the theme or layout so the committed screenshots match
what the renderer actually produces. Uses only Playwright, no imaging library.
"""

from __future__ import annotations

import struct
from pathlib import Path

from .fixtures import sample_conversation
from .render import render_png_sync

OUT_DIR = Path(__file__).resolve().parent.parent / "docs"

#: Committed at 1.75x: visibly sharper than 1x on a retina README while roughly
#: half the bytes of 3x. The bot itself renders at 3x.
EXAMPLE_SCALE = 1.75

TARGETS = {
    "light": "example.png",
    "dark": "example-dark.png",
}


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for theme, filename in TARGETS.items():
        conversation = sample_conversation(theme)
        conversation.options.scale = EXAMPLE_SCALE
        png = render_png_sync(conversation)
        path = OUT_DIR / filename
        path.write_bytes(png)
        width, height = struct.unpack(">II", png[16:24])
        print(f"{path.relative_to(OUT_DIR.parent)}: {width}x{height}  "
              f"{len(png) / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
