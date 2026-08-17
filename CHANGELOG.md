# Changelog

All notable changes to this repository are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This is a deploy-by-copy skills library (each skill is copied to
`~/.claude/skills/<name>/`) rather than a versioned package, so releases are
grouped by **date** instead of strict [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **`review-loop` gains `--mode deliverable` — a review of finished work for
  whether its reader can use it.** Every lens in the loop checked an artifact
  against its specification; nothing checked it against the person it was handed
  to. The one lens that exists for that question, `operator-empathy`, opened with
  "running … in `--mode plan`" and so only ever saw forward-looking plans. It
  could catch "this plan adds a dashboard that increases your workload" and never
  "this finished page is unusable."

  The lens now carries two scopes. Scope A (plan) is unchanged, byte-for-byte
  apart from its heading level. Scope B (deliverable) assigns the artifact a
  genre — `report` / `reference` / `decision-ask` / `narrative` /
  `machine-output` — and runs an eleven-item checklist against it: answer-first,
  inventories rendered as prose instead of tables, lists carrying reasoning,
  heading shape versus genre, a decision that links to its evidence instead of
  carrying it, a menu where a recommendation belongs, terms used before they are
  introduced, code detail in the reading path, density, rigor apparatus in the
  reading path, and edit-meta.

  Three mechanics keep it quiet, and all three came from exercising it rather
  than from theory — a first cut fired on every control document tried, with
  true-but-minor findings that a reader could not tell apart from a page nobody
  can use. First, the genre gate: a `reference` page is *supposed* to be dense
  with tables, so the density and list checks do not apply to one. Second,
  severity is blast radius rather than truthfulness, and the ratio has to appear
  in the finding — "6 inventories, 0 tables" is high, "2 of 9 headings" is low —
  with an all-low result returning `[]`. Third, the inventory check only fires
  when the reader has to **join material that is not adjacent**; an inventory
  already sitting in one place, in order, is not a finding, because a table
  being tidier is not a defect. Severity and `load_bearing` are also coupled
  now, since the loop only fixes findings that are both ≥medium and
  load-bearing — a true finding marked otherwise is one nobody acts on.

  The checklist is **embedded, not read at runtime**. This skill installs on
  machines with no operator instruction files at all, where a runtime-loaded
  checklist would load nothing and the lens would return `[]` — indistinguishable
  from a clean deliverable. A project can still point the lens at its own
  standard via `.claude/review-loop.deliverable-standard`, whose rules become
  additional checks and win on conflict; if that declared standard fails to load,
  the lens must emit a `checklist-unavailable` finding rather than return `[]`.

  The Stop hook auto-selects the new mode, with precedence **plan > code >
  deliverable**: `.md`/`.mdx` under `reports/`, `research/`, `content/` or
  `src/content/`, plus `**/*-report.md`, `**/*-brief.md`, `**/*-summary.md`,
  overridable per-project at `.claude/review-loop.deliverable-paths`. Those files
  previously matched nothing and exited at the nothing-reviewable gate, so this
  branch only *adds* review — a mixed code-and-deliverable diff still routes to
  code, and the deliverable rides along unreviewed until someone invokes the mode
  by hand. Anything under `.claude/` is excluded structurally, so a `session-end`
  handoff named `…_thing-summary.md` cannot fire a review loop.

- **`sync.py --capture` now refuses to pull private content into the repo.**
  Capture is the one direction that can leak: it copies the live tree into a
  public repo, and the live tree accumulates operator-private detail (absolute
  home paths, emails, session ids, client codenames) that the published copy
  deliberately generalizes. The existing `cruft_scan` only *warned*, and only
  *after* the writes had landed, so by the time it spoke the private content was
  already in the working tree.

  Lines that capture would ADD are now scanned **before anything is written**, and
  the whole skill is refused on a match. Generic shapes are built in; literal names
  live in a gitignored `.capture-private-terms` at the repo root (one per line,
  case-insensitive), because listing them in tracked source would publish the very
  names the gate withholds. A term already present in the repo's copy of a file is
  sanctioned, so a skill that legitimately names the terms it redacts is unaffected.

  Checked against the live tree: 9 of 26 drifting files would have leaked, across
  6 skills. All 6 are now refused.

- **Captured live-only improvements that existed on one machine only** —
  `review-loop` gains its "surface the shipping gap" and "reconcile the session's
  handoff" sections (72 lines the repo never had, both load-bearing), and
  `session-end` gains the handoff provenance-marker stamp that `/review-loop`'s
  reconciliation step keys on. `session-pickup`, `seo-index-validation` and
  `session-end` also pick up tightened descriptions. Captured file-by-file rather
  than by `--capture`, because the same skills carry redactions that must not
  travel back.

- **`register_finding.py` warns when the row it just wrote is uncommitted.** The
  registry is a plain file in the project's repo. If git tracks it, a freshly
  appended finding lives only in the working tree, and a `git restore`, branch
  switch, or stash discards it with no message. The script now prints a
  ready-to-run `git add && git commit` naming the new finding_id.

  Observed twice inside one 13-hour window, in a repo running a dozen-plus
  concurrent agent worktrees: rows were appended, then the file reverted to its
  committed state. Backups rotate at 3, so the evidence window is about three
  appends.

  **Correction to how this shipped.** The original entry claimed those two
  retrospectives "lost every finding they registered". That was wrong, and it
  was an inference stated as a measurement. The findings were already committed
  to the project's `main` in an earlier PR; the checkout being appended to was on
  a branch that predated it, so what got reverted were duplicate re-registrations,
  not unique records. The *mechanism* is real and demonstrable (append to a
  tracked file, `git restore`, the row is gone), which is what this warning
  addresses. The incident was not data loss. Diagnosing it from one branch's `git
  log` without checking `origin/main` is exactly the failure the `[derived]` tag
  added in the previous release exists to flag.

  It is advice, not a gate: the exit code is unchanged, and the check is
  fail-quiet (no git, untracked file, or any error → silent).

### Fixed

- **`session-end` wrote its handoff into a directory that gets deleted.** Step 4
  resolved `<project-root>` the obvious way (`git rev-parse --show-toplevel` / the
  cwd). Inside a linked git worktree that is the *worktree's* root, and worktrees
  are routinely removed when the task that created them ends — so the single
  artifact the skill exists to produce was the one thing guaranteed not to
  survive, while the session reported success. The destination is now resolved
  with `git worktree list --porcelain | head -1`, which is the primary checkout
  in a worktree and the same checkout in an ordinary clone, so it runs
  unconditionally. The skill now also states the absolute path it wrote to.

  The old "never commit" instruction sat oddly next to repos whose handoff
  history *is* tracked. Resolved in favour of location, not commits: the primary
  checkout already outlives worktree cleanup, so the read-only gate stands
  unchanged. Whether handoffs are tracked is left to the project, which genuinely
  varies — some repos commit them, others gitignore `.claude/` wholesale.

  `session-pickup` and `review-loop`'s handoff-reconcile step read that same
  directory and are updated to resolve it identically; otherwise a pickup or a
  reconcile running in a worktree would look in an empty directory and conclude
  no handoff existed. `review-loop`'s attribution safety is unaffected — it keys
  on the session-id provenance marker, never on recency, which is what makes a
  shared handoff directory safe for concurrent sessions.

  Because that directory is now genuinely shared across worktrees, `session-end`
  additionally stamps `<!-- session-end:origin branch=… worktree=… -->` into each
  handoff, and `session-pickup` selects on that stamp instead of on recency alone.
  Without it, ending two concurrent sessions minutes apart would leave pickup
  resuming whichever finished last rather than the branch in front of it — a
  silent wrong start rather than a visible error. Where the stamp is missing,
  ambiguous, or disagrees with the current branch, pickup now lists the candidates
  and asks instead of guessing. The `worktree=` value is a directory basename and
  never an absolute path: handoffs are durable and often committed, and an
  absolute path would carry the operator's home directory and username into
  shared history.

  Handoff filenames are also collision-proofed, for the same reason: a shared
  directory means two concurrent sessions can land on one path, and a silent
  overwrite loses a handoff just as thoroughly as worktree cleanup does. The
  timestamp is now specified to the second (existing handoff history mixes
  second, minute, and date-only resolutions), and every filename carries a unique
  discriminator from the start rather than gaining one once a clash is noticed.
  Checking for the file and only then adding a suffix does not close the race:
  both sessions can look, both can see nothing, and both can write the same path.
  The discriminator is a fresh random token generated per write, not the session
  id, which is constant within a session and so would rebuild the same path if
  `session-end` ran twice in one second. The no-clobber check is kept as a
  backstop: the token makes a collision improbable, and refusing to overwrite
  keeps an improbable one from costing a handoff.

- **`session-end` under-reported a session whose work landed through merged PRs.**
  Step 1 gathered evidence from `git status --short` and `git diff --stat`, both
  empty by construction once the work has merged, so the artifact list came out
  blank and a productive shift was recorded as an empty one. The skill now always
  looks for work that already landed (commits in the session window, the files
  they touched, merged PRs, and output living outside the checkout), rather than
  gating that search on the working tree being clean. Gating on cleanliness would
  have left the same hole half-open: merge a PR, leave one unrelated stray file,
  and the status probe is non-empty, so a clean-tree-only check never fires and
  the handoff keeps the stray file while dropping the merged work. If nothing
  turns up, the skill must say so as a stated finding instead of omitting the
  section silently.

  PR discovery is filtered by author and date and sets `--limit` explicitly
  (`gh pr list` defaults to 30 and truncates silently), and the skill is explicit
  that filtering is necessary but not sufficient: concurrent agent sessions share
  one account, so each candidate still has to be correlated with the session
  before it is claimed as this shift's work.

### Added

- **`session-end` can honour a project-declared close-out contract.** A project
  whose sessions acquire state at start — a claim on a shared desk or role, a
  lock, a lease — had no way to release it at close-out: the skill's safety gate
  forbids editing any file but the handoff, so a shift that ended cleanly could
  still leave a claim naming a session that no longer exists, with a symptom that
  points nowhere near the cause. If `<primary-checkout>/.claude/session-close-out.md`
  exists, the skill now reads and follows it, and the writes that file names are a
  declared, scoped exception to the read-only gate. Absent the file the step is
  skipped entirely — the skill never invents close-out actions.

  When the contract cannot be completed, the skill must name the state left held,
  where it lives, and how to clear it, in both the handoff and its closing
  message. Silent half-completion is the failure this exists to prevent: it leaves
  the project believing close-out ran. No project-specific logic lives in the
  skill; the contract file is the whole interface.

  Because this step necessarily runs after the handoff is written, a contract that
  succeeds mutates state the handoff has already described, leaving the file it
  touched out of the artifact list and any "claim is held" line false. The skill
  therefore amends the handoff after a successful close-out too, not only after a
  failed one. Otherwise the skill would produce exactly what it exists to prevent:
  a record that reads as current while describing state that no longer exists.

## [2026-07-27] — Record-keeping fixes: ledger closure, confidence gate, handoff provenance

Two independent defects in how these skills keep records. Both let a record
misrepresent itself to whoever read it next.

### Fixed

- **`pattern-retrospective` — a finding could never be closed.** The registry is
  append-only, so a resolved finding is retracted by appending a successor row
  carrying `supersedes: <old-id>`; nothing ever updated the superseded row, which
  kept `follow_up_status: pending` forever. `follow_up_check.py` reported resolved
  work as permanently past-due (one real finding showed as 27 days overdue three
  weeks after it was resolved), and a report that flags finished work as late is one
  the reader learns to skip. Closure is now **derived at read time**: a row that
  another row *in the same registry* supersedes is treated exactly as
  `follow_up_status: superseded`. Chose the reader-side fix over writing back to the
  superseded row because the registry's append-only invariant forbids editing a row
  in place — and a derivation also closes rows that were already written.
- **`pattern-retrospective` — `--confidence` could contradict its own evidence.**
  The counts and the confidence live in the same row, so a hand-picked number
  silently overstated the evidence. `--confidence` is now optional (computed from
  `supporting / (supporting + contradicting + 2)`, the skill's own §7 formula) and a
  supplied value that disagrees with the counts by more than 0.005 is refused with
  exit 5 and a message naming the expected value. Existing rows are never rewritten.
  Negative evidence counts are also refused, instead of reaching a division by zero.

### Added

- `follow_up_check.py --include-superseded` lists closed rows again, marked
  `(closed: superseded)`; `--format json` rows carry `closed_by_supersedes`. The
  default output **counts** what it hid in the summary line, so the suppression is
  visible rather than silent.
- `session-end` — a **provenance rule for load-bearing handoff claims**, with
  `[derived]` added to the existing `[verified]` / `[unverified]` / `[assumed]`
  vocabulary. `[derived]` is the tier that was missing: an inference is grounded
  enough to *feel* verified, so it gets written in the voice of a measurement. A real
  handoff asserted "the `review-pr` skill template is the source of that divergence"
  — an inference, and wrong; the divergence was a deliberate safety separation, and
  the next session nearly "repaired" it into letting an untested review auto-merge its
  own changes. The rule covers diagnoses and mechanisms, not just numbers, and applies
  hardest to the mid-flight block and the continuation prompt, where claims travel
  furthest from their evidence.
- `pattern-retrospective/tests/` — 64 tests over both registry fixes, wired into CI.

### Hardened

Found by adversarial review passes over the fixes above, and worth naming because
each one could hide an open finding or take down the whole report. **Hiding an open
finding is the worst thing this reader can do**, so every one of these now fails open
and warns on stderr rather than dropping a row quietly:

- **An unrecognized `follow_up_status` made a genuinely open finding vanish** with no
  warning and no count. The keep-gate was an allow-list of `pending` / `in-progress`,
  so a typo (`in_progress`), a different case (`Pending`), an empty value, a missing
  key, or a hand-edited non-string all dropped the row. It is now a deny-list: only
  the four terminal statuses close a finding, and **unknown means open**. The
  non-string case was a regression introduced by the field-coercion below, which
  turned a loud crash into a silent hide.
- **Two findings that supersede each other closed each other,** so both vanished and
  no successor survived. The self-reference guard only caught 1-cycles. Cycles of any
  length are now detected; their links are dropped so nothing is hidden.
- **A UTF-8 BOM dropped the registry's first row** (and made `register_finding.py`
  refuse every future append as corruption). `U+FEFF` is not whitespace, so `strip()`
  never removed it. All three readers now open with `utf-8-sig`. Windows PowerShell
  5.1 writes UTF-8 with a BOM, so this was reachable by ordinary local editing.
- **`repeat_detector.py` was left out of the hardening** while the other two readers
  got it, so the same corrupt row still killed it — and its crash exit code (1) is
  its own documented "REVIEW / candidate" verdict, making a crash indistinguishable
  from a result. It now shares the same warn-and-continue behavior.
- **A supplied `--confidence` that passed the check was stored verbatim,** so a
  hand-picked 0.755 landed beside counts worth 0.75, at a precision the formula never
  produces. `--confidence` is now purely an assertion to check; the derived value is
  what gets stored.
- Closure now requires **both** ends of a `supersedes` link to be well-formed
  `YYYY-MM-DD-NNN` ids. A corrupt or hand-written row previously hid a genuinely open
  finding on the strength of a `supersedes` key alone, and one with a non-string
  `finding_id` crashed the report outright. Rejected links warn on stderr.
- `follow_up_check.py` tolerates a malformed **field** the way it already tolerated a
  malformed **line** — a hand-edited `"follow_up_status": 123` used to raise
  `AttributeError` and take down the whole report (this predates the change above).
- `--confidence nan` is refused. Every comparison against NaN is false, so it slipped
  past both the new gate and the schema's min/max, then serialized as bare `NaN`,
  which is not valid JSON.
- Duplicate registry paths (`--registries a.jsonl,./a.jsonl`) are read once instead
  of doubling every row and every count.
- All three registry readers catch `ValueError`, not just `json.JSONDecodeError`. An
  integer past CPython's 4300-digit conversion limit raises a plain `ValueError`, so
  one bad line escaped the handler — killing the whole report in `follow_up_check.py`,
  and replacing `register_finding.py`'s corruption message (which names the recovery
  script) with a traceback. Also predates this change.

### Changed

- `session-pickup` — never act on a `[derived]` claim without checking it first,
  above all when it would justify "repairing" something that may be deliberate.
- `weekly-work-log` — `[derived]` maps to `designed, not proven`, never `shipped`.
- `requirements-optional.txt` — declares `jsonschema`, which
  `register_finding.py` has always imported but which was never listed.

## [2026-07-11] — Cross-runtime skill sync (Claude Code + Codex)

Skills can now deploy to Claude Code, Codex, or both from one shared source. The
sync engine moves from live-authoritative to **repo-authoritative**.

### Added

- `targets:` frontmatter key — a skill declares `[claude]`, `[codex]`, or
  `[claude, codex]` (absent → `[claude]`, so every existing skill is unchanged).
- Per-target **overlays** (`<skill>/overlays/<target>/`, additive-only) so
  product-only files (e.g. Codex's `agents/openai.yaml`) install to that product and
  never leak into the other. A `{{SKILL_HOME}}` token expands to each target's install
  path, keeping a single shared `SKILL.md` product-correct.
- `scripts/sync.py` gains `--target claude|codex|both`; `--capture` requires an
  explicit target and refuses to silently resolve a divergence between two live
  copies. Homes are env-overridable (`CLAUDE_SKILLS_DIR` / `CODEX_SKILLS_DIR`).
- `monitor-agent-thread` — the first dual-target skill: safely watch a Claude Code or
  Codex session from the other product, with a projection that never exposes hidden
  reasoning, raw tool arguments, signatures, encrypted content, or secrets.
- Test suites for the sync engine, the monitor's privacy invariants (both
  directions), and the drift hook — all wired into CI.

### Changed

- The drift hook is target-aware (detects `.claude/skills` vs `.codex/skills` edits
  and emits the correctly-targeted capture command).
- Docs (`README`, `CONTRIBUTING`, `SKILL-SPEC`) document the canonical model, target
  declaration, overlays, the command surface, and the migration path.

## [2026-06-15] — Open-source preparation

First public-release pass: licensing, contributor docs, privacy/security
documentation, CI, and a portability fix so the skills run outside the
author's machine.

### Added

- `LICENSE` — MIT license for the repository.
- `SECURITY.md` — documents the privacy surface (skills that mine local Claude
  chat history), the fail-closed write guard, and how to report a
  vulnerability.
- `CONTRIBUTING.md` — outside-contributor workflow: edit the repo copy
  directly, set `CLAUDE_GLOBAL_SKILLS_REPO`, and test with
  `scripts/sync.py --deploy`.
- `SKILL-SPEC.md` — the `SKILL.md` contract (YAML frontmatter, `name` must
  equal the directory name) and directory conventions.
- `CODE_OF_CONDUCT.md` — community expectations.
- Continuous integration plus GitHub issue and pull-request templates.

### Changed

- Portability fix (backward-compatible): hardcoded author home paths now
  derive from `Path.home()` / `%USERPROFILE%`, so the skills resolve the
  correct location on any machine.
- Curated the skills into two tiers: **Core** (portable, drop-in) and
  **Personal-example** (wired to the author — adapt before use).

### Security

- `.gitignore` now also covers `.claude/` and `.pytest_cache/`, alongside the
  existing `**/.local-state/`, `.env`, `**/.env`, `__pycache__/`, and `*.pyc`
  entries, to keep local config and mined private data out of the repository.
