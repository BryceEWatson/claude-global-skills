---
name: review-globals-loop
description: >-
  Mine ALL of Bryce's local Claude history across EVERY project (both corpora —
  Claude Code CLI and Cowork local-agent-mode) for operator friction that recurs
  across MULTIPLE projects, then propose GLOBAL ~/.claude leverage (a CLAUDE.md
  rule, a new/edited global skill, a memory, or a settings hook) to fix it — each
  proposal reconciled against what already ships globally, self-validated by an
  adversarial /review-loop --mode claim loop, and recorded in a cross-run ledger
  so nothing is re-proposed, dismissed-twice, or shipped on its own say-so.
  DISTINCT FROM: signal-scan (one repo → that repo's .claude/ config),
  command-retro (Command's own logs → Command's queue), and transcript-analysis
  (ONE project, Cowork-only, proposes that project's CLAUDE.md candidates). This
  skill is the GLOBAL, cross-project layer: it only surfaces friction proven to
  recur across >=N DISTINCT projects (counted by cwd-PATH attribution only) and
  only ever proposes changes to ~/.claude. Invoke as /review-globals-loop.
  Triggers: "what global rule should I add", "what recurs across my projects",
  "fleet-wide friction", "improve my global Claude config from how I actually
  work".
allowed-tools: Read, Grep, Glob, Bash, Task
---

# /review-globals-loop

The **cross-project, global self-improvement pass.** It reads how you actually
work across your WHOLE fleet — every project, both local Claude corpora — finds
the friction that recurs in **two or more different projects**, and proposes the
smallest **global `~/.claude`** change that would fix it. It PROPOSES only; it
has **no write tools** and cannot edit `~/.claude` itself (see Apply boundary).

It is one member of a deliberately-partitioned family — know which one you want
before you run it. The differentiator is **scope + the cross-project recurrence
gate**, not "the others don't propose" (two of them do):

| Skill | Unit of work | Reads | Proposes changes to | Promotion bar |
|---|---|---|---|---|
| `/signal-scan <repo>` | ONE repo | that repo's CLI history | that **repo's** `.claude/` (proposes) | recurs within one repo |
| `/command-retro` | Command itself | Command's own logs | Command's work queue (proposes) | Command-only friction |
| `transcript-analysis` | ONE project | that project's **Cowork** transcripts | that **project's** `CLAUDE.md` (proposes per-project candidates) | recurs across that project's sessions |
| **`/review-globals-loop`** | the **whole fleet** | **ALL projects, BOTH corpora** | the **global `~/.claude/`** (proposes) | recurs across **>=N DISTINCT projects** (path-attributed) |
| `/chat-history-search` | read-only | anything | nothing (search/inventory) | n/a |

**Why review-globals-loop is not a duplicate of transcript-analysis** (the closest
sibling — it also mines transcripts and also proposes CLAUDE.md candidates):
- **Scope.** transcript-analysis is **single-project** (filters by
  `userSelectedFolders` / project keywords, defaults to the CWD) and primarily
  **Cowork**; review-globals-loop spans **ALL projects in the registry across BOTH
  corpora**.
- **Bar.** transcript-analysis surfaces what recurs within *one* project's
  sessions and proposes that **project's** `CLAUDE.md` entries; review-globals-loop
  promotes **only** friction that recurs across **>=N DISTINCT projects**
  (default 2), attributed by **cwd PATH** not keyword, and routes the fix to the
  **global** `~/.claude` surface.
- **Single-project leftovers belong elsewhere.** A cluster that is really
  per-project (its per-project candidate is transcript-analysis's job; its
  per-repo CLI signal is `/signal-scan`'s) is OUT OF SCOPE here. review-globals-loop
  drops it from the global set and names where it belongs.

**Hard boundary (the no-duplication contract):** anything that recurs in only
ONE project is OUT OF SCOPE here. This skill never writes a per-repo signal,
never edits a project's `.claude/`, and never proposes a `project-claude-md`
edit. It mines the fleet corpus itself for the cross-project layer and proposes
ONLY the cross-project survivors.

> **On reuse vs. independent mining (be honest about what's wired).** This skill
> does **not** read another tool's friction bytes. signal-scan's durable output
> is a TypeScript Signal store (`data/signals.json`) readable only inside the
> Command checkout via its TS `SignalStore`; its `frictions.json` is an
> operator-named, ephemeral CLI argument with no stable path. There is no
> discoverable per-repo `frictions.json` to consume, and this skill must run on a
> machine with **no Node/Command checkout**. So review-globals-loop **independently
> mines the fleet corpus** (Step 3) and is distinct from signal-scan **because of
> the >=N-distinct-project gate and the global `~/.claude` target**, not because
> it reuses signal-scan's output. If a fresh `signal-scan frictions.json` happens
> to be handed to it via `--frictions-in <path>`, it will fold those in as extra
> input — but it never depends on that file existing.

---

## What it produces

A **ranked list of `CrossProjectProposal`s**, each one:
- justified by friction observed in **>=N DISTINCT projects** (default `N = 2`,
  raise with `--min-projects`), where every counted project is **cwd-PATH
  attributed** (keyword-attributed projects never count toward the bar),
- routed to exactly one of **four global targets** (`global-claude-md`,
  `global-skill`, `memory`, `settings-hook`),
- carrying **exact patch text** (ready to paste, not a description),
- reconciled against what already ships in `~/.claude` (already-encoded → dropped),
- **self-validated by `/review-loop --mode claim`** (only proposals a lens
  demonstrably examined and could not break are eligible to ship),
- recorded in a **cross-run ledger** so it is never re-proposed blind, and tracked
  for **loop closure** (did the applied fix actually stop the friction?).

Applying a survivor is a **separate, confirmation-gated, operator-driven** step
that this skill cannot perform itself.

---

## Helpers (self-contained Python — stdlib + two pip deps; NO Command/chat-arch TS imported)

All under `<SKILL_ROOT>/` = `C:/Users/Bryce/.claude/skills/review-globals-loop/`.

| Helper | Role | When |
|---|---|---|
| `scripts/corpus_retrieve.py` | Cross-project corpus enumeration + false-positive filtering (both corpora); stamps per-turn `attribution` mode | Step 1 |
| `lib/recurrence_promote.py` | Cluster fleet frictions, count distinct **path-attributed** projects, apply the >=N promotion rule | Step 4 |
| `scripts/reconcile_global_config.py` | Read the global `~/.claude` surface; drop already-encoded candidates | Step 5 |
| `lib/claimpack.py` | Build the claims-pack + evidence-pack (with explicit join ids); render the **fail-closed** verdict table | Step 6 / Step 9b |
| `lib/ledger_store.py` | The cross-run state ledger (validate / upsert / dismiss / mark-recurrence / panel) | Steps 2, 7, 9 |
| `lib/apply_record.py` | Record an operator-confirmed apply (loop-closure bookkeeping) | Step 10 (manual) |

All helpers are invoked via **Bash** (`python <SKILL_ROOT>/...`). This skill has
**no Edit/Write tools** — every PRIVATE-CORPUS artifact is written by a helper into
`.local-state` (never by the agent into `~/.claude`). Several intermediate JSON
files, however, have **no emitting helper** and must be MATERIALIZED by the agent
itself via **Bash** (shell redirection, `python -c`, or a heredoc) into
`<SKILL_ROOT>/.local-state/runs/<id>/`: `fleet-frictions.json` (Step 3),
`proposals.json` (Step 6), the per-batch `claims-pack-b<b>.json` slices (Step 8),
`findings-captured.json` (Step 8), and any per-survivor proposal file fed to
`ledger_store.py upsert` (Step 9b). Writing these intermediate JSON files into the
skill's own `.local-state/` via Bash is **explicitly allowed** — `.local-state/`
is skill-owned scratch, not `~/.claude` config — and the no-Edit/Write boundary is
about never mutating `~/.claude`, not about never producing scratch JSON.

`scripts/corpus_retrieve.py` is **stdlib only** (`json os re sys glob argparse pathlib datetime`).
`lib/ledger_store.py` and `lib/apply_record.py` perform **real `jsonschema`
validation** of every proposal/record against the ledger schema (a malformed
proposal is rejected, not silently stored) and use `filelock` for atomic writes,
so they need `jsonschema` + `filelock` (`pip install --user jsonschema filelock`);
on a machine without those deps they **exit 3** (missing-dep) printing the install
line. The others are stdlib only.

> All helpers mirror the **approach** of `chat-arch`'s schema modules
> (`correction.ts`, `applied-improvement.ts`, `upgrade-outcome.ts`),
> `pattern-retrospective/lib/register_finding.py` (atomic tmp+rename, filelock,
> 3-backup rotation, schema validation, streaming reads, explicit exit codes),
> and Command's `retroCorpus.ts` / `transcriptStream.ts` / `chatlog.ts` /
> `scan-cowork-lam.ts` / `command-sessions.mjs`. They import **none** of that
> TypeScript — the skill must run with just `python` on a machine that has no
> Node/Command checkout. Canonical schema source to keep the Python shapes in
> sync with (copy shapes, do not import):
> `C:/Users/Bryce/Projects/chat-arch/packages/schema/src/{correction,applied-improvement,upgrade-outcome}.ts`.

---

## Where artifacts live (local-only; never a tracked/remoted repo)

Run artifacts contain **verbatim operator turns mined from the whole fleet** —
private cross-project chat. They MUST NOT land in a git-tracked, remoted repo.

- **Corpus + evidence + claims artifacts** (`turns.ndjson`, `sessions.ndjson`,
  `fleet-frictions.json`, `global-candidates.json`, `reconciled.json`,
  `proposals.json`, `claims-pack.json`, `evidence-pack.md`,
  `findings-captured.json`, `validation-verdict.md`) → written ONLY under
  `<SKILL_ROOT>/.local-state/runs/<id>/`. This directory is the skill's own,
  is not a git repo, and is never committed. (Verified: Command's
  `research/studies/` IS git-tracked with a GitHub remote and no `.gitignore`
  cover — so it is explicitly NOT used for these artifacts.)
- The Python helpers **refuse to write any corpus/evidence artifact to an unsafe
  path.** Both `scripts/corpus_retrieve.py` AND `lib/claimpack.py` route every
  output (every `--out` and every derived output file path) through the shared
  `lib/_guards.assert_safe_out`, which **fails closed (exit 7)**: it refuses any
  path inside `~/.claude` EXCEPT the skill's own `.local-state/`, refuses any path
  inside a git working tree, and refuses on any error. So a stray `--out` cannot
  leak private turns into a tracked repo or into `~/.claude` config.
- The **ledger** lives at `<SKILL_ROOT>/.local-state/proposals-ledger.json`
  (also skill-owned, never `~/.claude` config, never a project).

Local-only guarantee: nothing this skill writes is intended to be committed or
pushed; all of it stays under `<SKILL_ROOT>/.local-state/` on this machine.

---

## Argument parsing

Parse from the slash-command arguments (all optional, all have defaults):

- `--since <YYYY-MM-DD>` — corpus lower bound on session last-activity. Default: 90 days ago.
- `--min-projects <N>` — distinct-project promotion floor. Default `2`.
- `--registry <path>` — default `C:/Users/Bryce/Projects/Command/registry.json`.
- `--project <id>` — restrict the corpus pass to one project (debugging; the
  promotion rule then can't fire — used only to inspect attribution).
- `--max-iter <n>` — claim-loop iteration cap. Default `3`.
- `--threshold <f>` — cross-project lexical cluster threshold (**cosine**). Default `0.40`.
- `--batch <n>` — max proposals handed to one review-loop invocation. Default `3`.
- `--include-automated` — keep Command watcher/runner sessions (default: excluded).
- `--frictions-in <path>` — optional extra `SurfacedFriction[]` to fold in (e.g.
  a fresh `signal-scan frictions.json`); never required.
- `--dry-run` — run Steps 1–6 and print the would-be proposals; write nothing to the ledger.

---

## Workflow (end-to-end, ordered)

### Step 0 — Pick the study id and scratch dir

Choose `<id>` = `review-globals-loop-<UTC-yyyymmddThhmmZ>`. All run artifacts live under
`<SKILL_ROOT>/.local-state/runs/<id>/` (skill-owned, untracked — see "Where
artifacts live"). Never `~/.claude` config, never a project, never
`research/studies/`.

### Step 1 — Retrieve the cross-project corpus + filter false positives

This stage produces the clean, project-attributed turn corpus every later stage
consumes. It is the cross-project, **both-corpora** generalization of the
single-repo retrieval `signal-scan` runs: it mirrors `retroCorpus.ts`
(enumerate + cwd-attribute), `transcriptStream.ts` (stream genuine turns + the
per-turn time guard), `chatlog.ts` (`pathMatchesProject`, `cleanPromptText`,
`isAutomatedSummaryPrompt`), `scan-cowork-lam.ts` (Cowork audit/outputs line-shape
filter), and `command-sessions.mjs` (the `MACHINE_PREFIXES` automation classifier)
— **without importing that TypeScript.**

```bash
python <SKILL_ROOT>/scripts/corpus_retrieve.py --since <YYYY-MM-DD> \
  --registry C:/Users/Bryce/Projects/Command/registry.json \
  --sessions --stats --out <SKILL_ROOT>/.local-state/runs/<id>/sessions.ndjson
python <SKILL_ROOT>/scripts/corpus_retrieve.py --since <YYYY-MM-DD> \
  --registry C:/Users/Bryce/Projects/Command/registry.json \
  --turns --stats --out <SKILL_ROOT>/.local-state/runs/<id>/turns.ndjson
```

`--out` is **required**: the script writes its NDJSON to that path (there is no
stdout-as-data path; `--stats` still prints the summary to stderr). Before writing,
it routes the resolved `--out` through the shared `lib/_guards.assert_safe_out`,
which fails closed (exit 7): it refuses any path inside a git working tree, refuses
any path inside `~/.claude` EXCEPT the skill's own `.local-state/`, and refuses on
any error — so a stray `--out` can never leak private cross-project turns into a
tracked repo or into config. Keep `--out` under
`<SKILL_ROOT>/.local-state/runs/<id>/` as shown. The output file is opened
UTF-8 so verbatim turns with em-dashes / smart quotes / arrows are preserved.

**What it does (and why each rule exists):**

1. **Enumerates ALL projects, BOTH corpora, within `--since`.** Two completely
   separate trees; the Cowork tree is ~10x larger.
   - **CLI corpus:** `~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl`
     (skips `<sessionId>/subagents/agent-*.jsonl` — no operator prompts).
   - **Cowork corpus:** sidecar `claude-code-sessions/<org>/<user>/local_*.json`
     (carries `originCwd`/`cwd`, `sessionId`, `lastActivityAt`, `title`) paired
     with its transcript under `local-agent-mode-sessions/<org>/<user>/<sessionId>/`
     (`audit.jsonl` = primary prompt source for older sessions; `outputs/.../*.jsonl`
     `queue-operation enqueue` events = cleanest verbatim for newer ones). Walks
     BOTH the `local-agent-mode-sessions` and `claude-code-sessions` dir names.
2. **Attributes by PATH, not keyword.** The authoritative key is the in-line
   `cwd` (CLI) / `originCwd` (Cowork) field — **NOT** the encoded-cwd directory
   name, which is mixed-case (`C--` vs `c--`) and lossy. Match case-insensitively
   / trailing-slash-normalized against each registry project's `path` +
   `matchHints.pathAliases`; most-specific (longest) path wins. Keyword
   (`matchHints.keywords` / `coworkMarkers`) is a fallback used ONLY when **no**
   path matches AND **exactly one** project keyword-matches; 0 or >1 →
   `(unattributed)`, never a guess. **Each emitted turn carries an `attribution`
   field (`path` / `keyword` / `none`)** so downstream stages can count only
   path-attributed projects toward the promotion bar. (A gold corpus must contain
   sessions ACTUALLY run in a repo, not ones that merely mention it.)
3. **`--since` is a cheap session-level LOWER bound** on last-activity (`mtime`
   for CLI, `lastActivityAt` for Cowork). No session-level upper bound — a
   straddling session is still opened so its in-window turns count. Any precise
   upper cut is enforced at the TURN level on each turn's own ISO `timestamp`.
4. **Streams line-by-line; never loads a multi-MB transcript whole.** Files over
   a cap (25 MB) are recency-only. Garbage line → skipped; unreadable file →
   degrades to empty. Never throws.
5. **Extracts only genuine human/assistant turns**, each `{role, text, tsMs}`
   with `{projectId, attribution, source, sessionId, filePath}` provenance,
   cleaned via the `cleanPromptText` recipe.

**False positives it FILTERS** (all look like `type:"user"`/content but are NOT
genuine operator turns):

| Reject | How detected | Why |
|---|---|---|
| Command automation (watcher/runner) | FIRST genuine prompt matches a `MACHINE_PREFIXES` template OR the structural narrative discriminator (below) | Command's own backend invocations — the dominant noise class |
| `tool_result` content | user line whose content array is entirely `tool_result` blocks | Tool output, not typed input |
| `<task-notification>` wrapper | **RAW** text starts with `<task-notification` | Subagent completion notice |
| `<scheduled-task>` / cron | sidecar `title` or raw text starts with `<scheduled-task` | Cron session |
| `<local-command-stdout>` / `<command-message>` / `<system-reminder>` / `<untrusted…>` | raw text starts with the tag | Harness residue / injected context |
| `TodoWrite` tool_use | block `{type:"tool_use",name:"TodoWrite"}` | Assistant-written todos |
| `isMeta:true` user line | top-level `isMeta===true` | Slash-command expansion, not the operator typing |
| `attachment`/`system`/other META_TYPES | top-level `type` in the meta set | Meta/system, not a turn |
| `isSidechain` / `isApiErrorMessage` | top-level boolean true | Background op / API error |
| subagent transcripts | file under `subagents/agent-*.jsonl` | No operator prompts; whole file skipped |
| Cowork audit/outputs dupes | same prompt in `audit.jsonl` (2–3 events) AND outputs `enqueue` | Prefer outputs `enqueue`; fall back to audit only when no outputs file |

**Order matters:** the raw-text wrapper checks run **BEFORE** `cleanPromptText` —
cleaning strips the wrapper tags and would leave the inner notification text
("auto ping") looking like a genuine prompt.

**The Command-automation discriminator** (the load-bearing class): a session is
automation-classified (excluded unless `--include-automated`) when its FIRST
genuine prompt is a Command backend template. BOTH a prefix list and a structural
check, so a one-off rename doesn't leak noise:
- **Prefix templates:** `Project: `, `Facts (the only ground truth):`,
  `Action to perform:`, `Allowed actions:`, `Action just completed:`,
  `<untrusted`, `Here is the whole fleet's state`, `You are ranking`,
  `Write one sentence instructing Claude Code`, `The operator gave this feedback`,
  `Research the following (read-only)`, `Invoke the `.
- **Structural narrative discriminator:** cleaned first prompt matches
  `^Project: .+? \([a-z]+\)` AND contains `Sessions: \d+ recorded` OR
  `Recent events (newest first):`. The header PLUS a second anchor keeps a
  genuine "Project: …" prompt from being misclassified. This matters
  fleet-wide: the narrative prompt NAMES the summarized project, so without it
  those sessions keyword-attribute onto dormant projects and inflate them.

**Read the `--stats` summary.** It reports, per corpus: files scanned, sessions
kept, sessions dropped-as-automation, sessions unattributed, sessions
keyword-attributed (NOT counted toward the bar), turns emitted, and the
false-positive categories excluded. State the proxy gap honestly ("N files →
M genuine sessions across K **path-attributed** projects") — never call a file
count a "session" count.

### Step 2 — Load the cross-run ledger (and suppress what's settled)

```bash
python <SKILL_ROOT>/lib/ledger_store.py list --full --json
```

This is the skill's private state at `<SKILL_ROOT>/.local-state/proposals-ledger.json`
(NOT `~/.claude`, NOT any project). It carries previously-surfaced proposals
(deduped by `proposalId`), the **dismissed** do-not-resuggest set, and the
**applied** loop-closure records. With `--full --json` the output adds, alongside
the existing counts, the full `applied[]` array (each entry with `appliedAt` plus
`canonicalRule` or `ruleSummary`) and the full `dismissed[]` array (each with
`proposalId` + `reason`) — the plain `list --json` would only give you counts.
Note which `proposalId`s are in `dismissed[]` — you'll suppress any new candidate
that hashes to one of them and print a one-line `N suppressed (dismissed)` note.
Note the `applied[]` entries with their `appliedAt` and `canonicalRule` — Step 7
reads that `applied[]` array directly to check loop closure.

### Step 3 — Mine per-project friction (reuse the validated lens; do NOT invent a detector)

For each active code project in the corpus, read its genuine turns **closely**
through the **same `FRICTION_LENS`** `signal-scan` validated at Gate 3, in
priority order:

1. **Recurring friction** — the same complaint/correction across sessions.
2. **Intent-vs-behaviour gaps** — work did something other than wanted; re-steer.
3. **Unmet / repeated requests** — asked for but not delivered, or asked twice.
4. **Abandoned / thrashed work** — stalled, looped, dropped mid-task.
5. **Corrections that should become a standing rule.**

Emit, per project, a ranked `SurfacedFriction[]` (the established contract). Each
member's `scope` carries the repoId **and** the attribution mode that produced it,
so the promotion stage can ignore keyword-attributed scope:
```json
[{ "summary": "...", "proposedChange": "...", "targetHint": "...",
   "scope": ["<repoId>"], "scopeAttribution": { "<repoId>": "path" }, "rank": 1,
   "evidence": [{ "text": "<verbatim turn>", "sourceRef": "<sessionId>#<turn>",
                  "projectId": "<repoId>", "attribution": "path", "provenance": "measured" }] }]
```
**Provenance discipline is mandatory:** every friction cites a real turn;
`provenance` is `measured` for a verbatim quote, `derived` for an interpretation —
never label an interpretation `measured`. A pleasant "we did X" is context, not a
finding.

**This skill independently mines the fleet corpus** (it does not read another
tool's friction bytes — see the reuse note above). If `--frictions-in <path>` was
supplied (e.g. a fresh `signal-scan frictions.json`), fold those members in as
additional input first, then mine the remaining corpus inline. For a large corpus
you MAY fan one project's digest out to a subagent that returns the JSON; note any
fan-out (subagent billing/hooks were unverified before 2026-06-15 — see the
Command billing memory).

Concatenate every project's frictions into one
`<SKILL_ROOT>/.local-state/runs/<id>/fleet-frictions.json`.

### Step 4 — Cross-project recurrence promotion (the new rule)

```bash
python <SKILL_ROOT>/lib/recurrence_promote.py \
  --frictions <SKILL_ROOT>/.local-state/runs/<id>/fleet-frictions.json \
  --min-projects <N> --threshold <0.40> --json \
  > <SKILL_ROOT>/.local-state/runs/<id>/global-candidates.json
```

What it does:

1. **Cluster** all fleet frictions by lexical similarity of `summary + proposedChange`
   — single-link agglomerative over **cosine** similarity (the shared
   `_guards.cosine_sim` token + char-3gram primitive), default threshold **0.40**
   (intentionally well below the per-repo fold's 0.5: the same friction is worded
   differently across repos, so demand less overlap — near-identical frictions sit
   just above 0.40 cosine and must cluster, which a higher cut would split).
   Single-link gives transitivity so reworded variants chain into one cluster.
2. **Distinct-project count** per cluster = number of distinct `repoId`s across
   its members that were **PATH-attributed**. A repoId attributed only by keyword
   (`scopeAttribution[repoId] != "path"`) is **excluded from the count** — keyword
   over-attribution would manufacture false global candidates (a chat that merely
   *mentions* another project must not supply the 2nd distinct project). This is
   enforced in code, not just prose. Consequence by construction: a friction that
   recurs **five times in one repo** counts as **1 distinct project** —
   intra-project repetition is corroboration, not cross-project breadth
   (within-project occurrence is kept separately as a secondary rank signal).
3. **Promotion rule:**
   ```
   distinctPathProjectCount >= N (default 2)  →  GLOBAL CANDIDATE
   distinctPathProjectCount <  N              →  out of scope (see routing below)
   ```
4. **Route the demoted clusters** (named, not silently dropped): a cluster that
   misses the bar is reported as a **1-project leftover** with its home —
   per-project `CLAUDE.md` candidate → `transcript-analysis`; per-repo CLI signal
   → `/signal-scan <repoId>` (handoff requires the Command checkout; see Step 5
   note). Without a Command checkout, just **report** these clusters in the
   end-of-run summary — there is nothing to hand them to in-process.
5. **Rank** survivors by `(distinctPathProjectCount desc, total-occurrences desc)`.

**Honest caveat (carry it to the report):** this is **lexical** clustering only.
Two semantically-identical frictions with no shared content words will NOT
cluster. Never claim semantic dedup; the project COUNT is `measured`, "this is the
same friction across repos" is `derived`. The helper's footer states this.

**Surface `nearMissPairs` for human promotion review.** The helper emits a
`nearMissPairs` list — friction pairs whose **cosine** similarity lands in the band
`[threshold-0.10, threshold)` (with the 0.40 default, `[0.30, 0.40)`): close enough
that they *might* be the same cross-project friction but fell just under the
clustering cut. You MUST **surface these pairs in the run output** (not just let
the helper print them) so a human can decide whether to manually promote a pair
into one global candidate — never silently merge or split them yourself.

### Step 5 — Reconcile each global candidate against the global config surface

```bash
python <SKILL_ROOT>/scripts/reconcile_global_config.py reconcile \
  <SKILL_ROOT>/.local-state/runs/<id>/global-candidates.json --threshold 0.6 \
  > <SKILL_ROOT>/.local-state/runs/<id>/reconciled.json
```

`reconciled.json` **echoes each candidate's full body** (summary, proposedChange,
evidence, scope) alongside its `encoded` verdict and matched surface — it is
**self-contained**, so Step 6 authors `CrossProjectProposal`s straight from it
without re-joining back to `global-candidates.json`.

Before proposing ANY candidate, check it against what already ships globally — the
single biggest failure mode is recommending a rule/skill/behavior **already
encoded** in `~/.claude`. The reconciler reads three surfaces:

| Surface | Location | Encodes |
|---|---|---|
| Global CLAUDE.md | `~/.claude/CLAUDE.md` | Standing directives, rigor rules, gates, workflow |
| Global skills | `~/.claude/skills/<name>/SKILL.md` | Capabilities already built (dir name = the unit a skill proposal targets) |
| Memories | `~/.claude/projects/*/memory/*.md` (+ `MEMORY.md`) | Operator-confirmed corrections/facts, per-project — swept across ALL projects |

A candidate is judged **already-encoded** by three OR'd paths (mirroring Command's
`configExists`): (1) **skill-name** exact match; (2) **memory-title** overlap;
(3) **token overlap** of the candidate's key tokens present in the surface blob at
fraction `>= threshold` (default 0.6). **Drop every `encoded: true` candidate**;
keep the rest as genuinely-novel.

**Honest limit — always disclose:** this check is **lexical**. A candidate
phrased differently from an equivalent rule already in config (e.g. a
`mergeStyle: squash` setting vs the prose "always squash-merge") may survive as a
false novel. The reconciler sets `stopFlag: true` on borderline scores
(`threshold-0.15 <= frac < threshold`); eyeball those. **Never claim semantic
dedup you didn't do.**

**Report what you dropped** — list each reconciled-out candidate with the surface
that covers it ("dropped: 'mark claims measured/derived/assumed' — already in
~/.claude/CLAUDE.md Claims & Evidence Rigor"). Transparency about drops proves the
check ran. If the reconciler exits non-zero with `WARN: empty global surface`, the
reader is broken / `HOME` is wrong — STOP and investigate before trusting any
NOVEL verdict.

> **/signal-scan handoff requires the Command checkout.** Several places suggest
> handing a 1-project cluster back to `/signal-scan`. `/signal-scan` is a
> **Command-repo skill** invoked from within `C:/Users/Bryce/Projects/Command`;
> it does not exist on a machine with no Command checkout. On such a machine the
> correct behavior is to **report** the 1-project clusters (and their suggested
> home) in the end-of-run summary, not to promise a handoff target that is absent.

### Step 6 — Assemble the proposal set and freeze it into a claims pack

For each surviving candidate, author a `CrossProjectProposal` (schema below):
pick the single best of the four routing targets, write the **exact patch text**,
a `<=15-word` headline, a smoothed `confidence` (`s/(s+c+2)` — never "always" from
a thin sample), and attach the cluster's distinct-project evidence (>=2 distinct
**path-attributed** `projectId`s, each with a real `sourceRef`).

Then freeze the ranked set into the loop's handoff artifacts:

```bash
python <SKILL_ROOT>/lib/claimpack.py build \
  --proposals <SKILL_ROOT>/.local-state/runs/<id>/proposals.json \
  --evidence-dir <SKILL_ROOT>/.local-state/runs/<id>/turns.ndjson \
  --min-projects <N> \
  --out <SKILL_ROOT>/.local-state/runs/<id>/claims-pack.json
```

> **Note on `--evidence-dir`:** despite the "-dir" suffix, this flag accepts the
> **turns NDJSON file** (`turns.ndjson`), not a directory. It is a **documentation
> pointer only** — the evidence-pack excerpts are pulled from each proposal's
> `evidence[].text` (which IS the verbatim mined turn, captured in Steps 3/6), not
> re-read from this file. `claimpack build` warns if the path does not exist.

This emits two co-located files **and** dedups the proposal set within itself at
**cosine `>= 0.85`** — actual cosine similarity over token (and/or char-ngram)
vectors of the sub-claim text, the same `cosine >= 0.85` constant the in-loop
drift-guard uses (Step 9) — so two phrasings of one friction don't each burn a
review slot:

- **`claims-pack.json`** — each proposal rendered as **three conjoined
  falsifiable sub-claims**, each with a stable, joinable `id` of the form
  `<proposalId>:recurrence` / `:gap` / `:efficacy`:
  1. **Recurrence** — "friction `<id>` appears in >=N distinct projects: A, B, …"
  2. **Gap** — "the proposed fix is NOT already encoded in any cited project's
     `.claude/` surface (and not in the global surface)."
  3. **Efficacy** — "**this specific fix** would actually reduce **this specific
     friction** (the cited turns)."
- **`evidence-pack.md`** — per sub-claim, **pointers to primary sources** the
  claim lenses must read directly: the exact `session#turn` refs (recurrence), the
  exact `.claude/` file paths to grep (gap), the cited friction turns (efficacy).
  **Each section header prints the exact joinable sub-claim id** (e.g.
  `### <pid>:recurrence`) so reviewers can echo it. **Excerpts, not summaries** —
  the lenses read ground truth, not our gloss.

The pack also carries a `contentHash`. **Note (do not over-claim):** this hash is
this skill's own integrity/idempotency key over the normalized sub-claim text. It
is **not** review-loop's internal stall key — review-loop computes its own hash
over the text it receives (Step 9). Stall/termination is decided purely by
review-loop's `<promise>review-stalled</promise>` output, not by comparing this
hash.

### Step 7 — Loop-closure check (is a previously-applied fix still failing?)

For each entry in the `applied[]` array surfaced by the Step 2
`list --full --json` call, detect recurrence yourself — **there is no helper for
the detection; the agent performs it** over the corpus retrieved in Step 1.
Concretely, for each applied entry `{proposalId, canonicalRule, appliedAt}`: scan
`<SKILL_ROOT>/.local-state/runs/<id>/turns.ndjson` for any turn whose `tsMs` is
**> `appliedAt`** AND whose text matches the rule's intent — match on the rule's
key content (or reuse the shared metric, e.g. a one-off
`python -c "import sys; sys.path.insert(0,'<SKILL_ROOT>/lib'); import _guards; print(_guards.cosine_sim(a,b))"`
with a `>= 0.40` cut), **not** a brittle substring. The first such hit is the
recurrence. If one exists, record it:

```bash
python <SKILL_ROOT>/lib/ledger_store.py mark-recurrence \
  --proposal-id <id> --recurred-session <sid> --ts <ms>
```

This flips `loopClosure.recurringPostApplication = true`, records the recurrence
(the `--recurred-session` / `--ts` are persisted to
`loopClosure.recurrences[]` by the helper, so the audit trail of *which* later
sessions re-hit the rule is durable across runs), drops confidence in the
un-changed patch, and sorts the proposal to the very top — it means **"a rule you
added to `~/.claude` is being ignored in practice."** Operationally: don't
re-propose the same prose — **escalate** (a rule that recurred after going into
`global-claude-md` should be re-proposed as a `settings-hook` or a `global-skill`,
citing the recurrence in the new `rationale`).

---

## THE VALIDATION LOOP (the central requirement)

The skill does NOT ship proposals on its own say-so. It hands the set to
`/review-loop --mode claim` and lets an **independent** reviewer team try to break
each one against the ground-truth corpus. Survivors ship; falsified proposals are
dropped; partial-failures are deflated and re-tested.

This **reuses `--mode claim` verbatim** — we do NOT copy review-loop's agent files
into this skill (the Option-B fork was explicitly rejected; pattern-retrospective
§14). If review-loop's claim lenses change, this skill inherits the change.

> **Critical contract — the verdict is built from findings YOU capture, not from
> review-loop's state file.** Verified against real archived claim-mode runs:
> review-loop's `findings_history` is either empty or a list of per-iteration
> **summaries** (`{iteration, fixed, speculative, method}`) — NOT per-finding
> records, and the file is **moved to `.local-state/archive/<session-id>-<ts>.json`**
> on terminal exit (sometimes with a UTF-8 BOM). You therefore **cannot** rely on
> reading per-finding dispositions out of that file. Instead, the orchestrating
> agent (you) must **capture each claim lens's returned finding JSON in-session**
> and write it to a normalized file this skill controls. The verdict step reads
> **that** file and is **fail-closed**: a sub-claim with no finding that
> demonstrably examined it is reported `unvalidated`, never `survived`.

### Step 8 — Run the claim-mode loop (in batches) and capture findings

Hand the claims-pack to review-loop **in batches of `--batch` proposals**
(default 3) so the per-loop finding budget is not spread so thin that most
sub-claims go unexamined. For each batch `b` (a `claims-pack.json` slice the
agent writes):

```
/review-loop --mode claim \
  --session-id review-globals-loop-<id>-b<b> \
  --max-iter 3 \
  <claims under review: .local-state/runs/<id>/claims-pack-b<b>.json> \
  <evidence: .local-state/runs/<id>/evidence-pack.md>
```

The four claim lenses fire in parallel, each told to read the cited sources
directly:
- `claim-falsification` → go to the transcript refs and the live `.claude/` files
  and try to **disprove** recurrence / gap / efficacy.
- `claim-method` → is the recurrence count honest (distinct **path-attributed**
  projects, not the same project re-counted; attribution by an authoritative
  field, not inference)?
- `claim-calibration` → does the rank/headline match the evidence; is a 2-project
  floor being sold as fleet-wide?
- `claim-coverage` → did assembly examine the right corpus; is a high-leverage
  friction missing to over-aggressive filtering?

**Discipline line you MUST add to the invocation (every batch):** *"Tag every
finding with `subClaimId` = the exact sub-claim id it addresses (the
`<proposalId>:recurrence|gap|efficacy` token printed in the evidence-pack section
header). A finding with no `subClaimId` cannot be attributed and will be dropped.
If you tried to break a sub-claim and could NOT, return a finding with
`category: "failed-falsification"`, `load_bearing: true`, and that `subClaimId` —
this is a positive result that marks the sub-claim examined-and-held."*

**Capture step (the load-bearing fix).** As each batch's lenses return, collect
their finding JSON objects and append them to a single normalized file the skill
owns:

```
.local-state/runs/<id>/findings-captured.json   ==  a JSON array of:
{ "subClaimId": "<pid>:recurrence|gap|efficacy",   // REQUIRED — the join key
  "category": "...", "severity": "low|medium|high",
  "load_bearing": true|false, "claim": "...", "file": "<source ref>" }
```

Do not trust the state file to contain these. You transcribe what the lenses
returned in-session. (One file across all batches.)

**`subClaimId` is assigned by the ORCHESTRATING AGENT, not by the reviewer.** The
claim-lens finding schemas do **not** carry a `subClaimId` field, so it is not
present in what the lenses return. When YOU (the agent) transcribe each lens
finding into `findings-captured.json`, YOU assign its `subClaimId` — you know which
sub-claim the finding addresses from the **evidence-pack section header it cited**
(each section prints the exact `<proposalId>:recurrence|gap|efficacy` token; the
finding's `file`/source ref points back into that section). The reviewer "tag every
finding with `subClaimId`" discipline line above is a **hint** that steers the lens
to name the section it attacked — it is **not** the source of truth for the join.
The authoritative `subClaimId` is the one the orchestrating agent records here.

Each finding is itself **falsified against its primary-source excerpt**
(review-loop Step 5, claim variant) before it can drop a proposal — a reviewer
that misquotes its own source is dropped. This double gate keeps the loop from
over-pruning.

### Step 9 — Apply the verdict, refine, converge

Attribute each captured finding to a sub-claim via its **`subClaimId`** (exact
match on the joinable id — not substring guessing):

- **Dropped** — a `recurrence` or `gap` sub-claim is falsified (load-bearing,
  severity >= medium). A friction that doesn't actually recur across N projects, or
  a fix that already ships, is not fleet leverage. Remove it.
- **Refined (deflation-ONLY)** — an `efficacy`/`calibration` finding is actionable
  but the gap is real. **Narrow** the proposal so the surviving claim is true:
  lower the asserted project count to what evidence supports, tighten the fix to
  the friction it provably prevents, or downgrade rank. **Never strengthen a claim
  to rescue it** — that re-introduces the overstatement the loop exists to catch.
- **Survived (examined)** — a `failed-falsification` finding cites the sub-claim:
  a lens actively attacked it and could not break it. This is the strongest
  positive result.
- **Unvalidated (untouched)** — **no** finding cites the sub-claim at all. The
  loop did not examine it. It is reported `unvalidated`, **NOT** `survived`, and
  is **not** eligible to apply. (This is the fail-closed rule: absent evidence
  never renders as a pass. An un-reviewed proposal and an actively-attacked-and-held
  proposal MUST be distinguishable.)

**Convergence / termination** — re-run only over the **changed** proposals
(deflations create new claim text). Terminate on the first of:
- **Clean** — review-loop returns `<promise>review-clean</promise>`: every
  surviving proposal's three sub-claims were examined and withstood falsification.
- **Exhausted** — `--max-iter` (default 3) reached or cost ceiling crossed
  (`<promise>review-exhausted</promise>`). Unresolved proposals are reported
  **unvalidated**, not shipped as validated.
- **Stalled** — review-loop emits `<promise>review-stalled</promise>` (its own
  internal stall detection — its hash over the claim text it received was
  unchanged after an attempted-deflation iteration). Stop; report survivors-so-far
  as **partially validated**. (We rely on review-loop's promise, not our own
  `contentHash`, for this.)

Deflation-only refinement guarantees forward progress: each refined claim is
strictly weaker, so the set monotonically shrinks toward supportable claims — it
cannot oscillate.

**Dedup at cosine `>= 0.85`** in two passes (the established fleet constant):
within-set pre-loop (done by `claimpack.py build`, Step 6) and cross-iteration
in-loop (review-loop's own Step 6 drift-guard, so a deflated proposal isn't
re-flagged for the reason it was already refined).

### Step 9b — Render the terminal verdict + persist survivors to the ledger

```bash
python <SKILL_ROOT>/lib/claimpack.py verdict \
  --claims-pack <SKILL_ROOT>/.local-state/runs/<id>/claims-pack.json \
  --findings <SKILL_ROOT>/.local-state/runs/<id>/findings-captured.json \
  --terminal-state <review-clean|review-exhausted|review-stalled> \
  --iteration <n> \
  --out <SKILL_ROOT>/.local-state/runs/<id>/validation-verdict.md
```

**Combining per-batch promises into one `--terminal-state`:** Step 8 may run
several batches, each ending in its own promise. Pass the **worst-of** to
`--terminal-state`, ordered `review-stalled` > `review-exhausted` > `review-clean`
— the run is `review-clean` only if EVERY batch returned clean; any stalled batch
makes the whole run `review-stalled`. This keeps the headline from over-claiming
validation when one batch failed to converge.

`verdict` reads the **agent-captured findings file** (not review-loop's state). It
is **fail-closed**: any sub-claim with no attributed finding renders `unvalidated`.
A proposal is `survived` **only** if all three of its sub-claims are either
`held-examined` (a `failed-falsification` finding cited them) — never if they are
merely untouched. It also prints the **attribution match rate** (findings that
joined to a sub-claim / total findings) and **warns loudly** if the rate is low
(a sign reviewers didn't echo `subClaimId`).

The verdict is a **per-proposal disposition table**:

| field | meaning |
|---|---|
| `id`, `summary` | which proposal |
| `disposition` | `survived` \| `refined` \| `dropped` \| `unvalidated` |
| `projects_validated` | distinct **path-attributed** projects the recurrence claim survived at (>= N for survivors) |
| `sub_claims` | recurrence / gap / efficacy each `held-examined` \| `falsified` \| `deflated` \| `unexamined` |
| `examined` | did ANY lens demonstrably examine each sub-claim (yes/no per sub-claim) |
| `falsifier_evidence` | for dropped/deflated: the short reason + the source ref that broke it |
| `iteration` | which iteration settled it |
| `final_rank` | rank among survivors only |

Headline states the terminal promise verbatim + counts, including the
fail-closed bucket: *"K of M proposals survived (examined & held), J refined, L
dropped, U unvalidated (never examined); validation reached `<terminal-state>` at
iteration `<n>`; finding attribution match rate X%."*

Then upsert survivors into the cross-run ledger (cross-run dedup, corroboration,
smoothed confidence, dismissed-skip; rejects any malformed proposal):

```bash
python <SKILL_ROOT>/lib/ledger_store.py upsert --proposal-file <each-survivor>.json
```

(Skip in `--dry-run`.) Only `survived` (examined & held) proposals are eligible to
be shown for apply. `refined` survivors carry their deflated text. `dropped` and
`unvalidated` proposals are recorded in `validation-verdict.md` as the audit trail
but are NOT upserted as apply-eligible. Honesty rule (no-edit-meta in
deliverables): the proposal states its final validated position; the
drop/deflate/unexamined history lives only in the verdict file.

---

## Proposal output format (the product)

A ranked list of `CrossProjectProposal`s. It reuses `chat-arch`'s `ProposedUpgrade`
verbatim and wraps it with the cross-project evidence and lifecycle this skill adds:

```jsonc
{
  "proposalId": "sha1(canonicalRule)",            // stable across runs
  "canonicalRule": "Run the build before claiming a fix is done", // imperative, project-agnostic
  "rank": 1,                                       // 1 = highest leverage / most likely to recur
  "scope": { "kind": "global", "distinctProjects": 4 }, // path-attributed projects only
  "occurrenceCount": 7,
  "firstSeen": 1733000000000, "lastSeen": 1734000000000,
  "confidence": 0.71,                              // smoothed s/(s+c+2); never "always" from one hit
  "evidence": [                                     // >=2 DISTINCT path-attributed projects, hard-required
    { "projectId": "shopforge",     "attribution": "path", "text": "<verbatim operator turn>", "sourceRef": "<sessionId>#<turn>", "provenance": "measured" },
    { "projectId": "signal-engine", "attribution": "path", "text": "<verbatim operator turn>", "sourceRef": "<sessionId>#<turn>", "provenance": "measured" }
  ],
  "proposedUpgrade": {                              // chat-arch ProposedUpgrade, verbatim
    "target": "settings-hook",                     // ONE of the four routes below
    "targetPath": "settings.json :: hooks.Stop",
    "headline": "Block 'done' claims until the build passes",   // <=15 words, the punchline
    "patch": "<EXACT text to add — ready to paste, not a description>",
    "rationale": "Recurred in 4 projects after being added as prose to global CLAUDE.md → escalate to a hook.",
    "applied": false, "appliedAt": null
  },
  "loopClosure": { "recurringPostApplication": false, "alreadyEncoded": false }
}
```

**Non-negotiables for every proposal:**
1. **Ranked** — weight by distinct path-attributed project count, then recurrence,
   then recency. A 2-project proposal must not outrank a 4-project one on recency
   alone.
2. **>=2 distinct path-attributed-project instances** — `evidence[]` spans two or
   more different `projectId`s, **each with `attribution: "path"`**;
   `ledger_store.py` rejects (exit 4) any that fails. `projectId` is derived from
   the session's repo/cwd (path-attribution), never LLM-labeled.
3. **Exact patch text** — `patch` is the literal string to paste.
4. **Exactly one of the four targets** (next section).
5. **Provenance-tagged evidence** (`measured` / `derived` / `assumed`).

### The four routing targets

The skill is the **global** layer — per-project config is out of scope (that's
`signal-scan` / `transcript-analysis`). Every proposal routes to exactly one:

| `target` | `targetPath` example | When |
|---|---|---|
| `global-claude-md` | `~/.claude/CLAUDE.md` | A standing behavior/preference rule for the global directives. |
| `global-skill` | `~/.claude/skills/<name>/SKILL.md` | Best fixed by creating/editing a reusable skill, not a prose line. |
| `memory` | `~/.claude/projects/.../memory/<slug>.md` | A durable fact/correction better as auto-memory than a hard rule. |
| `settings-hook` | `settings.json :: hooks.<Event>` | Must be *enforced* by the harness (rule keeps getting ignored as prose → mechanize). Apply via `update-config`. |

**Excluded by design:** `project-claude-md` (duplicates signal-scan /
transcript-analysis), `agent`, `command`, `prompt-snippet`. The validator rejects
any target outside the four. (Internally these map from chat-arch's
`UpgradeTarget`: `skill`→`global-skill`.)

---

## State ledger (cross-run memory)

The skill is **stateful across runs** so it never re-proposes what you've seen,
dismissed, or applied. State lives in **one file the skill owns**:

```
<SKILL_ROOT>/.local-state/proposals-ledger.json
```

This is the skill's private cache — **NOT** `~/.claude/` and **NOT** any project.
(The skill proposes changes to `~/.claude`; it must never write its own
bookkeeping there.) Same `.local-state/` convention review-loop uses. Manage it
**only** through the helpers (atomic writes, filelock, schema-validated, 3 rotating
backups). Never hand-edit the JSON.

```jsonc
{
  "schemaVersion": 1,
  "generatedAt": 1734120000000,
  "proposals": [ /* CrossProjectProposal; deduped by proposalId */ ],
  "dismissed": [ { "proposalId": "…", "reason": "…", "dismissedAt": 1734000000000 } ],
  "applied":   [ /* AppliedImprovement (chat-arch shape, verbatim) */ ]
}
```

- **`proposals[]` — cross-run dedup.** `proposalId = sha1(canonicalRule)` (the
  id-derivation chat-arch uses for `CorrectionPattern.id`), so a re-run assigns the
  same proposal the same id. Re-mining **corroborates** an existing row (merge
  evidence, bump `occurrenceCount`, refresh `lastSeen`) instead of duplicating.
- **`dismissed[]` — do-not-resuggest.** Decline a proposal →
  `python <SKILL_ROOT>/lib/ledger_store.py dismiss --proposal-id <id> --reason "<why>"`.
  Every future run skips it and prints `N suppressed (dismissed)` (honest, not
  silent).
- **`applied[]` — loop closure.** When the operator applies a proposal, an
  `AppliedImprovement` (chat-arch shape, verbatim) is appended with `appliedAt`.
  The next run's Step 7 checks whether the rule recurred after that timestamp.

---

## Apply boundary — PROPOSE only; the boundary is MECHANICAL, not honor-system

**This skill has NO write tools.** Its `allowed-tools` are `Read, Grep, Glob,
Bash, Task` — there is **no `Edit` and no `Write`**. Every PRIVATE-CORPUS artifact
and the ledger are written by the Python helpers (invoked via Bash) into the skill's
own `.local-state/`; the helpers refuse any path inside `~/.claude` config or a git
tree. (The agent may also materialize the intermediate JSON files that have no
emitting helper — see Helpers note — but only into the skill's `.local-state/` via
Bash; it still cannot write `~/.claude`.) The agent running this skill therefore **cannot** edit `~/.claude/CLAUDE.md`,
a global `SKILL.md`, a memory file, or `settings.json` — the apply boundary is
enforced by the absence of write tools, not by the model choosing to obey a prose
rule.

### Step 10 — Apply (operator-confirmed, through a separate surface)

Applying a survivor is a **separate step the operator drives through a different,
write-capable skill** — review-globals-loop hands off; it does not apply.

1. review-globals-loop **shows** the chosen `survived` proposal — `headline`,
   `target`, `targetPath`, and the **exact `patch`** — as text.
2. The operator applies it via the surface that owns that target. None of these
   are done by review-globals-loop:
   - `settings-hook` → the **`update-config`** skill (it owns `settings.json`).
   - `memory` → the memory mechanism.
   - `global-claude-md` / `global-skill` → a confirmation-gated edit the operator
     makes in a separate, write-capable session (e.g. paste the exact patch
     themselves). The patch is shown verbatim precisely so the apply is an
     out-of-band, operator-confirmed action — not a same-agent Edit triggered by a
     possibly-misread "yes".
3. **Only then** record it for loop-closure bookkeeping:
   ```bash
   python <SKILL_ROOT>/lib/apply_record.py --proposal-id <id> \
       --target-files "~/.claude/CLAUDE.md" --notes "<what was done>" --confirm-operator
   ```
   `apply_record.py` appends an `AppliedImprovement` (chat-arch shape, verbatim,
   idempotent on the `(patternId,target,targetPath)` triple) to the ledger's
   `applied[]` — it touches **only the ledger**, never the config. It is a
   required step — without it the loop never closes and the proposal keeps
   ranking. **It fails closed unless it has positive proof of an attending
   operator: the explicit `--confirm-operator` token (shown in the command above)
   is the SOLE positive attendance proof.** An interactive TTY on stdin is **NOT**
   trusted (`isatty()` is unreliable on Windows, so it cannot be used as evidence
   an operator is present). As a secondary block, the helper additionally aborts if
   any known watcher/unattended env marker is set (incl. `COMMAND_PARALLEL_SLOT`,
   `COMMAND_ROOT`, `COMMAND_PORT`, `COMMAND_FIELDS`, plus `CLAUDE_WATCHER` /
   `COMMAND_WATCHER` / `CLAUDE_UNATTENDED` / `CLAUDE_REVIEW_LOOP_ACTIVE` / `CI`) —
   so even a stray `--confirm-operator` in an automated slot is refused. It also
   warns that, since review-globals-loop has no write tools, the actual config edit
   must already have been made by the operator through the surface above.

**The mutating path is guarded primarily by the absence of write tools.** Because
review-globals-loop has **no write tools**, it cannot itself perform the global-config
mutation under any context (attended or not) — that is the primary guard. As a
belt-and-suspenders second layer, `apply_record.py` requires **positive attended
proof** — the explicit `--confirm-operator` token (an interactive TTY is NOT
trusted, since `isatty()` is unreliable on Windows) — and additionally aborts if a
known watcher/unattended env marker is set
(`COMMAND_PARALLEL_SLOT`, `COMMAND_ROOT`, `COMMAND_PORT`, `COMMAND_FIELDS`,
`CLAUDE_WATCHER`, `COMMAND_WATCHER`, `CI`, …); and the `update-config` / memory
surfaces are themselves operator-driven and confirmation-gated. There is no path
by which a watcher run mutates global config while only the audit record is
skipped — the mutation requires a write-capable surface a watcher does not drive.

**Guards baked into the helpers:**
- This skill has **no Edit/Write tools** — the mechanical apply boundary.
- `apply_record.py` **fails closed unless an operator is attending** — the explicit
  `--confirm-operator` token is the sole positive attendance proof (an interactive
  TTY is NOT trusted; `isatty()` is unreliable on Windows), and it also aborts under
  known watcher/unattended env markers (`COMMAND_PARALLEL_SLOT`, `COMMAND_ROOT`,
  `COMMAND_PORT`, `COMMAND_FIELDS`, `CLAUDE_WATCHER`, `COMMAND_WATCHER`, `CI`, …).
- `corpus_retrieve.py` and `claimpack.py` route every output path through the shared
  `_guards.assert_safe_out` (refuses `~/.claude` except the skill `.local-state/`,
  refuses git trees, **exit 7**); `ledger_store.py` likewise refuses any output path
  inside `~/.claude` config or a git working tree — bookkeeping and private corpus
  can never be mistaken for config or be committed.
- The skill **never** sets `proposedUpgrade.applied = true` itself; that flips
  only via `apply_record.py` acting on the operator's confirmation (chat-arch's
  "set true ONLY by the user" rule).

---

## Loop guardrails (non-circularity)

The loop is only meaningful if the validator is independent of the proposer:

- **Two halves, hard wall.** The mining/assembly half (this skill) produces
  proposals; it **never scores or defends** them in the loop. The falsification
  half is review-loop's claim-lens subagents reading the **primary corpus**
  (transcripts + live `.claude/` files), not this skill's summary. The
  evidence-pack ships excerpts/pointers precisely to force ground-truth reads.
- **No self-grading.** Do not let the assembling agent also play falsifier in the
  same pass.
- **Fail-closed verdict.** Absent evidence is `unvalidated`, never `survived`. A
  proposal no lens examined is never shippable. The verdict is built from
  agent-captured per-finding JSON (joined by `subClaimId`), not from review-loop's
  state file (which carries only per-iteration summaries).
- **Deflation-only refinement.** Survivors may only be made weaker between
  iterations. Strengthening to dodge a falsifier is forbidden.
- **Verbatim reuse, no fork.** Invoke `/review-loop --mode claim`; never copy its
  agent files here.
- **Failed falsification is a result — but it must be cited.** "Tried to break
  this and could not" is the strongest outcome — but only when a
  `failed-falsification` finding with the sub-claim's `subClaimId` was actually
  returned. An empty finding set is NOT promotion; it is `unvalidated`.
- **Reconcile gap-claims against LIVE config.** The `gap` sub-claim is grepped
  against each cited repo's actual `.claude/` surface at loop time — never a stale
  inventory.

---

## Guardrails (summary)

- **Cross-project ONLY.** A 1-project cluster — however many times it repeats — is
  out of scope; report it and name its home (`transcript-analysis` for a
  per-project CLAUDE.md candidate, `/signal-scan` for a per-repo CLI signal — the
  latter only when a Command checkout is present). This skill writes no per-repo
  config and proposes nothing global from a single project.
- **Global ~/.claude is the only proposal target; it is never written by this skill
  (no write tools).**
- **Path-attribution for the count, never keyword.** The promotion bar counts only
  distinct **path-attributed** projects; keyword over-attribution is excluded in
  code so it cannot manufacture false global candidates.
- **Reconcile before propose.** Drop already-encoded; disclose the lexical limit.
- **Grounded + provenance-tagged.** Every friction cites a real turn; measurement
  and interpretation are labeled separately (Claims & Evidence Rigor).
- **Validate before ship, fail-closed.** Only `survived` (examined & held)
  claim-loop proposals are eligible to be applied; `unvalidated` (never examined),
  `exhausted`, and `stalled` survivors are clearly labeled and not apply-eligible.
- **Private corpus stays local.** Verbatim cross-project turns are written only
  under the skill's untracked `.local-state/`, never a tracked/remoted repo.
- **Honest sample size.** Smoothed confidence; a 2-project proposal is "emerging,"
  not "always."

## End-of-run report

Close with (operator persona — terse, plain-language first):
- One line: *"Scanned N files → M genuine sessions across K path-attributed
  projects (both corpora). P global candidates promoted (>=N distinct
  path-attributed projects); Q reconciled out as already-encoded. Validation
  reached `<terminal-state>`: K survived (examined & held), J refined, L dropped,
  U unvalidated."*
- The ranked survivor table (headline, distinct projects **named**, target +
  exact patch, confidence).
- The single highest-value item first if any `recurringPostApplication` fired
  ("you applied X but it's still recurring — escalate to a hook").
- The 1-project leftovers and where they belong (transcript-analysis / signal-scan).
- The lexical-limit caveat (clustering + reconcile are lexical; reworded duplicates
  can slip through) and the finding attribution match rate.
- Then a few ranked next actions (apply #1 via the right surface? raise
  --min-projects? hand the 1-project clusters off where a checkout exists?).

