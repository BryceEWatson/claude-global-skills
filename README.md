# claude-global-skills

Version-controlled source for **global `~/.claude/skills/`** skills — the
cross-project, machine-wide skills that are deployed to `~/.claude/skills/` and
must run on any machine with just `python` (no per-project checkout).

> Deploy is a copy step: the canonical source lives here; the live copy lives at
> `~/.claude/skills/<name>/`. This repo is the review surface, not the runtime.

## Skills

| Skill | What it does |
|---|---|
| [`global-review-loop`](global-review-loop/) | Mines all local Claude history across every project (both corpora) for operator friction that recurs across **multiple** projects, then proposes the smallest global `~/.claude` change to fix it — each proposal reconciled against what already ships globally and self-validated by an adversarial `/review-loop --mode claim` pass. Proposes only; never writes `~/.claude` itself. |
| [`review-loop`](review-loop/) | Multi-agent review team over the session's work + an execution-grounded lint/test/build check, validated through a falsifier stage and re-reviewed until clean or budget hits; records a commit-pinned verdict on the PR. Ships its Stop-hook + install/uninstall machinery. |
| [`chat-history-search`](chat-history-search/) | Exhaustive search across ALL local Claude chat history (Cowork + Claude Code CLI) — find prompts, recover conversations, inventory pattern usage, audit prior work; knows every log location and false-positive gotcha. |
| [`pattern-retrospective`](pattern-retrospective/) | Rigorous pattern-retrospective analysis of Claude transcripts: target-system audit before specifying requirements, streaming JSONL parse, 5-tuple extraction with provenance, self-falsification, Bayesian confidence. Ships `lib/` helpers. |
| [`transcript-analysis`](transcript-analysis/) | Single-project Cowork transcript miner → proposes that project's `CLAUDE.md` candidates. (The single-project sibling of `global-review-loop`.) |
| [`session-end`](session-end/) | Close out a session with an evidence-grounded record (decisions, claims + verification, assumptions, artifacts, reversals); when mid-flight, also captures resumable state + a ready-to-paste continuation prompt. |
| [`session-handoff`](session-handoff/) | Alias of `session-end` (renamed); routes old muscle memory to the handoff (mid-flight) mode. |
| [`session-pickup`](session-pickup/) | Inverse of `session-end`: rehydrate a continued session from the latest handoff, reconciled against current git/file state before acting. |
| [`chat-arch-thrash-detect`](chat-arch-thrash-detect/) | Calibration-gated PostToolUse hook that nudges on Edit-thrash / Read-loop / Test-loop / Tool-flail patterns. Hook host (not user-invokable); ships its install machinery. |

## Privacy

These skills mine **verbatim cross-project operator turns** — private chat. That
data is written only under each skill's untracked `.local-state/` (git-ignored
here) and is routed through a fail-closed privacy guard
(`lib/_guards.assert_safe_out`) so it can never land in a tracked tree or in
`~/.claude` config. Never commit anything under `.local-state/`.

## Keeping the repo in sync

Model: **live-authoritative + capture.** Skills are edited where they run
(`~/.claude/skills/<name>/`); this repo is the durable, reviewed mirror. Changes
flow **live → repo** (capture) as reviewed PRs; the reverse (**repo → live**,
deploy) is the secondary path for a fresh machine or a rollback.

`scripts/sync.py` is the engine (stdlib-only; never copies `.local-state/`,
`__pycache__`, `*.pyc`; line-ending-insensitive so a git `autocrlf` working tree
doesn't show false drift):

```bash
python scripts/sync.py --check     # report drift live ↔ repo (read-only; exit 3 if drift)
python scripts/sync.py --capture   # copy live → repo working tree + cruft/secret pre-scan
                                   # (leaves git add/commit/PR to you, so it lands as a reviewed PR)
python scripts/sync.py --deploy    # copy repo → live (fresh machine / rollback; preserves live .local-state/)
```

After `--capture`, review the diff, commit on a `sync/…` branch, open a PR, and run
`/review-loop` for the verdict — same discipline as any change here.

**Drift-detector hook.** `hooks/skills_drift_hook.py` is a PostToolUse hook: after
you edit a file under `~/.claude/skills/`, it runs `sync.py --check` and, if that
skill has drifted from the repo, prints a one-line stderr nudge to capture it.
Non-blocking (always exits 0). Install by adding to `~/.claude/settings.json`:

```jsonc
{
  "hooks": {
    "PostToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit|NotebookEdit",
        "hooks": [
          { "type": "command",
            "command": "python \"C:/Users/Bryce/Projects/claude-global-skills/hooks/skills_drift_hook.py\"" }
        ]
      }
    ]
  }
}
```

Silence it with `CLAUDE_SKILLS_DRIFT_HOOK=0`; point it at a non-default checkout
with `CLAUDE_GLOBAL_SKILLS_REPO=<path>`.

## Enabling the review-loop Stop hook

`review-loop` ships its own **Stop hook** (`review-loop/stop-hook.cjs`) plus an
idempotent installer — so the hook is shared from this repo, not hand-wired.
After the skill is deployed (`sync.py --deploy`, or on the machine where it
already lives), wire it into `~/.claude/settings.json`:

```bash
node ~/.claude/skills/review-loop/install.cjs    # enable  (bakes an absolute path; strips any legacy entry)
node ~/.claude/skills/review-loop/uninstall.cjs  # disable (surgical removal; preserves unrelated hooks)
```

The installer resolves an **absolute** path via `os.homedir()` at install time, so
the entry is correct on any machine — Claude Code's shell-form hooks don't reliably
expand `${HOME}` on Windows, which would otherwise leave the hook silently dead.

Once enabled, the hook is a cheap **always-on gate**: on every session stop it does
a no-LLM check and only escalates to the review loop when the change warrants it
(skips docs/handoffs/scratch-only and already-reviewed diffs), logging every
decision to `review-loop/.local-state/hook.log`. Controls:

- skip one run: `touch ~/.claude/skills/review-loop/.skip-next`
- opt a repo out: `<repo>/.claude/review-loop.disabled`
- tune what counts as reviewable: `<repo>/.claude/review-loop.code-exts`, `…/review-loop.plan-paths`

## License

[MIT](LICENSE) © 2026 Bryce Watson.

These skills run autonomously, and several read your local Claude chat history.
The license's "AS IS / NO WARRANTY" terms apply — review each skill before you
install it.
