"""GitHub-flavored markdown -> Telegram HTML.

Telegram's Bot API supports a small HTML subset when ``parse_mode=HTML``:
``<b> <strong> <i> <em> <u> <s> <code> <pre> <a href=...> <blockquote>``.
That's it. ``<h1>``, ``<ul>``, ``<table>`` and friends are *not* parsed —
they render as literal text and the message either fails outright (on
malformed tags) or shows as garbage.

This module is a pragmatic GFM-to-Telegram-HTML transformer:

  * Bold/italic/code/code-blocks: mapped to the supported subset.
  * Headings: rendered as ``<b>`` (Telegram has no heading concept).
  * Tables: re-rendered as a ``<pre>`` block with column alignment via
    spaces — Telegram's monospace font preserves the layout.
  * Bullet/numbered lists: rendered as ``•`` and ``1.`` prefixes; the
    list semantics don't exist in Telegram HTML.
  * Links: ``[text](url)`` -> ``<a href="url">text</a>``.
  * Horizontal rules: rendered as a blank line.
  * Claude Code's ``★ Insight ───...`` blocks (learning-output-style):
    stripped, since we never want them in a chat reply.

The transformation is careful about ordering: code spans are extracted
*before* HTML escaping so their content is preserved verbatim, and
*before* bold/italic processing so a ``**`` inside a code span doesn't
trigger bold matching.

If the output is rejected by Telegram (malformed HTML somehow), the
caller falls back to sending the original plain text.
"""

from __future__ import annotations

import html
import re

__all__ = ["balance_html_tags_across_chunks", "markdown_to_telegram_html"]


# ---------------------------------------------------------------------------
# Code-span / code-block extraction with placeholders
# ---------------------------------------------------------------------------


_PLACEHOLDER_PREFIX = "\x00CODE"
_PLACEHOLDER_SUFFIX = "\x00"


def _make_placeholder(idx: int) -> str:
    return f"{_PLACEHOLDER_PREFIX}{idx}{_PLACEHOLDER_SUFFIX}"


_FENCED_BLOCK = re.compile(r"```([a-zA-Z0-9_+-]*)\n(.*?)```", re.DOTALL)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")


def _extract_code(text: str, store: list[str]) -> str:
    """Replace fenced and inline code with placeholders; remember rendered HTML."""

    def fenced(match: re.Match[str]) -> str:
        lang = match.group(1).strip()
        body = match.group(2).rstrip("\n")
        body_escaped = html.escape(body, quote=False)
        if lang:
            rendered = f'<pre><code class="language-{lang}">{body_escaped}</code></pre>'
        else:
            rendered = f"<pre>{body_escaped}</pre>"
        store.append(rendered)
        return _make_placeholder(len(store) - 1)

    text = _FENCED_BLOCK.sub(fenced, text)

    def inline(match: re.Match[str]) -> str:
        body_escaped = html.escape(match.group(1), quote=False)
        store.append(f"<code>{body_escaped}</code>")
        return _make_placeholder(len(store) - 1)

    text = _INLINE_CODE.sub(inline, text)
    return text


def _restore_placeholders(text: str, store: list[str]) -> str:
    for idx, rendered in enumerate(store):
        text = text.replace(_make_placeholder(idx), rendered)
    return text


# ---------------------------------------------------------------------------
# Learning-mode "Insight" block stripping
# ---------------------------------------------------------------------------


# Matches the Claude Code learning-output-style insight block, e.g.:
#   ★ Insight ─────────────────────────────────────
#   ...content...
#   ─────────────────────────────────────────────────
_INSIGHT_BLOCK = re.compile(
    r"(?:^|\n)\s*[★\*]?\s*Insight\s*[─━-]{2,}.*?[─━-]{2,}\s*",
    re.DOTALL,
)


def _strip_insight_blocks(text: str) -> str:
    return _INSIGHT_BLOCK.sub("\n", text)


# ---------------------------------------------------------------------------
# Tables: pipe syntax -> aligned <pre> block
# ---------------------------------------------------------------------------


