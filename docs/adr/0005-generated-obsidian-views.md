# ADR 0005: Generated Obsidian views + wiki interlinking

**Status:** Accepted
**Date:** 2026-05-15
**Issue:** [#11](https://github.com/chijunzheng/personal-assistant-agentic/issues/11)

## Context

The vault is Jason's second brain, read primarily through Obsidian. But
half of it is invisible there. `journal/` is native markdown — it renders,
links, and shows up in Obsidian's graph. The structured domains do not:
`finance/transactions.jsonl`, `inventory/state.yaml`, and the
`fitness/{workouts,metrics,meals}.jsonl` files this repo plans to add are
canonical data formats that Obsidian cannot display. Jason can ask the
bot "how much did I spend on groceries?" over Telegram, but he cannot
*browse* his own finance data in the tool he uses for everything else.

The earlier "Reminders — keep the Obsidian view in sync" section of
`prompts/system.md` already solved this for one domain. `reminders.jsonl`
stays canonical (append-only, sha256 ids); a `reminders.md` checklist is
regenerated on every write and never read back. It works. But it is a
**one-off**: the section names the reminder domain, the rule lives only
inside that section, and nothing about the prompt convention says the
next structured domain (finance, fitness, inventory) has to do the same.

Two problems with leaving it there:

1. **The projection gap will keep reopening.** Every new structured
   domain starts life invisible in Obsidian. "Add a view" becomes
   something the prompt has to be re-taught, every time. A convention
   that is restated per-domain is a convention that drifts.

2. **There is no wiki.** Obsidian's graph view, backlink pane, and
   unresolved-link pane all rely on `[[wikilinks]]` between notes. The
   structured domains produce no markdown, so they produce no links,
   so they are absent from the graph entirely. The journal becomes a
   loose pile of notes, not a wiki, because nothing connects them.

The user's ask, distilled: *every structured-storage domain should be
viewable in Obsidian, and the vault should read like an interlinked wiki,
not a pile of disconnected files.*

There is real tension with `CLAUDE.md`'s rule against writing markdown
for structured data. This ADR does not contradict it — it sharpens it.
The rule is about **what you query against**, not about file extensions.
Canonical data stays `.jsonl`/`.yaml`; Glob/Grep against the structured
files is still the only query path. The generated `.md` is a *projection,
never read back* — the same category as a digest. What makes the
distinction safe in this repo (which has no kernel to enforce a
`generated_by:` frontmatter check) is that the contract is stated in
explicit prompt wording: "never read the generated `.md` back as a query
source." The LLM, not a kernel hook, is what keeps the two file classes
separate.

### Prior context

The legacy deterministic repo settled the same design tension in
`../personal-assistant/docs/decisions/2026-05-14-markdown-views-primitive.md`.
That ADR introduced a *kernel primitive*: domains declare a `view:` block
in `domain.yaml`, and `Orchestrator._render_domain_view` calls the
plugin-owned renderer after every write. The design rationale (monthly
rollups for append-logs, single-file for live-state, Phase-1 structural
links only, semantic linking deferred) carries over. The *mechanism* does
not, because this repo has no kernel, no `domains/`, no `domain.yaml`,
no orchestrator hook. Per the agentic `CLAUDE.md`'s cardinal rule — *"the
agent decides"* — the only place to put a cross-domain convention is
`prompts/system.md`, enforced by the LLM via its tool calls. This ADR
records that recast.

## Decision

PR #16 landed the convention in `prompts/system.md` (new section
"Generated Obsidian views" plus the finance instance). The five
decisions that section codifies, all settled in the prior design pass:

1. **One generic convention, not per-domain sections.** The rule is
   stated once in a "Generated Obsidian views" section that names *no*
   specific domain. It applies to any structured-storage domain — every
   `.jsonl` event log and every `.yaml` state file — by definition. The
   existing reminder section stays as the worked example and concrete
   shape (Open / Done / Cancelled checklist), but it no longer carries
   the generic rule on its own. New structured domains (fitness,
   inventory, anything later) inherit the contract without further
   prompt edits.

