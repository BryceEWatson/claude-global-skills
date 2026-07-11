# Cross-runtime skill sync — implementation plan

## In plain terms

This repository is a library of "skills" (small folders of instructions + scripts)
that get copied into a tool's global skills folder so you can invoke them in any
session. Until now the copy-engine (`scripts/sync.py`) only knew about **one**
destination: Claude Code's folder at `~/.claude/skills`. A second tool, **Codex**,
keeps its skills at `~/.codex/skills`. We want one skill folder in this repo to be
able to install into Claude Code, Codex, or both — without duplicating the shared
files and without shipping Codex-only files into Claude Code.

This plan makes the **repository the single source of truth**. Each skill declares
which tools ("targets") it supports. Files that are unique to one tool live in a
small per-tool **overlay** folder that is layered on top of the shared files at
install time. The engine can install ("deploy"), compare ("check"), or pull edits
back ("capture") for Claude Code, Codex, or both. The first skill to use all of
this is `monitor-agent-thread` — a tool that safely watches one product's session
from the other without leaking secrets or hidden reasoning.

**Source of truth:** the task brief in this session (the acceptance requirements),
plus the repo's own contracts: [`README.md`](../README.md),
[`CONTRIBUTING.md`](../CONTRIBUTING.md), [`SKILL-SPEC.md`](../SKILL-SPEC.md), and the
current [`scripts/sync.py`](../scripts/sync.py). This revision also folds in an
adversarial 4-lens review of the first draft (see "Design decisions from review").

## Glossary (defined on first use)

- **Target** — a destination product for a skill: `claude` (Claude Code,
  `~/.claude/skills`) or `codex` (Codex, `~/.codex/skills`).
- **Shared content** — the files of a skill that are identical for every target
  (the bulk: `SKILL.md`, `scripts/`, `references/`). Stored **once** in the repo.
- **Overlay** — a per-target folder (`<skill>/overlays/<target>/`) whose files are
  **added** to the shared content when deploying to that target. Overlays are
  **additive-only**: an overlay path must NOT shadow a shared-content path (this is
  validated). This is how Codex's `agents/openai.yaml` reaches Codex but never
  Claude Code.
- **Token substitution** — a tiny, documented set of `{{TOKEN}}` placeholders in
  shared text files that expand to a per-target value at deploy time, so one shared
  `SKILL.md` produces a product-correct command in each install.
- **Materialize (forward)** — compute the exact file set a target should receive =
  shared content + that target's overlay, with tokens expanded for that target.
- **Canonical / repo-authoritative** — the repo is the authority; live installs are
  materialized copies. (Previously the model was *live-authoritative*.)

## The model shift (canonical source)

Previous model (documented in `sync.py`): **LIVE-AUTHORITATIVE + CAPTURE** — edit
the live skill at `~/.claude/skills/<name>/`, then `--capture` it back to the repo.

New model: **REPO-AUTHORITATIVE (canonical) + materialized deploy**. The repo holds
shared content + overlays; `--deploy` materializes it to each target. `--capture` is
retained as a maintainer convenience for pulling live edits back, but is now
**target-explicit** and **refuses to silently resolve divergence**.

