"""Entity -> HTML correctness, including the two classic failure modes:
UTF-16 offset drift and mis-nested overlapping entities.
"""

from __future__ import annotations

from html.parser import HTMLParser

import pytest

from chatshot.entities import esc, plain_preview, to_char_offsets, to_html, u16_len
from chatshot.model import Ent

VOID = {"img", "br", "hr", "input"}


class _Balance(HTMLParser):
    """Fails on any unbalanced or crossed tag."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        if not self.stack:
            self.errors.append(f"</{tag}> with nothing open")
        elif self.stack[-1] != tag:
            self.errors.append(f"</{tag}> closed while <{self.stack[-1]}> was open")
            self.stack.pop()
        else:
            self.stack.pop()


def assert_well_formed(html: str) -> None:
    parser = _Balance()
    parser.feed(html)
    parser.close()
    assert not parser.errors, f"{parser.errors} in {html!r}"
    assert not parser.stack, f"unclosed {parser.stack} in {html!r}"


# --------------------------------------------------------------------------
# UTF-16
# --------------------------------------------------------------------------


def test_u16_len_counts_surrogate_pairs_as_two() -> None:
    assert u16_len("abc") == 3
    assert u16_len("\U0001F389") == 2           # party popper
    assert u16_len("\U0001F389 ok") == 5
    assert u16_len("\u20b9500") == 4            # rupee sign is BMP


def test_offsets_after_an_emoji_are_converted_not_shifted() -> None:
    """The bug this exists to prevent: Telegram counts the emoji as 2 units."""
    text = "\U0001F389 bold"
    # Telegram would send offset=3 (emoji=2 units + space) length=4.
    html = to_html(text, [Ent("bold", 3, 4)])
    assert html == "\U0001F389 <b>bold</b>"


def test_offset_conversion_is_exact_for_multiple_emoji() -> None:
    text = "\U0001F525\U0001F525 hi"
    # two emoji = 4 units, space = 1 -> "hi" starts at unit 5
    assert to_html(text, [Ent("bold", 5, 2)]) == "\U0001F525\U0001F525 <b>hi</b>"


def test_entities_outside_the_text_are_clamped_or_dropped() -> None:
    assert to_html("abc", [Ent("bold", 99, 5)]) == "abc"
    assert to_html("abc", [Ent("bold", 1, 999)]) == "a<b>bc</b>"
    assert to_char_offsets("abc", [Ent("bold", 2, 0)]) == []


# --------------------------------------------------------------------------
# overlap / nesting
# --------------------------------------------------------------------------


def test_partially_overlapping_entities_stay_well_formed() -> None:
    html = to_html("abcdef", [Ent("bold", 0, 4), Ent("italic", 2, 4)])
    assert_well_formed(html)
    assert html == "<b>ab</b><b><i>cd</i></b><i>ef</i>"


def test_link_partially_covering_bold_stays_well_formed() -> None:
    html = to_html(
        "click here now",
        [Ent("text_url", 0, 10, url="https://x.test"), Ent("bold", 6, 8)],
    )
    assert_well_formed(html)
    assert 'href="https://x.test"' in html
    assert "<b>" in html


def test_three_way_overlap_stays_well_formed() -> None:
    html = to_html(
        "0123456789",
        [Ent("bold", 0, 6), Ent("italic", 3, 5), Ent("underline", 5, 4)],
    )
    assert_well_formed(html)


@pytest.mark.parametrize(
    "ents",
    [
        [Ent("bold", 0, 3), Ent("italic", 1, 3), Ent("strike", 2, 3)],
        [Ent("italic", 0, 5), Ent("bold", 0, 5)],
        [Ent("spoiler", 1, 3), Ent("bold", 0, 5), Ent("code", 2, 2)],
        [Ent("underline", 4, 2), Ent("strike", 0, 6), Ent("bold", 3, 1)],
    ],
)
def test_overlap_combinations_are_always_well_formed(ents) -> None:
    assert_well_formed(to_html("abcdefgh", ents))


def test_fully_nested_entities_render_nested() -> None:
    html = to_html("abcdef", [Ent("bold", 0, 6), Ent("italic", 2, 2)])
    assert html == "<b>ab</b><b><i>cd</i></b><b>ef</b>"
    assert_well_formed(html)


# --------------------------------------------------------------------------
# escaping
# --------------------------------------------------------------------------


def test_markup_in_message_text_is_escaped() -> None:
    html = to_html("<script>alert(1)</script> & co", [])
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&amp; co" in html


def test_escaping_happens_inside_styled_runs_too() -> None:
    html = to_html("a <b> c", [Ent("bold", 0, 7)])
    assert html == "<b>a &lt;b&gt; c</b>"
    assert_well_formed(html)


def test_link_url_is_attribute_escaped() -> None:
    html = to_html("x", [Ent("text_url", 0, 1, url='" onerror="alert(1)')])
    assert 'onerror="alert' not in html
    assert "&quot;" in html


def test_esc_leaves_newlines_for_pre_wrap_css() -> None:
    assert esc("a\nb") == "a\nb"


# --------------------------------------------------------------------------
# block entities
# --------------------------------------------------------------------------


def test_pre_becomes_a_container_with_a_language_label() -> None:
    html = to_html("print(1)", [Ent("pre", 0, 8, language="python")])
    assert '<div class="pre-lang">python</div>' in html
    assert "<pre" in html
    assert_well_formed(html)


def test_blockquote_becomes_a_container() -> None:
    html = to_html("quoted", [Ent("blockquote", 0, 6)])
    assert html.startswith('<blockquote class="bq">')
    assert_well_formed(html)


def test_collapsed_blockquote_is_marked() -> None:
    html = to_html("quoted", [Ent("blockquote", 0, 6, collapsed=True)])
    assert "bq collapsed" in html


def test_text_around_a_block_is_kept_in_order() -> None:
    text = "before\ncode\nafter"
    html = to_html(text, [Ent("pre", 7, 4)])
    assert html.index("before") < html.index("<pre") < html.index("after")
    assert_well_formed(html)


def test_inline_styles_apply_inside_a_blockquote() -> None:
    html = to_html("quoted text", [Ent("blockquote", 0, 11), Ent("bold", 0, 6)])
    assert "<blockquote" in html and "<b>quoted</b>" in html
    assert_well_formed(html)


# --------------------------------------------------------------------------
# custom emoji
# --------------------------------------------------------------------------


def test_custom_emoji_is_replaced_by_an_image() -> None:
    text = "hi \U0001F389"
    html = to_html(
        text,
        [Ent("custom_emoji", 3, 2, document_id=777)],
        emoji_src=lambda doc: f"data:image/webp;base64,AA{doc}",
    )
    assert '<img class="ce"' in html
    assert "AA777" in html
    assert "\U0001F389" not in html          # placeholder glyph consumed
    assert html.startswith("hi ")


def test_custom_emoji_falls_back_to_its_placeholder_glyph() -> None:
    text = "hi \U0001F389"
    html = to_html(text, [Ent("custom_emoji", 3, 2, document_id=777)])
    assert "<img" not in html
    assert html == "hi \U0001F389"


def test_custom_emoji_inside_bold_keeps_the_styling() -> None:
    text = "a\U0001F389b"
    html = to_html(
        text,
        [Ent("bold", 0, 4), Ent("custom_emoji", 1, 2, document_id=5)],
        emoji_src=lambda _doc: "x.webp",
    )
    assert_well_formed(html)
    assert "<img" in html and "<b>" in html


# --------------------------------------------------------------------------
# misc
# --------------------------------------------------------------------------


def test_empty_text_renders_nothing() -> None:
    assert to_html("", [Ent("bold", 0, 4)]) == ""
    assert to_html("") == ""


def test_code_and_spoiler_classes() -> None:
    assert '<code class="mono">' in to_html("x", [Ent("code", 0, 1)])
    assert '<span class="sp">' in to_html("x", [Ent("spoiler", 0, 1)])


def test_bare_url_and_mention_render_as_links_without_href() -> None:
    html = to_html("@someone", [Ent("mention", 0, 8)])
    assert '<a class="lnk">' in html
    assert "href" not in html


def test_plain_preview_collapses_whitespace_and_truncates() -> None:
    assert plain_preview("a\n\n  b") == "a b"
    long = plain_preview("x" * 200, limit=10)
    assert len(long) == 10 and long.endswith("\u2026")
