---
name: chat-arch-thrash-detect
description: |
  Calibration-gated PostToolUse hook that watches the rolling window
  of tool calls in a Claude Code session and emits a stderr nudge
  when it detects an Edit-thrash, Read-loop, Test-loop, or Tool-flail
  pattern. NOT a user-invokable skill — the directory exists to host
  the hook script + install/uninstall machinery alongside the rest
  of the chat-arch skill family. Gated behind
  `CHATARCH_THRASH_DETECT=1` for the 4-week calibration window
  (plan §Phase 4 #8).
allowed-tools: []
---

# chat-arch-thrash-detect

This skill is a holder for the PostToolUse hook at
`post-tool-use-hook.cjs`. There is no slash-command surface — the hook
runs automatically on every tool call when the user has run
`install.cjs` AND set `CHATARCH_THRASH_DETECT=1` in their environment.

If you reached this skill via a slash command by mistake, no-op: there
is nothing to invoke here.

## Calibration

Threshold values are duplicated in `thresholds.cjs` to keep the hook
hermetic (no chat-arch package import). The canonical source is
`packages/analysis/src/thresholds.ts` in the chat-arch repo
(`THRESHOLDS.thrash`). When that file changes, mirror the change
here.

Fire events are appended to
`~/.claude/cache/chat-arch-thrash/fires.jsonl` so the calibration pass
can audit fire rate, false-positive rate, and ack-or-pivot follow-up
rate per the plan's pre-registered launch criteria.