**Intended authoring path for portable (multi-target) skills:** edit the repo
(shared file or the target's overlay), then `--deploy`. `--capture` remains for
single-target skills and for pulling an occasional live edit back; the target-aware
drift hook reflects this.

## Target declaration

A new **optional top-level `targets:` key** in `SKILL.md` frontmatter:

```yaml
targets: [claude, codex]   # dual-target (flow list)
# targets:                 # block list also accepted
#   - claude
#   - codex
# targets: [claude]        # Claude-only  (also the default when the key is ABSENT)
```

- **Absent key → default `[claude]`** (silent). Every existing skill has no
  `targets` key, so they all remain Claude-only with zero edits (backward compat).
- **Present but malformed / unknown value → hard error** (non-zero exit), never a
  silent degrade to Claude-only. Allowed values: `claude`, `codex`. Only the leading
  fenced frontmatter block is parsed (SKILL.md bodies may contain `---` rules).

## Overlay + substitution mechanics

Repo layout for a dual-target skill (`monitor-agent-thread`):

```
monitor-agent-thread/
├── SKILL.md                     # shared; targets: [claude, codex]; uses {{SKILL_HOME}}
├── scripts/thread_watch.py      # shared (byte-identical for both)
├── references/surfaces.md       # shared
└── overlays/
    └── codex/
        └── agents/openai.yaml   # Codex-only  (never installed into Claude Code)
```

- **Deploy to claude** → `SKILL.md` (tokens→claude), `scripts/`, `references/`.
  **No `agents/` folder, no `overlays/` folder.**
- **Deploy to codex** → the same shared files (tokens→codex) **plus**
  `agents/openai.yaml` (from `overlays/codex/`). **No `overlays/` folder.**

The repo-internal `overlays/` directory is **excluded from the shared file set** and
is **never** written into any install.

**Token set (fixed, documented):**

| Token | claude expansion | codex expansion |
|---|---|---|
| `{{SKILL_HOME}}` | `$HOME/.claude/skills/<name>` | `$HOME/.codex/skills/<name>` |

`$HOME` stays literal so the emitted command runs in bash and PowerShell.
Substitution applies only to decodable UTF-8 text files (binaries are skipped). The
token addresses the skill's **install** path only — it does not touch product log
paths like `~/.claude/projects` or `~/.codex/sessions`, which stay literal in both
installs. **Invariant (asserted):** a shared file must not contain a literal string
equal to any target's `{{SKILL_HOME}}` expansion — the token is the only sanctioned
representation, which keeps capture's reverse pass unambiguous.

## Command surface (targets)

**Default target set** (for `--check` and `--deploy` with no `--target`) = **the
skill's declared targets whose live home directory already exists**. This is
symmetric across check and deploy, so `--deploy` then `--check` is round-trip
stable, and a machine with no `~/.codex/skills` is never touched or nagged about
Codex. An explicit `--target` **forces** that target (creating its home on deploy).

| Command | Default target set | Behavior |
|---|---|---|
| `sync.py --check [--target claude\|codex\|both] [--skill N]` | declared ∩ homes-that-exist | Read-only drift report: live vs **forward-materialized** expected output, per target, **LF-normalized**. Skips a declared target whose home is absent (informational note). Exit 3 on drift; `EXIT_ENV` only if no requested home exists. Live-only (unmanaged) skills are noted, never counted as drift. |
| `sync.py --deploy [--target claude\|codex\|both] [--skill N]` | declared ∩ homes-that-exist | Materialize repo → live for skills declaring the target. Preserves live `.local-state/`; only touches `<home>/<skill>/`, so sibling/unrelated installed skills and other product homes are untouched. Prints per-skill which targets it wrote and which declared targets it skipped (home absent → run explicit `--target`). |
| `sync.py --capture --target claude\|codex [--skill N]` | **required (single target)** | Reverse-map one target's live copy → repo. **Refuses** on divergence or unclassifiable files (below). Writes file-by-file (never wipes the skill dir), leaving every other target's overlay untouched. Never touches git. |

`--target both` is invalid for `--capture`. Backward compat: existing skills default
to `[claude]`, so on a Claude-only machine `--check`/`--deploy` behave as before, and
`--capture --target claude` replaces the old bare `--capture` (see Migration).

## Capture: reverse-classification + safety

For `--capture --target T`, each live file `rel` (after **reverse token
substitution**, LF-normalized) is classified:

1. `overlays/T/rel` exists in repo → write back to `overlays/T/rel` (a T-overlay file).
2. else `rel` exists in repo shared → write back to shared (`<skill>/rel`).
3. else (no counterpart in shared or `overlays/T`) → **UNCLASSIFIABLE → REFUSE**,
   print the path, instruct the operator to place it explicitly (shared vs
   `overlays/<target>/`) and re-run. Never guess (prevents a Codex-only file leaking
   into shared, or a shared file being siloed to one target).

Capture never deletes repo files that are absent from live (surfaces them as a note,
like the old engine). It writes only classified files, so `overlays/<other>/` and
unrelated repo content are untouched.

## Divergence guard (capture safety)

Because overlays are additive-only and `{{SKILL_HOME}}` is the only per-target
variance in shared files, the **shared region** of any target's live copy is
recoverable by reverse-substitution. For `--capture --target T` of a multi-target
skill, before writing:

1. Reverse-materialize `live[T]` → `shared_T` (shared region only, LF-normalized).
2. For every **other** declared target `T2` with a live copy: reverse-materialize
   `live[T2]` → `shared_T2`, and take `repo_shared_now` (LF-normalized).
   - `shared_T2 == repo_shared_now` → `T2` is merely stale/behind → safe.
   - `shared_T2 == shared_T` → both live copies agree in the shared region → safe.
   - **else → DIVERGENCE → REFUSE**: print the conflicting shared files; write
     nothing. A divergent live copy is **never** silently overwritten or merged.

All equality here uses the same **CRLF/LF normalization** (`_read_norm`) the engine
already uses for drift, so Windows autocrlf trees don't false-trigger.

## Privacy boundary (monitor-agent-thread) — preserve, do not weaken

`thread_watch.py`'s safe projection already excludes, and must continue to exclude,
**all six** surfaces — tests assert each, for **both** projection directions
(`claude_projection` and `codex_projection` both already exist; this is
preserve-behavior, not new logic):

| Surface | How it's excluded | Fixture in test |
|---|---|---|
| Hidden reasoning / thinking | only assistant `text` parts / `agent_message` emitted; `thinking`/`reasoning` items ignored | a thinking/reasoning block |
| Raw tool arguments / inputs | tool `name` only; `input`/`arguments` never read | a tool call carrying args |
| Signatures | thinking (which carries `signature`) is dropped whole; no field named `signature` is ever emitted | a signed thinking block |
| Encrypted content | `redacted_thinking`/`encrypted_content` items ignored | an encrypted-content field |
| Credentials / tokens / secrets | all emitted prose passes through `clip()` → `SECRET_PATTERNS` redaction | a planted API key |
| System / developer instructions | only `role == assistant` prose emitted; system/developer/user text ignored | a developer-role instruction |

Plus **false-blocker / stall discipline**: a stale alert (older than the last event)
is not counted as an active alert; a quiet-but-recent session (`age < stall_seconds`)
reports `active`, not `stalled`.

## In scope

1. Rewrite `scripts/sync.py`: loud `targets` frontmatter parsing (stdlib), overlay
   materialization (additive-only, `overlays/` excluded from shared), `{{SKILL_HOME}}`
   forward substitution + anchored reverse for capture, target-aware
   `--check`/`--deploy`/`--capture` with the declared∩exists default, the capture
   reverse-classification + refuse rule, and the divergence guard — all comparisons
   LF-normalized. Add `CLAUDE_SKILLS_DIR`/`CODEX_SKILLS_DIR` env seams (mirroring
   `CLAUDE_GLOBAL_SKILLS_REPO`) for hermetic tests. Stdlib-only, Windows-safe.
2. Import `monitor-agent-thread` into the repo: shared `SKILL.md`/`scripts/`/
   `references/`, `overlays/codex/agents/openai.yaml`, `targets: [claude, codex]`,
   `{{SKILL_HOME}}` tokenization of the two install-path references in SKILL.md.
3. Make `hooks/skills_drift_hook.py` target-aware: resolve target from whether the
   edit is under `.claude/skills` or `.codex/skills`; pass `--target <T>` to `--check`
   AND emit a runnable `--capture --target <T>` nudge (for a multi-target skill,
   recommend the repo-edit+redeploy path). Add a focused test.
4. New tests, wired into CI:
   - `scripts/tests/test_sync.py` — targets parsing (absent/flow/block/malformed/
     unknown), per-target + combined check/deploy, overlay-only-in-codex, no
     `overlays/`/`agents/` leak into claude, deployed skill reports in-sync (forward
     materialization), `.local-state` preserved, sibling/unrelated skills untouched,
     capture round-trip, capture refuse-on-unclassifiable, divergence refusal,
     declared∩exists default, backward-compat `[claude]` default.
   - `monitor-agent-thread/tests/test_thread_watch.py` — claude-from-codex and
     codex-from-claude projections; all six privacy surfaces × both directions;
     false-blocker (stale alert) and stall (recent-but-quiet) discipline.
5. **Verify-then-deploy** the real skill: after import, run `--check --target codex`
   and confirm the ONLY difference from the validated live Codex copy is the intended
   `targets:` metadata line (no other file added/removed/changed) — proving the repo
   faithfully reproduces the validated skill — THEN `--deploy --target both`. Confirm
   `~/.claude/skills/monitor-agent-thread` has **no** `agents/`/`overlays/`,
   `~/.codex/skills/monitor-agent-thread` has `agents/openai.yaml`,
   `~/.codex/skills/.system` and all unrelated skills are untouched, and
   `--check --target both` is clean.
6. Docs: README, CONTRIBUTING, SKILL-SPEC, CHANGELOG — canonical model, target
   declaration, additive overlays + substitution, command surface + defaults,
   capture/conflict behavior, how to add a portable skill, and the **Migration**
   section (below). This plan records the design rationale.

## Design decisions from review (first-draft fixes)

- Overlays are **additive-only** (dropped "replace a shared file"); a shadowing
  overlay path is a validation error → keeps the divergence guard well-defined.
- `--check` uses **forward** materialization (compare live to expanded expected), so
  no reverse-substitution is needed for the common path; capture's reverse pass is
  anchored to the exact expansion and guarded by the "no literal expansion in shared"
  assertion.
- **Symmetric default** (declared ∩ homes-that-exist) for check and deploy kills the
  phantom-drift loop the asymmetric draft created.
- **Verify-then-deploy** replaces the draft's deploy-then-check ordering so a lossy
  import can't clobber the validated Codex skill unnoticed.
- Every comparison is **LF-normalized**; test **home seams** added; frontmatter
  parse **fails loud**; privacy tests cover **all six** surfaces × both directions.

## Migration (from the Claude-only, live-authoritative workflow)

- **Nothing breaks for existing skills.** With `targets` absent, every skill defaults
  to `[claude]`; `--check`/`--deploy` on a Claude-only machine behave exactly as
  before and never touch `~/.codex`.
- **`--capture` now requires a target.** Replace bare `python scripts/sync.py --capture`
  with `python scripts/sync.py --capture --target claude`. The drift hook's nudge now
  emits the correctly-targeted command automatically.
- **Model is now repo-authoritative.** Prefer editing the repo + `--deploy`; capture
  is the pull-back convenience. For a portable skill, edit shared/overlay and
  redeploy rather than editing two live copies (which the divergence guard would flag).

## Out of scope / Deferred

- No symlinks/junctions; no routine two-way live↔live sync (forbidden).
- No behavior change to unrelated skills (only the mechanical `targets` default).
- No new third-party dependencies (stdlib/zero-dep rule holds).
- No redesign of `thread_watch.py`'s projection logic (import + test invariants only;
  both projection directions already exist).
