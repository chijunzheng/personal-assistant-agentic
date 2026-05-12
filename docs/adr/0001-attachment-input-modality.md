# ADR 0001: Attachment input modality (Telegram → vault inbox → LLM)

**Status:** Accepted
**Date:** 2026-05-12
**Issue:** [#1](https://github.com/chijunzheng/personal-assistant-agentic/issues/1)

## Context

`agent/telegram_bridge.py` previously listened for text only
(`filters.TEXT & ~filters.COMMAND`). When the user sent a PDF, photo, or
any other document to the bot, the update was silently dropped and the
agent never saw the file.

The motivating use case is Jason's finance workflow: he receives credit-
card statements as PDFs and wants to drop them into the bot, have the
agent extract structured transactions, and write them into the vault's
finance domain. The legacy deterministic kernel handled this with a
brittle "watcher on a syncthing folder" mechanism. The agentic system
should treat attachments as a first-class input modality.

The agentic project's `CLAUDE.md` explicitly authorizes kernel changes
for "new input modality (image, voice)" but requires an ADR documenting
the surface expansion before the change lands.

## Decision

The bridge captures attachments; the vault stores them at a predictable
path; the LLM reads them. Specifically:

1. **Bridge accepts** `filters.Document.ALL | filters.PHOTO` in addition
   to the existing TEXT handler. Both filters are registered on the
   same `Application` as separate `MessageHandler`s. Voice / audio / video
   are deferred to follow-up issues.
2. **Bridge downloads** each attachment to
   `<VAULT_ROOT>/_inbox/raw/<YYYY-MM-DD>/<HHMMSS>-<sanitized-filename>.<ext>`,
   where date/time are UTC and `<sanitized-filename>` is the original
   filename with anything outside `[A-Za-z0-9._-]` collapsed to `_`.
   The file lives at this path forever; processing never moves it.
3. **Bridge does NOT classify** the file. It does not peek at the
   filename or magic bytes to guess "this is a statement." Classification
   is the LLM's job once it `Read`s the file at the saved path. This
   keeps the bridge dumb and the agent intelligent — the contract we
   are buying with `claude -p`.
4. **Idempotency on retries.** Telegram delivers duplicates on transient
   network errors with the same `file_unique_id`. Within the same second,
   the timestamp-based path collides; the bridge skips the download when
   the target path already exists. Cross-second retries land at slightly
   different paths but with identical content — acceptable given Telegram
   retries within ~5s and the LLM treats the duplicate gracefully.
5. **Runner API expansion.** `agent.runner.handle_turn` gains a new
   keyword argument `attachments: Sequence[Path] = ()`. With a default
   of `()`, every existing text-only caller stays byte-identical. The
   runner composes the LLM-facing user message as:
   - With caption + attachments:
     `<caption>\n\n[attachment saved to: <rel>]`
   - Without caption: just `[attachment saved to: <rel>]` (no leading
     newlines, no empty caption prefix)
   - Multiple attachments: each on its own `[attachment saved to: ...]`
     line after the caption block.
6. **Relative paths.** The path threaded into the user message is
   relative to `VAULT_ROOT`. The LLM runs `claude -p` with
   `cwd=vault_root` and the existing `Read` allowlist, so the relative
   path is directly usable. Absolute paths would leak vault-root details
   into the prompt for no gain.

## Alternatives considered

### A. User manually drops file to iCloud / Drive sync folder

Rejected. This is what v1 did. It forced Jason to leave Telegram, find
the right shared folder, and place the file with a specific naming
convention so the watcher would pick it up. The whole point of the
agentic redesign is to keep Telegram as the single input surface.

### B. Bridge converts PDF to text upfront (pdfplumber / pdfminer)

Rejected. (1) Claude Code's `Read` tool natively reads PDFs — no
preprocessing needed. (2) Pre-extracted text loses formatting,
column structure, and embedded images that may be relevant. (3) It
forces the bridge to pick a parser, install dependencies, and own
parser failures — exactly the kind of brittleness this project is
trying to escape. The LLM handles parsing; the bridge handles transport.

### C. Stream the file contents through the prompt directly (no vault save)

Rejected. (1) Attachments routinely exceed Telegram's text-message size
limits and Claude's context window when inlined. (2) Saving to the
vault means the file exists for future turns to reference, for audit,
and for human inspection. (3) The vault-as-source-of-truth contract is
load-bearing — every artifact lives there, full stop.

### D. Sub-folder per content type (`_inbox/pdfs/`, `_inbox/photos/`)

Rejected for now. Adds bridge-side classification (which extension
goes where), which is exactly the responsibility we want to keep with
the LLM. A flat `_inbox/raw/<date>/` is sufficient; if directory
sprawl becomes a problem, the LLM can move files post-classification.

## Consequences

### Positive

- Telegram remains the single input surface. The user's workflow is
  unchanged from their perspective: send file, get reply.
- The path pattern (`_inbox/raw/<date>/<time>-<name>`) is greppable,
  date-ordered, and sortable.
- Future modalities (voice notes, video) follow the same pattern:
  add a filter, route through `make_attachment_handler`, no further
  kernel surface changes needed.
- Idempotency is "free" — derived from the deterministic path, not
  from a separate dedup table.

### Negative

- The kernel surface grew. `handle_turn` has a new kwarg; the bridge
  has a new public type (`AttachmentReplyFn`), a new handler factory
  (`make_attachment_handler`), and a new `build_application` parameter
  pair. Future kernel changes inherit this complexity.
- `_inbox/raw/` will accumulate files indefinitely. A future garbage-
  collection policy will be needed (out of scope for this slice).
- The user's caption is the only signal the LLM has about user intent
  for the attachment. If the caption is empty, the LLM must ask. The
  follow-up "Slice 2" issue will add system-prompt rules for the
  empty-caption case.

### Neutral

- Audit log machinery is unchanged. The LLM's `Read` of the saved
  file shows up as a normal `tool_use` event in `_audit/<date>.jsonl`
  and the markdown mirror — no special handling needed.
- The existing `--allowed-tools` set already includes `Read`; no
  permission change is required for PDF / image reading.

## Implementation notes

- `agent/telegram_bridge.py`:
  - New: `AttachmentReplyFn` type alias.
  - New: `_sanitize_filename`, `_inbox_landing_path`,
    `_pick_attachment_source`, `make_attachment_handler`,
    `_build_default_attachment_reply_fn`, `_resolve_vault_root`.
  - Changed: `build_application` and `run_polling_loop` accept optional
    `attachment_reply_fn` + `vault_root`. Passing only one of the two
    raises `ValueError`.
- `agent/runner.py`:
  - New: `_compose_user_text(caption, attachments, vault_root)`.
  - Changed: `handle_turn` accepts `attachments: Sequence[Path] = ()`.
    Default value preserves text-only behavior exactly.
- Tests: 15 new tests across `tests/test_telegram_bridge.py` and
  `tests/test_runner.py`. Existing 47 tests unchanged and still pass.
