"""Tests for the markdown -> Telegram HTML transformer."""

from __future__ import annotations

from agent.format import markdown_to_telegram_html


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