_TABLE_BLOCK = re.compile(
    r"(?:^|\n)((?:\|.*\|\s*\n)+)",
    re.MULTILINE,
)


def _is_separator_row(cells: list[str]) -> bool:
    """The ``|---|---|---|`` row that follows the header in GFM tables."""
    return all(re.match(r"^:?-+:?$", c.strip()) for c in cells if c.strip())


def _render_table(raw: str) -> str:
    rows = [line for line in raw.strip().splitlines() if line.strip().startswith("|")]
    parsed: list[list[str]] = []
    for row in rows:
        # strip leading/trailing | and split
        inner = row.strip().strip("|")
        cells = [c.strip() for c in inner.split("|")]
        if _is_separator_row(cells):
            continue
        parsed.append(cells)

    if not parsed:
        return raw

    cols = max(len(r) for r in parsed)
    for r in parsed:
        while len(r) < cols:
            r.append("")

    # Strip our own bold/italic markers from cell content (the <pre> block
    # won't render them anyway and they pollute alignment width).
    def clean(cell: str) -> str:
        return re.sub(r"\*\*?(.+?)\*\*?", r"\1", cell)

    parsed = [[clean(c) for c in r] for r in parsed]

    widths = [max(len(r[i]) for r in parsed) for i in range(cols)]
    lines = []
    for r in parsed:
        line = "  ".join(r[i].ljust(widths[i]) for i in range(cols)).rstrip()
        lines.append(line)
    body = "\n".join(lines)
    return f"\n<pre>{html.escape(body, quote=False)}</pre>\n"


def _convert_tables(text: str) -> str:
    return _TABLE_BLOCK.sub(lambda m: _render_table(m.group(1)), text)


# ---------------------------------------------------------------------------
# Bold / italic / strikethrough / links / headings / hrules / lists
# ---------------------------------------------------------------------------


# Order matters: **bold** before *italic* so the inner * doesn't match first.
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_BOLD_UNDERSCORE = re.compile(r"__(.+?)__", re.DOTALL)
_ITALIC_STAR = re.compile(r"(?<!\*)\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\*)")
_ITALIC_UNDERSCORE = re.compile(r"(?<!_)_(?!\s)([^_\n]+?)(?<!\s)_(?!_)")
_STRIKE = re.compile(r"~~(.+?)~~", re.DOTALL)
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_HRULE = re.compile(r"^\s*[-*_]{3,}\s*$", re.MULTILINE)
_UL_BULLET = re.compile(r"^(\s*)[-*+]\s+", re.MULTILINE)


def _convert_formatting(text: str) -> str:
    text = _BOLD.sub(r"<b>\1</b>", text)
    text = _BOLD_UNDERSCORE.sub(r"<b>\1</b>", text)
    text = _STRIKE.sub(r"<s>\1</s>", text)
    text = _ITALIC_STAR.sub(r"<i>\1</i>", text)
    text = _ITALIC_UNDERSCORE.sub(r"<i>\1</i>", text)
    text = _LINK.sub(r'<a href="\2">\1</a>', text)
    text = _HEADING.sub(lambda m: f"<b>{m.group(2)}</b>", text)
    text = _HRULE.sub("", text)
    text = _UL_BULLET.sub(lambda m: f"{m.group(1)}• ", text)
    return text


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def markdown_to_telegram_html(md: str) -> str:
    """Convert GFM-flavored markdown to Telegram-flavored HTML."""
    if not md:
        return md

    md = _strip_insight_blocks(md)

    # Extract code spans first so their content is shielded from later
    # escaping and substitution.
    code_store: list[str] = []
    md = _extract_code(md, code_store)

    # Now safe to escape: code placeholders are NUL-bracketed and contain
    # no special chars to escape.
    md = html.escape(md, quote=False)

    md = _convert_tables(md)
    md = _convert_formatting(md)

    md = _restore_placeholders(md, code_store)

    # Collapse runs of 3+ newlines that formatting may have introduced.
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