2. **Append-logs → monthly rollups; live-state → single file.**
   Per-domain granularity, stated in a compact table:

   - `finance/transactions.jsonl` → `finance/YYYY-MM.md`
   - `fitness/workouts.jsonl` → `fitness/workouts-YYYY-MM.md`
   - `fitness/metrics.jsonl` → `fitness/metrics-YYYY-MM.md`
   - `fitness/meals.jsonl` → `fitness/meals-YYYY-MM.md`
   - `inventory/events.jsonl` + `inventory/state.yaml` → `inventory/state.md`
   - `reminder/reminders.jsonl` → `reminder/reminders.md`

   Monthly rollups for append-logs because dated month files are the
   natural wiki nodes (prev/next links graph cleanly) and they keep
   file counts sane — finance's eventual ~1,200 transactions become
   ~3 monthly notes, not 1,200. Live-state files have exactly one
   "current" view, so they project to a single file.

3. **Regenerate only the month-file(s) the current turn's write
   touched.** If a finance ingestion turn appends rows dated
   `2026-04-12` and `2026-04-30`, the turn regenerates only
   `finance/2026-04.md`. If rows span two months (`2026-04-30` and
   `2026-05-01`), both month files are regenerated. The full history
   is *never* re-emitted. This is the agentic-specific bound on tool
   cost: every regeneration is one `Read` of the canonical `.jsonl` and
   one `Write` of the touched month, and the cost is `O(months touched
   this turn)`, not `O(months in history)`. Single-file projections
   (`inventory/state.md`, `reminder/reminders.md`) regenerate fully
   every time, because there is only one file.

4. **Generated views are never read back for queries — canonical
   `.jsonl`/`.yaml` is always the query target.** This is the rule
   that lets the projection coexist with `CLAUDE.md` pitfall #7
   ("markdown is not a canonical storage format for transactions").
   In the kernel repo the rule was machine-checkable via a
   `generated_by:` frontmatter marker that the kernel honored in
   `retrieval.py` and `index.py`. **This repo has no kernel** — there
   is no module to consult a marker, no central read path to filter.
   The rule is therefore stated in explicit prompt wording: queries
   about transactions, workouts, or inventory always Glob/Grep the
   canonical `.jsonl`/`.yaml`; the generated `.md` is "not a cache,
   not an index, not a query target." The "do not edit" header on
   every projection is the human-readable signal of the same contract.
   The convention is enforced by the LLM following its instructions,
   not by a code check.

5. **Phase-1 structural `[[wikilinks]]` only.** Every generated view
   emits three classes of deterministic, computed-from-dates links:

   - **Prev/next same-domain month** — `finance/2026-04.md` links
     `[[finance/2026-03]]` and `[[finance/2026-05]]`.
   - **Cross-domain same-month rollups** — `finance/2026-04.md` also
     links `[[fitness/workouts-2026-04]]`,
     `[[fitness/metrics-2026-04]]`, `[[fitness/meals-2026-04]]`,
     `[[inventory/state]]`. All of them, even if the target files do
     not yet exist (Obsidian shows them as unresolved and resolves
     them the moment the matching slice lands).
   - **Same-period journal links** — `finance/2026-04.md` lists the
     journal files in that month, found via
     `Glob 'journal/2026-04-*.md'`. Days without a journal file are
     omitted; if there are none, the `Journal:` line is omitted
     entirely.

   These are deterministic — computed from the month string and from
   `Glob` results — so the LLM produces the same set of links every
   time for the same month, with no creativity. They light up
   Obsidian's graph the moment the convention ships.

   **Semantic journal interlinking** — auto-populating the journal
   handler's `links: []` frontmatter with topically related notes,
   or clustering finance entries by merchant — is **explicitly out of
   scope here**. It is LLM-dependent (not deterministic), it would
   mutate user-authored narrative files (hitting the 30-min user-edit
   buffer invariant), and it deserves its own eval. Deferred to a
   future Phase-2 ADR.

### Failure contract

