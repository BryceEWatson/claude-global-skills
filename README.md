# claude-global-skills

Version-controlled source for **global `~/.claude/skills/`** skills — the
cross-project, machine-wide skills that are deployed to `~/.claude/skills/` and
must run on any machine with just `python` (no per-project checkout).

> Deploy is a copy step: the canonical source lives here; the live copy lives at
> `~/.claude/skills/<name>/`. This repo is the review surface, not the runtime.

## Skills

| Skill | What it does |
|---|---|
| [`review-globals-loop`](review-globals-loop/) | Mines all local Claude history across every project (both corpora) for operator friction that recurs across **multiple** projects, then proposes the smallest global `~/.claude` change to fix it — each proposal reconciled against what already ships globally and self-validated by an adversarial `/review-loop --mode claim` pass. Proposes only; never writes `~/.claude` itself. |

## Privacy

These skills mine **verbatim cross-project operator turns** — private chat. That
data is written only under each skill's untracked `.local-state/` (git-ignored
here) and is routed through a fail-closed privacy guard
(`lib/_guards.assert_safe_out`) so it can never land in a tracked tree or in
`~/.claude` config. Never commit anything under `.local-state/`.
