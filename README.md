# claude-global-skills

A curated collection of [Claude Code](https://claude.com/claude-code) **skills** —
a multi-agent code-review loop, exhaustive local chat-history search, an
evidence-grounded session end/resume pair, Gemini image generation, and rigorous
transcript retrospectives — that run **machine-wide with nothing but `python` and
`node`**. No per-project install: drop a skill into `~/.claude/skills/` and invoke
it as a slash command in any session.

These are global (`~/.claude/skills/`) skills, version-controlled here so they can
be reviewed, shared, and deployed to a fresh machine. The repo is the source of
truth; the live copy under `~/.claude/skills/<name>/` is a deployed copy.

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
| [`review-loop`](review-loop/) | Dispatches a multi-agent review team over your session's diff, runs an execution-grounded lint/test/build check, validates each finding through a falsifier stage, and posts a commit-pinned verdict on the PR. Ships a Stop-hook + installer. |
| [`gemini-image`](gemini-image/) | Generate and edit images via Google's Gemini API from one zero-dependency Python CLI — reference-image input, multi-image output, safety-block diagnostics, best-available-model selection. |
| [`chat-history-search`](chat-history-search/) | Exhaustively search your local Claude history across both corpora (Claude Code CLI + Cowork/Desktop) — knows every log location and the false-positive gotchas (task-notifications, TodoWrite items, tool results) that trip up naive grep. |
| [`pattern-retrospective`](pattern-retrospective/) | Mine your transcripts for recurring patterns with real rigor: audit-the-target-first discipline, streaming JSONL parse, 5-tuple extraction with provenance, self-falsification, and Krippendorff-α inter-rater checks. |
| [`session-end`](session-end/) | Close out a session into an evidence-grounded record (decisions, claims + verification, assumptions, artifacts, reversals); mid-flight, also emits a ready-to-paste continuation prompt. |
| [`session-pickup`](session-pickup/) | The inverse of `session-end`: rehydrate a continued session from the latest handoff, reconciled against current git/file state before acting. |

### Personal examples — wired to the author's setup; adapt before use

These show real, working patterns but reference the author's own projects, sites,
or companion skills. Read them as reference implementations and adjust the paths,
domains, and assumptions to yours.

| Skill | What it does |
|---|---|
| [`transcript-analysis`](transcript-analysis/) | Single-project transcript miner → proposes that project's `CLAUDE.md` candidates. The single-project sibling of `global-review-loop`. |
| [`seo-index-validation`](seo-index-validation/) | Probe a deployed site's crawl/index health (status codes, redirects, soft-404, sitemap, GSC) and diagnose why pages aren't indexed. A no-auth `bash`+`curl` script plus a playbook. |
| [`global-review-loop`](global-review-loop/) | Mine your whole fleet's history for friction that recurs across projects, then propose global `~/.claude` changes — reconciled against what already ships and self-validated by an adversarial claim loop. (Wired to a project registry; see its SKILL.md.) |
| [`weekly-work-log`](weekly-work-log/) | Build a public weekly work-log page from session-end handoffs + git, with every number re-verified. Wired to the author's site as a worked example. |

> `session-handoff` is a thin alias that routes to `session-end` (the skill was
> renamed); `/session-handoff` still works if it's installed.

## Privacy & safety

Several skills (`chat-history-search`, `transcript-analysis`,
`pattern-retrospective`, `global-review-loop`) read your **private local Claude
chat history**. That data is written only under each skill's git-ignored
`.local-state/`, behind a fail-closed guard that refuses to write into your
`~/.claude` config or any git working tree — so mined data can't land in a tracked
or published tree. Skills also run as executable code (some install hooks), so a
skill is a code-execution surface. **Read [`SECURITY.md`](SECURITY.md) before
installing or contributing**, and review a skill's code before you deploy it.

## How this repo is maintained

Skills are edited where they run (`~/.claude/skills/<name>/`) and captured back here
as reviewed PRs; deploying the reverse direction (`repo → live`) sets up a fresh
machine. The engine is `scripts/sync.py` (stdlib-only). Contributors don't need the
maintainer's live tree — see [`CONTRIBUTING.md`](CONTRIBUTING.md) for the
outside-contributor path (`CLAUDE_GLOBAL_SKILLS_REPO` + `sync.py --deploy`), the
test commands, and the merge gate.

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