Stated once in the section and not duplicated: if the canonical append
succeeds but the projection `Write` throws, the turn is still a success.
The canonical write stands. The LLM mentions the failure briefly in its
reply and continues. The canonical source is truth; the view is
recoverable on the next write. (Same contract reminder has used
successfully since its introduction.)

## Consequences

### Positive

- Every structured-storage domain becomes browsable in Obsidian
  immediately — finance via PR #16, and fitness/inventory the moment
  their slices land. No further prompt edits per domain, only the new
  row in the per-domain table.
- The vault starts to read like a wiki. Prev/next, cross-domain same-
  month, and same-period journal links connect what used to be six
  disconnected directories. Obsidian's graph view becomes useful for
  the first time on the structured half of the vault.
- Per-turn tool cost is bounded by the number of months touched, not
  by history size. Finance's monthly rollup pattern scales: a turn
  that appends 50 rows across two months does two `Read`s and two
  `Write`s for projections, period.
- The convention is one section. New structured domains inherit it for
  free: add an entry to the per-domain table and write the canonical
  shape, and the projection rules, failure contract, and structural
  links all apply automatically.

### Negative / things to watch

- **Enforcement is by prompt, not by code.** This is the agentic-model
  cost: there is no kernel hook that fires `_render_domain_view` after
  a write, and no `generated_by:` frontmatter check that keeps a
  generated file out of a retrieval path. If the LLM drifts on the
  "never read back" rule, queries can silently degrade (e.g. reading a
  stale month rollup instead of `.jsonl`). The mitigation is the
  explicit prompt wording plus the "do not edit" header on every
  projection; the failure mode is observable in the audit log (a
  `Read` of `finance/2026-04.md` for a transaction query is the flag).
- **Eval gate is aspirational.** The kernel-repo ADR required a
  head-to-head eval against `configs/baseline.yaml` before promotion.
  **This repo has no `eval/` directory** — `eval/run.py`,
  `configs/default.yaml`, and `configs/baseline.yaml` do not exist
  (and per `CLAUDE.md` are not planned). The practical gate is a
  manual representative-turn smoke test: a finance statement
  ingestion turn must produce `finance/<YYYY-MM>.md` with the
  expected header, monthly rows, and structural links. Acceptance
  criterion #8 on issue #11 names this gate explicitly.
- **Cross-domain links produce unresolved-link noise until sibling
  slices land.** Right now `finance/2026-04.md` emits
  `[[fitness/workouts-2026-04]]`, `[[inventory/state]]`, etc., and
  those files do not yet exist. Obsidian shows them as unresolved
  (faded) links. Accepted: it is the cheapest way to make the graph
  "self-heal" the moment those slices land, and unresolved links are
  the normal Obsidian failure mode (not a hard error).

### Neutral / out-of-scope

- **This ADR is not strictly required by `CLAUDE.md`.** The agentic
  `CLAUDE.md` requires an ADR before *kernel* changes (new input
  modality, new tool exposure, audit schema migration, etc.).
  `prompts/system.md` is the LLM's contract — a "public API" per
  `CLAUDE.md`'s file ownership table — but it is not the kernel. The
  ADR-before-kernel-change gate therefore does not strictly require
  this document. This ADR exists for design traceability and parity
  with the legacy-architecture ADR, so the *why* of the convention
  is recorded next to the *what* in `prompts/system.md`.
- **The reminder section is unchanged.** PR #16 left
  "Reminders — keep the Obsidian view in sync" intact as the concrete
  shape for that domain. Folding it into the generic section (so the
  generic rule and the reminder example sit together) is a separate
  follow-up slice (issue #14), running in parallel with this ADR. The
  outcome of that slice does not affect any decision recorded here.
- **No code in this repo changes.** Neither this ADR nor PR #16 edits
  `agent/runner.py`, `agent/vault.py`, or any other Python module.
  The existing test suite is unaffected; `pytest` stays green.
- **Phase-2 (semantic interlinking) gets its own ADR.** When journal
  notes start carrying topical `[[wikilinks]]` to one another and to
  the structured projections, that work is its own decision — with
  its own LLM-driven concerns, mtime-buffer interactions, and (by
  then, ideally) eval coverage.
