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
