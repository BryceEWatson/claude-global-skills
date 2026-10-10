# claude-global-skills

A curated collection of [Claude Code](https://claude.com/claude-code) **skills**:
a multi-agent code-review loop, exhaustive local chat-history search, an
evidence-grounded session end/resume pair, Gemini image generation, and rigorous
transcript retrospectives. They run **machine-wide with nothing but `python` and
`node`**. There's no per-project install: drop a skill into `~/.claude/skills/` and
invoke it as a slash command in any session.

These are global skills, version-controlled here so they can be reviewed, shared,
and deployed to a fresh machine. **The repo is the source of truth**; each live copy
is a deployed (materialized) copy. Most skills target **Claude Code**
(`~/.claude/skills/`). A skill can also declare **Codex** (`~/.codex/skills/`) as a
target and deploy to both from one shared source; see
[Cross-runtime targets](#cross-runtime-targets-claude-code--codex).

## What this shows

These are the checks I run on my own software before anyone relies on it:

- A code review that tries to prove each problem it finds wrong before reporting it, runs the project's own tests and build, and, when there's a pull request, records its verdict on the exact version it checked.
- Work notes where every important claim says how I checked it, or says plainly that I didn't.

I bring the same habits to client work through [Watson Standard](https://watsonstandardco.com), where I build, fix and harden software before real users rely on it.

## Quickstart

Install a single skill by copying its directory into your Claude Code skills folder:

```bash
git clone https://github.com/BryceEWatson/claude-global-skills
cp -r claude-global-skills/review-loop ~/.claude/skills/
```

Then invoke it in any Claude Code session:

```
/review-loop
```

That's it for prompt-only skills. A few skills also ship an installer (they wire a
hook into `~/.claude/settings.json`) — run `node ~/.claude/skills/<name>/install.cjs`
after copying. See each skill's section below.

## Skills

### Core — portable, useful for anyone

| Skill | What it does |
|---|---|
| [`review-loop`](review-loop/) | Dispatches a multi-agent review team over your session's diff, runs an execution-grounded lint/test/build check, validates each finding through a falsifier stage, and posts a commit-pinned verdict on the PR. Four lens sets by artifact type: code, plans, analytical claims, and finished deliverables (whether the human it's handed to can actually use it). Ships a Stop-hook + installer. |
| [`gemini-image`](gemini-image/) | Generate and edit images via Google's Gemini API from one zero-dependency Python CLI — reference-image input, multi-image output, safety-block diagnostics, best-available-model selection. |
| [`chat-history-search`](chat-history-search/) | Exhaustively search your local Claude history across both corpora (Claude Code CLI + Cowork/Desktop) — knows every log location and the false-positive gotchas (task-notifications, TodoWrite items, tool results) that trip up naive grep. |
| [`pattern-retrospective`](pattern-retrospective/) | Mine your transcripts for recurring patterns with real rigor: audit-the-target-first discipline, streaming JSONL parse, 5-tuple extraction with provenance, self-falsification, and Krippendorff-α inter-rater checks. |
| [`session-end`](session-end/) | Close out a session into an evidence-grounded record (decisions, claims + verification, assumptions, artifacts, reversals); mid-flight, also emits a ready-to-paste continuation prompt that carries its own reconcile rules. Closes light, with no handoff file, when everything already sits in PRs. |
| [`monitor-agent-thread`](monitor-agent-thread/) | Watch a live or recent Claude Code **or** Codex session from the other product via local session logs, with a safe projection that never exposes hidden reasoning, raw tool arguments, signatures, encrypted content, or secrets. The first **dual-target** skill (Claude + Codex). |
| [`fable-seat`](fable-seat/) | Operating profile for a session running on Fable (`claude-fable-5`): default effort `high` and step DOWN for routine work rather than reaching for `xhigh`, lead task framing with the outcome, keep the prompt cache warm by farming cheap work to a subagent instead of inline-switching the main model, and budget + compact long runs. A genuine no-op on Opus and every non-Fable model; Fable-mechanics guidance, not a superiority claim. |
| [`session-portal`](session-portal/) | Pass a short message from a Claude Code session to a Codex session, or back, through a local SQLite queue behind an authenticated MCP server. The recipient picks it up at a safe point and acts under its own permissions; nothing is pushed into a running session. Dual-target (Claude + Codex). |
| [`ship-it`](ship-it/) | Take a build task from first read to a reviewed pull request: research, a plan that gets reviewed before any code, implementation, tests against the real app, then a review of the PR, looping each step until it's done. Leans on `review-loop`. |

### Personal examples — wired to the author's setup; adapt before use

These show real, working patterns but reference the author's own projects, sites,
or companion skills. Read them as reference implementations and adjust the paths,
domains, and assumptions to yours.

| Skill | What it does |
|---|---|
| [`transcript-analysis`](transcript-analysis/) | Single-project transcript miner → proposes that project's `CLAUDE.md` candidates. The single-project sibling of `global-review-loop`. |
| [`seo-index-validation`](seo-index-validation/) | Probe a deployed site's crawl/index health (status codes, redirects, soft-404, sitemap, GSC) and diagnose why pages aren't indexed. A no-auth `bash`+`curl` script plus a playbook. |
| [`global-review-loop`](global-review-loop/) | Mine your whole fleet's history for friction that recurs across projects, then propose global `~/.claude` changes — reconciled against what already ships and self-validated by an adversarial claim loop. (Wired to a project registry; see its SKILL.md.) |
| [`chat-arch-thrash-detect`](chat-arch-thrash-detect/) | A `PostToolUse` hook that nudges when a session falls into edit-thrash / read-loop / test-loop / tool-flail spirals. Hook host (not slash-invoked); ships its installer. |
| [`weekly-work-log`](weekly-work-log/) | Build a public weekly work-log page from session-end handoffs + git, with every number re-verified. Wired to the author's site as a worked example. |
| [`email-editorial-pass`](email-editorial-pass/) | An editorial pass that runs before an outbound email draft is created: no em dashes, no hype, first person, the point up front. Wired to the author's published voice profile as a worked example. |

> `session-handoff` is a thin alias that routes to `session-end` (the skill was
> renamed); `/session-handoff` still works if it's installed.

## Privacy & safety

Several skills (`chat-history-search`, `transcript-analysis`,
`pattern-retrospective`, `global-review-loop`) read your **private local Claude
chat history**, and they don't all handle what they write the same way:

- **`global-review-loop`** is the only one with a write guard. Its scripts that
  take an output path check it with a fail-closed guard that refuses to write
  into your `~/.claude` config or into any git working tree. Its own git-ignored
  `.local-state/` folder is the one exception (the guard only warns if that folder
  sits inside a git working tree), and its proposals ledger is written to a fixed
  file in that folder.
- **`pattern-retrospective`** has no guard. Its instructions put studies and
  extracted data under `research/studies/` in the current project, its
  finding-registry scripts write to `reports/_data/` there, and its coder script
  writes wherever you point `--output`.
- **`transcript-analysis`** is instructions only. It tells the session to write its
  report to `research/studies/` in the current project, or to the project root if
  that folder doesn't exist, so the report can land in a tracked tree.
- **`chat-history-search`** is instructions only. Its example commands keep lists
  of matching log files in `/tmp`, and it reports what it finds in the session
  rather than in a report file.

Check where a skill will write before you run it, and keep mined output out of
anything you commit or publish. Skills also run as executable code (some install
hooks), so a skill is a code-execution surface. **Read [`SECURITY.md`](SECURITY.md)
before installing or contributing**, and review a skill's code before you deploy it.

## How this repo is maintained

The repo is **canonical**: skills are authored here and *deployed* (materialized) to
each live product tree. The engine is `scripts/sync.py` (stdlib-only):

```bash
python scripts/sync.py --deploy    # repo -> live, for each skill's declared targets
python scripts/sync.py --check     # report drift (live vs the repo-materialized output)
python scripts/sync.py --capture --target claude   # pull a live edit back into the repo
```

`--check`/`--deploy` with no `--target` act on each skill's **declared targets whose
live home exists**, so `--deploy` then `--check` round-trips cleanly and a machine
without Codex is never touched. `--capture` requires an explicit `--target` and
refuses to silently resolve a divergence between two live copies. Contributors don't
need the maintainer's live tree — see [`CONTRIBUTING.md`](CONTRIBUTING.md) for the
outside-contributor path, the test commands, and the merge gate.

**Optional drift nudge.** `hooks/skills_drift_hook.py` reminds you to reconcile a
skill you edited in a live tree. It's a `PostToolUse` hook — register it in
`~/.claude/settings.json` (point `CLAUDE_GLOBAL_SKILLS_REPO` at your checkout):

```json
{ "hooks": { "PostToolUse": [ { "matcher": "Edit|Write|MultiEdit|NotebookEdit",
  "hooks": [ { "type": "command",
    "command": "python \"$HOME/Projects/claude-global-skills/hooks/skills_drift_hook.py\"" } ] } ] } }
```

It's non-blocking (always exits 0), target-aware (Claude or Codex), and silenced with
`CLAUDE_SKILLS_DRIFT_HOOK=0`.

### Cross-runtime targets (Claude Code + Codex)

A skill declares where it installs with an optional top-level `targets:` key in its
`SKILL.md` frontmatter:

```yaml
targets: [claude, codex]   # dual-target;  omit the key entirely for Claude-only
```

- **Absent key → `[claude]`** (every existing skill is unchanged — backward compatible).
- **Shared content lives once.** Files unique to one product go under
  `<skill>/overlays/<target>/` and are *added* on top of the shared files at deploy
  time (additive-only). This is how Codex's `agents/openai.yaml` reaches
  `~/.codex/skills/` but is **never** installed into Claude Code.
- **One `SKILL.md`, product-correct commands.** A `{{SKILL_HOME}}` token in shared
  text expands to `$HOME/.claude/skills/<name>` or `$HOME/.codex/skills/<name>` per
  target, so each install shows a runnable, product-correct path.

`monitor-agent-thread` is the reference dual-target skill. Homes are env-overridable
(`CLAUDE_SKILLS_DIR` / `CODEX_SKILLS_DIR`), which is how the test suite runs
hermetically. Full contract: [`SKILL-SPEC.md`](SKILL-SPEC.md); migration from the
previous Claude-only workflow: [`CONTRIBUTING.md`](CONTRIBUTING.md#migration).

## Documentation

- [`CONTRIBUTING.md`](CONTRIBUTING.md) — how to add or change a skill, run the tests, and the review gate
- [`SKILL-SPEC.md`](SKILL-SPEC.md) — the `SKILL.md` contract (frontmatter, directory layout, dependency + privacy rules)
- [`SECURITY.md`](SECURITY.md) — data-handling model, secret scanning, and how to report a vulnerability
- [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md) · [`CHANGELOG.md`](CHANGELOG.md)

## License

[MIT](LICENSE) © 2026 Bryce Watson.

These skills run autonomously, and several read your local Claude chat history.
The license's "AS IS / NO WARRANTY" terms apply — review each skill before you
install it.
