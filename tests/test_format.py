"""Tests for the markdown -> Telegram HTML transformer."""

from __future__ import annotations

from agent.format import markdown_to_telegram_html, strip_markdown_markers


def test_bold_double_star() -> None:
    assert markdown_to_telegram_html("**hello**") == "<b>hello</b>"


def test_bold_double_underscore() -> None:
    assert markdown_to_telegram_html("__hello__") == "<b>hello</b>"


def test_italic_star() -> None:
    assert markdown_to_telegram_html("*italic*") == "<i>italic</i>"


def test_italic_inside_bold_is_just_bold() -> None:
    # **bold** should NOT eat the inner * as italic markers.
    assert markdown_to_telegram_html("**bold text**") == "<b>bold text</b>"


def test_inline_code_preserved_verbatim() -> None:
    out = markdown_to_telegram_html("call `os.path.join(x, y)` instead")
    assert "<code>os.path.join(x, y)</code>" in out


def test_code_block_with_language() -> None:
    md = "```python\nprint('hi')\n```"
    out = markdown_to_telegram_html(md)
    assert '<pre><code class="language-python">' in out
    assert "print(&#x27;hi&#x27;)" in out or "print('hi')" in out


def test_code_block_without_language() -> None:
    md = "```\njust text\n```"
    out = markdown_to_telegram_html(md)
    assert "<pre>just text</pre>" in out


def test_html_special_chars_escaped() -> None:
    out = markdown_to_telegram_html("a < b & c > d")
    assert "&lt;" in out and "&gt;" in out and "&amp;" in out


def test_links_converted() -> None:
    out = markdown_to_telegram_html("see [docs](https://x.com/y)")
    assert '<a href="https://x.com/y">docs</a>' in out


def test_headings_become_bold() -> None:
    assert "<b>Title</b>" in markdown_to_telegram_html("# Title")
    assert "<b>Sub</b>" in markdown_to_telegram_html("### Sub")


def test_bullet_lists_get_bullet_char() -> None:
    md = "- first\n- second"
    out = markdown_to_telegram_html(md)
    assert "• first" in out
    assert "• second" in out


def test_horizontal_rule_stripped() -> None:
    out = markdown_to_telegram_html("a\n\n---\n\nb")
    assert "---" not in out
    assert "a" in out and "b" in out


def test_table_renders_as_pre_block_with_alignment() -> None:
    md = (
        "| Day | Session |\n"
        "|-----|---------|\n"
        "| Mon | Push    |\n"
        "| Wed | Pull    |\n"
    )
    out = markdown_to_telegram_html(md)
    assert "<pre>" in out and "</pre>" in out
    # Both rows present, header preserved.
    assert "Day" in out and "Session" in out
    assert "Mon" in out and "Push" in out
    assert "Wed" in out and "Pull" in out
    # Separator row should be dropped.
    assert "---" not in out


def test_table_strips_bold_markers_in_cells() -> None:
    md = "| Day | Session |\n|---|---|\n| **Mon** | **Push** |\n"
    out = markdown_to_telegram_html(md)
    # Bold should be flattened inside the pre block.
    assert "**Mon**" not in out
    assert "Mon" in out


def test_insight_block_stripped() -> None:
    md = (
        "the reply\n\n"
        "★ Insight ─────────────────────────────\n"
        "some learning content here\n"
        "─────────────────────────────────────────\n"
        "tail"
    )
    out = markdown_to_telegram_html(md)
    assert "Insight" not in out
    assert "some learning content here" not in out
    assert "the reply" in out
    assert "tail" in out


def test_combined_realistic_reply() -> None:
    md = (
        "Here's the schedule:\n\n"
        "**Weekly pattern**\n\n"
        "| Day | Session |\n"
        "|---|---|\n"
        "| **Mon** | Push |\n"
        "| **Wed** | Pull |\n\n"
        "**Why this shape:**\n\n"
        "- PPL gives focused stimulus\n"
        "- 45-60s rest keeps density\n\n"
        "Plan saved at `fitness/plans/2026-05-11.md`."
    )
    out = markdown_to_telegram_html(md)
    assert "<b>Weekly pattern</b>" in out
    assert "<pre>" in out
    assert "• PPL gives focused stimulus" in out
    assert "<code>fitness/plans/2026-05-11.md</code>" in out


def test_empty_input_returns_empty() -> None:
    assert markdown_to_telegram_html("") == ""


def test_no_markdown_passes_through() -> None:
    assert markdown_to_telegram_html("just text") == "just text"


# ---------------------------------------------------------------------------
# strip_markdown_markers — the plain-text-fallback helper (issue #29)
#
# The HTML send path is the happy case; when Telegram rejects the HTML payload
# the send layer falls back to a plain-text retry. This helper strips the
# Telegram-subset markdown markers so the fallback message is *readable* —
# without it the user sees literal ``**asterisks**`` / ``# heading`` / etc.
#
# These tests pin each marker class independently plus a few common
# compositions (nested markers, fenced code blocks, links). The helper is the
# plain-text twin of ``markdown_to_telegram_html``: same input vocabulary,
# different output target.
# ---------------------------------------------------------------------------


