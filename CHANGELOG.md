# Changelog

All notable changes to this repository are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This is a deploy-by-copy skills library (each skill is copied to
`~/.claude/skills/<name>/`) rather than a versioned package, so releases are
grouped by **date** instead of strict [Semantic Versioning](https://semver.org/).

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
- `pattern-retrospective/tests/` — 45 tests over both registry fixes, wired into CI.

### Hardened

Found by an adversarial review pass over the fixes above, and worth naming because
each one could hide an open finding or take down the whole report:

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
- Both scripts catch `ValueError`, not just `json.JSONDecodeError`, when parsing a
  registry line. An integer past CPython's 4300-digit conversion limit raises a plain
  `ValueError`, so one bad line escaped the handler — killing the whole report in
  `follow_up_check.py`, and replacing `register_finding.py`'s corruption message (which
  names the recovery script) with a traceback. Also predates this change.

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