# ---------------------------------------------------------------------------
# Chunk-boundary tag balancing (issue #28)
#
# Telegram's HTML parser rejects a message that opens a ``<b>`` / ``<i>`` /
# ``<code>`` and never closes it. When the digest send layer slices an
# over-4096-char HTML payload on paragraph boundaries, a slice may begin or
# end mid-tag — e.g. ``<b>some long bold span ...`` lands at the end of
# chunk K and the closing ``</b>`` is in chunk K+1. The fix: walk each
# chunk in order, track which simple tags are open at the seam, append the
# missing closers to chunk K and prepend the corresponding reopeners to
# chunk K+1. Each chunk then parses standalone.
#
# Only the three flat tags Telegram cares about for this slice
# (``<b>``, ``<i>``, ``<code>``) are balanced — they're the ones the digest
# actually emits. ``<a href=...>`` is intentionally out of scope: it has an
# attribute, and our paragraph-boundary splitter never breaks a link
# because a link cannot straddle a ``\n\n``. ``<pre>`` is also out of
# scope; code blocks are atomic units in the source markdown and so end up
# wholly inside a single chunk.
# ---------------------------------------------------------------------------


# Order matters for the reopen prefix: opening order must match the
# closing order's reverse for nested tags to come back out the same way.
_BALANCED_TAGS = ("b", "i", "code")


def _scan_open_tags(chunk: str, tags: tuple[str, ...]) -> list[str]:
    """Return the tags currently open at the end of ``chunk``, in
    open-order. ``<b><i>...<b>`` returns ``["b", "i", "b"]``.

    The scan walks the chunk left-to-right, treating any open/close token
    for a tracked tag as a push/pop on a stack. We don't try to validate
    nesting — Telegram doesn't care that ``<b>x<i>y</b>z</i>`` is wrong
    XML; the converter doesn't produce it; and the only thing the
    balancer must do is leave each chunk with no *unclosed* tags so the
    parser accepts it.
    """
    pattern = re.compile(r"</?(" + "|".join(tags) + r")>")
    stack: list[str] = []
    for match in pattern.finditer(chunk):
        token = match.group(0)
        tag = match.group(1)
        if token.startswith("</"):
            # Pop the most recent matching open if there is one. If the
            # closer has no matching opener (e.g. it was opened in a
            # previous chunk and we're walking only this one), ignore —
            # the caller scans each chunk in isolation.
            for i in range(len(stack) - 1, -1, -1):
                if stack[i] == tag:
                    del stack[i]
                    break
        else:
            stack.append(tag)
    return stack


def balance_html_tags_across_chunks(chunks: list[str]) -> list[str]:
    """Close-and-reopen ``<b>`` / ``<i>`` / ``<code>`` across chunk seams.

    Walks ``chunks`` in order. For each chunk K, finds any tracked tags
    still open at K's end, appends their closers to K, and prepends the
    same tags (as reopeners, in open-order) to chunk K+1. The result is a
    list of the same length where every chunk is standalone valid
    Telegram HTML.

    Empty input or a single chunk passes through unchanged — the function
    is a no-op when there are no seams.
    """
    if len(chunks) <= 1:
        return list(chunks)

    out = list(chunks)
    # Tags carried over from the previous chunk's unclosed openers. Each
    # chunk K reopens whatever K-1 closed.
    reopen_prefix: list[str] = []
    for idx in range(len(out)):
        if reopen_prefix:
            out[idx] = "".join(f"<{t}>" for t in reopen_prefix) + out[idx]
        open_at_end = _scan_open_tags(out[idx], _BALANCED_TAGS)
        if open_at_end and idx < len(out) - 1:
            # Close in reverse open-order so nested tags unwind LIFO.
            out[idx] = out[idx] + "".join(
                f"</{t}>" for t in reversed(open_at_end)
            )
            reopen_prefix = list(open_at_end)
        else:
            reopen_prefix = []
    return out