def test_strip_markdown_markers_bold_double_star() -> None:
    assert strip_markdown_markers("**hello**") == "hello"


def test_strip_markdown_markers_bold_double_underscore() -> None:
    assert strip_markdown_markers("__hello__") == "hello"


def test_strip_markdown_markers_italic_single_star() -> None:
    assert strip_markdown_markers("*italic*") == "italic"


def test_strip_markdown_markers_italic_single_underscore() -> None:
    assert strip_markdown_markers("_italic_") == "italic"


def test_strip_markdown_markers_inline_code() -> None:
    assert strip_markdown_markers("call `os.path.join(x, y)` instead") == (
        "call os.path.join(x, y) instead"
    )


def test_strip_markdown_markers_fenced_code_block_keeps_body() -> None:
    """Fenced code blocks drop the ``` fences and any language tag; the body
    survives so the fallback message is still readable."""
    md = "```python\nprint('hi')\n```"
    out = strip_markdown_markers(md)
    assert "```" not in out
    assert "python" not in out.splitlines()[0] or out.splitlines()[0] == "print('hi')"
    assert "print('hi')" in out


def test_strip_markdown_markers_fenced_code_block_no_language() -> None:
    md = "```\njust text\n```"
    out = strip_markdown_markers(md)
    assert "```" not in out
    assert "just text" in out


def test_strip_markdown_markers_strikethrough() -> None:
    assert strip_markdown_markers("~~gone~~") == "gone"


def test_strip_markdown_markers_dash_bullets() -> None:
    md = "- first\n- second"
    out = strip_markdown_markers(md)
    # The bullet markers are gone, the text after each survives.
    assert "first" in out
    assert "second" in out
    assert "- first" not in out
    assert "- second" not in out


def test_strip_markdown_markers_star_bullets() -> None:
    md = "* first\n* second"
    out = strip_markdown_markers(md)
    assert "first" in out
    assert "second" in out
    # Leading "* " is gone; the helper must not confuse a leading bullet with
    # an italic marker (italics are paired, bullets are anchored to line-start).
    assert "* first" not in out


def test_strip_markdown_markers_plus_bullets() -> None:
    md = "+ first\n+ second"
    out = strip_markdown_markers(md)
    assert "first" in out
    assert "second" in out
    assert "+ first" not in out


def test_strip_markdown_markers_headings_one_through_six() -> None:
    """All six heading levels: leading ``#``..``######`` markers strip, text
    after the marker survives unchanged."""
    for level in range(1, 7):
        prefix = "#" * level + " "
        md = f"{prefix}Title"
        assert strip_markdown_markers(md) == "Title", (
            f"heading level {level} did not strip cleanly"
        )


def test_strip_markdown_markers_link_unwraps_to_text_and_url() -> None:
    """``[text](url)`` becomes ``text (url)`` — the reader still gets both
    pieces of information when the fallback drops formatting."""
    md = "see [docs](https://x.com/y) for more"
    out = strip_markdown_markers(md)
    assert out == "see docs (https://x.com/y) for more"


def test_strip_markdown_markers_nested_bold_italic() -> None:
    """``**bold _and italic_**`` becomes clean prose: no asterisks, no
    underscores. The helper must not leave dangling markers when a marker
    class is nested inside another."""
    md = "**bold _and italic_**"
    out = strip_markdown_markers(md)
    assert out == "bold and italic"
    assert "*" not in out
    assert "_" not in out


def test_strip_markdown_markers_combined_realistic_digest_body() -> None:
    """A realistic digest body — multiple marker classes mixed together —
    must round-trip to clean prose with no surviving markers."""
    md = (
        "**Due today:** finish PR\n\n"
        "## Coming up\n\n"
        "- Tax filing (due *Friday*)\n"
        "- Rogers payment `$8,280`\n\n"
        "See [the plan](https://example.com/plan)."
    )
    out = strip_markdown_markers(md)
    # No literal markdown markers survive.
    assert "**" not in out
    assert "##" not in out
    assert "`" not in out
    # The bullet text survives.
    assert "Tax filing" in out
    assert "Rogers payment $8,280" in out
    # The heading text survives.
    assert "Coming up" in out
    # The link unwraps.
    assert "the plan (https://example.com/plan)" in out
    # And the leading-bullet text is no longer prefixed with "- ".
    for line in out.splitlines():
        assert not line.startswith("- ")


def test_strip_markdown_markers_empty_string() -> None:
    assert strip_markdown_markers("") == ""


def test_strip_markdown_markers_plain_text_passes_through() -> None:
    """No markers, no change. The helper must be safe to run on text that
    happens to already be marker-free (the steady state for a clean digest
    that hit the fallback for an unrelated reason — converter bug, glyph)."""
    assert strip_markdown_markers("just text") == "just text"
