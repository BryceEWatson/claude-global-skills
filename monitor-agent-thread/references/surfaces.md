# Session surfaces

## Local logs

| Product | Main log pattern | Stable identifier |
|---|---|---|
| Claude Code | `~/.claude/projects/**/<session-id>.jsonl` | Claude session UUID / filename |
| Codex | `~/.codex/sessions/YYYY/MM/DD/rollout-...-<thread-id>.jsonl` | Codex thread ID / filename |

Exclude Claude `subagents/` during main-session discovery. Both formats may contain private prompts, tool arguments, hidden reasoning, signatures, and configuration. Use `scripts/thread_watch.py`; do not inspect or relay raw records unless debugging the parser locally, and never expose unsafe fields.

## Preferred live APIs

- In Codex, use thread tools and the automation heartbeat when available. Use local logs to monitor Claude Code or to corroborate Codex state.
- In Claude Code, use `ccd_session_mgmt` tools for Claude sessions and local Codex rollouts for Codex sessions. A Claude-global installation must point to the same portable skill folder or carry an equivalent copy.

## Portable Claude installation

The skill body and watcher script are product-neutral. For Claude Code auto-discovery, install an equivalent skill under its global skills surface and adjust the script path in examples. Command does not silently edit that separately owned surface; use its normal global-skills workflow.

## Terminal-state caveat

`task_complete` means a Codex turn completed, not necessarily that the entire thread objective is done. Claude `end_turn` has the same caveat. Verify the expected artifact, process/worktree state, or explicit final message before reporting the monitored objective complete.