- Replace-style overlays (an overlay shadowing a shared file) are deferred; only
  additive overlays are supported in this pass.

## Definition of done (testable)

- [ ] `python -m unittest discover -s scripts/tests -p 'test_*.py'` passes.
- [ ] `python -m unittest discover -s monitor-agent-thread/tests -p 'test_*.py'` passes.
- [ ] Existing tests still pass (node review-loop/thrash-detect; gemini-image unittests; krippendorff self-test).
- [ ] `py_compile` clean on all tracked `.py`.
- [ ] **Real app exercised:** verify-then-deploy per scope item 5; `--check --target both` clean;
      claude install has no `agents/`/`overlays/`; codex install has `agents/openai.yaml`;
      `~/.codex/skills/.system` + unrelated skills untouched; `thread_watch.py discover`/`snapshot`
      run against synthetic Claude and Codex logs and redact a planted secret.
- [ ] A deliberate divergence between two live copies makes `--capture` **refuse**; an
      unclassifiable live file makes `--capture` **refuse**.
- [ ] Docs match actual command behavior (verified by running the commands).
- [ ] PR opened against `main`; `/review-loop` run to `<promise>review-clean</promise>`
      with a commit-pinned verdict comment.

## PR target

Branch `claude/cross-runtime-skill-sync-8b3a73` (current worktree branch) → `main`.
