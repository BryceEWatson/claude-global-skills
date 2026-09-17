# Retired skills

Skills that no longer install. `scripts/sync.py` only treats a top-level folder with a `SKILL.md` as a
skill, so a folder here is never deployed, checked for drift, or captured. Nothing here is deleted: to
restore a skill, move its folder back to the repo root and run `python scripts/sync.py --deploy`.

Retiring doesn't uninstall. `--deploy` never removes a live skill, so a machine that already has a retired
skill installed keeps running it until you delete that folder by hand (for example
`~/.claude/skills/session-pickup`). Until then `--check` lists it as an unmanaged live-only skill and
`--capture` suggests adding it to the repo; ignore that suggestion.

- `session-pickup`, retired 2026-09-16. Across every project's chat logs it had run 7 times, while 25
  sessions resumed by pasting the continuation prompt that `session-end` emits. Its reconcile rules now
  travel inside that prompt (`session-end` Step 5, item 7).
- `transcript-analysis`, retired 2026-09-06 and moved here 2026-09-17. A global skills audit that day
  recorded it as never invoked, and the cleanup removed it from `~/.claude/skills`. It stayed at the repo
  root for eleven more days, so `--deploy` would have reinstalled it. Pattern mining across sessions now
  goes through `pattern-retrospective`, which is what `chat-history-search` points to.
