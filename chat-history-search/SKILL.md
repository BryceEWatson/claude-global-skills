---
name: chat-history-search
description: "Exhaustive search across ALL of your local Claude chat history — Cowork (Claude Desktop local-agent-mode) AND Claude Code CLI — to find user prompts, recover past conversations, inventory how often a pattern was used, or audit prior work. Knows every log location, every JSONL line shape, and the false-positive gotchas (task-notification wrappers, TodoWrite items, tool_result content, audit-log duplicates) that trip up naive grep. Use when the user asks to find all uses of X, when they last said Y, inventory prompts about Z, recover a conversation, or audit their sessions across projects."
---

# Chat History Search

Exhaustive search across the two separate corpora of local Claude chat history.

## Why this skill exists

Claude writes its session transcripts to **two completely separate locations**,
and a naive search misses one of them or drowns in false positives. The Claude
Code CLI logs to `~/.claude/projects/`; Claude Desktop's local-agent-mode
(Cowork) logs to a separate, often ~10× larger corpus under the OS app-data
directory. On top of that, both formats serialize TodoWrite items,
`<task-notification>` wrappers, and tool_result content as `type:"user"`
messages — so they read like typed prompts but are not. This skill encodes the
full corpus map plus the verification rules that reject those false positives,
so the search is right the first time.

## When this skill triggers

User intent that maps here:
- "find all uses of X in my chat history" / "every time I asked Claude to Y"
- "when did I last Z" / "inventory my prompts about W"
- "recover that conversation where we talked about V"
- "audit my sessions for U"
- "search my local logs for T"

## Storage map — search ALL of these

> Paths below use `~` / `$HOME`. On Windows that resolves to `%USERPROFILE%`
> (e.g. `C:\Users\<you>`). The Cowork app-data base differs by OS — see Corpus 2.

### Corpus 1: Claude Code CLI / VS Code (`~/.claude/projects/`)

```
~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl
~/.claude/projects/<encoded-cwd>/<sessionId>/subagents/agent-*.jsonl
```

`<encoded-cwd>` is the working dir path with `:` and `/` replaced by `-`,
lowercased (e.g. `c--Users-you-Projects-example`). One project = one directory;
one session = one JSONL file. Subagent transcripts live in a sibling directory
matching the parent session UUID.

### Corpus 2: Cowork / Claude Desktop local-agent-mode

The app-data base depends on the OS:

```
Windows   %AppData%\Claude\                       ($HOME/AppData/Roaming/Claude/)
macOS     ~/Library/Application Support/Claude/
Linux     ~/.config/Claude/   (or $XDG_CONFIG_HOME/Claude/)
```

Under that base, two top-level session-dir names — the rename is in progress,
both may coexist:

```
<base>/local-agent-mode-sessions/     ← current
<base>/claude-code-sessions/          ← migration target (often empty)
```

Under each:

```
<org-uuid>/<user-uuid>/<sessionId>/
├── agent/local_ditto_<sessionId>/audit.jsonl              ← top-level audit log (rare)
├── local_<cwdUuid>/
│   ├── audit.jsonl                                        ← per-cwd audit log (PRIMARY user-prompt source)
│   ├── .claude/projects/
│   │   ├── -sessions-<processName>/<cliSessionId>.jsonl   ← CLI subprocess session
│   │   └── <encoded-output-path>/<sessionId>.jsonl        ← outputs session (with queue-operation events)
│   │       └── subagents/agent-*.jsonl                    ← subagent transcripts
│   └── outputs/                                           ← (sometimes)
```

There is typically ONE `<sessionId>` directory per user — all Cowork sessions
live under it.

### What lives where — quick reference

| Looking for | Best file to grep | Why |
|---|---|---|
| Your typed prompts (CLI) | `~/.claude/projects/<slug>/*.jsonl` | Direct |
| Your typed prompts (Cowork) | Cowork `outputs/.../*.jsonl` then `audit.jsonl` | Outputs has `enqueue` events with verbatim typed text; audit has same prompts deduped by event type |
| Subagent task descriptions | `subagents/agent-*.jsonl` OR `<task-notification>` blocks inside parent | These are NOT your prompts |
| Tool / system events | CLI subprocess files (`-sessions-<processName>/`) | Mostly tool_results when called from Cowork |
| Session cost / duration | `audit.jsonl` lines with `total_cost_usd` or `type:"result"` | Per-session totals |
| Skill / hook activity | Filter by `attributionSkill` field | Tags loop/review-loop/etc invocations |

## JSONL line shapes — what to parse, what to skip

### User-message shapes (the GOAL — keep these)

**CLI / Cowork CLI-subprocess sessions** — content is usually an ARRAY of blocks:
```json
{"type":"user","message":{"role":"user","content":[{"type":"text","text":"<typed prompt>"}]},"timestamp":"..."}
```

**Cowork audit.jsonl** — content is usually a STRING:
```json
{"type":"user","session_id":"...","message":{"role":"user","content":"<typed prompt>"},"_audit_timestamp":"..."}
```

**Cowork outputs `enqueue` events** — content is a STRING at top level:
```json
{"type":"queue-operation","operation":"enqueue","timestamp":"...","content":"<typed prompt>"}
```

### Look-alikes you must REJECT (false-positive sources)

These all serialize as `"type":"user"` or contain the search term but are NOT the user typing:

| Pattern | How to detect | Why it's not a user prompt |
|---|---|---|
| `tool_result` | Content array contains `{"tool_use_id":"...","type":"tool_result","content":"..."}` | This is a tool execution result, not typed input |
| `<task-notification>` wrapper | Content text starts with `<task-notification>\n<task-id>...` | This is a subagent completion notification — the embedded task description is what THE ASSISTANT told the subagent to do, not what the user typed |
| `TodoWrite` tool input | `message.content` contains `{"type":"tool_use","name":"TodoWrite","input":{"todos":[...]}}` | These are todo items the assistant wrote — common to find search-term hits inside `content`/`activeForm` strings |
| `attachment` events | `"type":"attachment"` at top level (todo_reminder, ide_selection, file_contents) | System-injected context, not typed input |
| `system` events | `"type":"system"` | Init events, system prompts |
| Document content via Read tool | The hit is inside a tool_result string that contains file body text | The project has docs that mention the term |
| `isSidechain: true` | Top-level field | Background operations, not main conversation |
| `isApiErrorMessage: true` | Top-level field | API errors, not conversation |
| Subagent task descriptions | Whole file is under `subagents/agent-*.jsonl` | First line is the assistant's instruction to the subagent, subsequent are the subagent's own turns |

## Search strategy — the order that works

### Step 1 — enumerate, don't assume

```bash
# All CLI jsonl files
ls "$HOME"/.claude/projects/*/*.jsonl

# All Cowork jsonl files (use find — directory is deep).
# COWORK_BASE is the OS app-data dir from the Storage map above, e.g.
#   Windows: "$HOME/AppData/Roaming/Claude"
#   macOS:   "$HOME/Library/Application Support/Claude"
#   Linux:   "$HOME/.config/Claude"
find "$COWORK_BASE/local-agent-mode-sessions/" -name "*.jsonl"

# Also check the rename target (usually empty during migration but verify)
find "$COWORK_BASE/claude-code-sessions/" -name "*.jsonl"
```

Count files containing the search term BEFORE deciding scope:
```bash
find <base> -name "*.jsonl" | xargs grep -l "<term>" 2>/dev/null | wc -l
```

### Step 2 — categorize Cowork hits before reading

Cowork files split into 4 buckets — search priority differs:

```bash
# Build file list once (TMP = a temp dir: $TMPDIR on macOS/Linux, %TEMP% on Windows)
find "$COWORK_BASE/local-agent-mode-sessions/" -name "*.jsonl" > "$TMP/all.txt"
grep -l "<term>" $(cat "$TMP/all.txt") > "$TMP/hits.txt"

# Categorize
grep "audit[0-9]*\.jsonl$"        "$TMP/hits.txt" > "$TMP/audit.txt"     # PRIMARY user-prompt source
grep "/-sessions-"                 "$TMP/hits.txt" > "$TMP/cli_sub.txt"   # mostly tool_results (low value for prompts)
grep "/subagents/agent-"           "$TMP/hits.txt" > "$TMP/subagent.txt"  # subagent transcripts (skip for user-prompt search)
grep -v "audit\|/-sessions-\|/subagents/" "$TMP/hits.txt" > "$TMP/output.txt"  # outputs/*.jsonl — CLEANEST verbatim
```

For finding the user's typed prompts: scan `output.txt` first (cleanest),
`audit.txt` second (covers older sessions before output logs existed), skip
`subagent.txt` entirely, skip `cli_sub.txt` unless you specifically want
tool-call patterns.

### Step 3 — parallel agent dispatch

For scopes over ~30 files, dispatch parallel Explore agents (NOT general-purpose) with each agent's file list pre-staged as a temp file. Explicitly tell each agent:
1. The exact files they own (path to a manifest text file)
2. The line shape they'll see (CLI array vs audit string vs enqueue string)
3. The false-positive checklist above
4. To return: file basename, timestamp, verbatim excerpt ≤300 chars, 1-line label
5. To order results by timestamp

### Step 4 — DEDUPE across audit + outputs

The Cowork audit log fires 2–3 events per typed prompt (queue + dispatch + run), so audit counts over-count by ~2-3x. The outputs file's `enqueue` event is the canonical "what the user typed" record. When both report the same prompt:
- Use the outputs version's verbatim text
- Use the audit version only when no outputs file exists (older sessions, before output logs existed)

### Step 5 — VERIFY before reporting

Spot-check agent reports against raw grep before trusting them. The two most common agent failures:
1. Reporting `<task-notification>` content as user prompts (subagent task descriptions, not typed input)
2. Reporting TodoWrite item strings as user prompts (assistant-generated)

Quick verifier — extract only direct typed prompts from a single file:

```bash
grep -n "<term>" <file> | \
  grep '"type":"user"' | \
  grep -v 'tool_result\|tool_use_id\|task-notification\|TodoWrite' | \
  grep -oE '"(text|content)":"[^"]{0,250}'
```

Note: if a file uses content-as-array (CLI shape), the verifier needs `"text":"..."` not `"content":"..."`. If audit shape (string), use `"content":"..."`. Try both.

## Issues encountered — explicit gotchas to avoid

Common failure modes, called out so a search skips the wrong turns:

1. **Missing the Cowork corpus entirely.** It is easy to search only `~/.claude/projects/` and treat that as the sole log location. Reality: Cowork writes a completely separate, ~10× larger corpus to the OS app-data dir. ALWAYS scan both.

2. **`<task-notification>` blocks misread as user prompts.** Cowork wraps subagent-completion notifications inside `type:user` messages. The embedded task description is what the assistant told the subagent, not what the user typed. Five hits of one phrase in a single file can turn out to be a single typed prompt plus four subagent task descriptions.

3. **TodoWrite item content misread as user prompts.** When Claude writes a todo list during `/loop` execution, items appear inside `type:user` events (as `tool_result` confirmations). Strict tool_use-id filtering catches them.

4. **CLI session files use content-as-array, audit uses content-as-string.** `grep '"content":"<term>"'` misses CLI matches because the user text lives inside `"content":[{"type":"text","text":"<term>..."}]`. Use a regex that handles both shapes, or run the grep against unpacked text.

5. **Audit duplicates inflate counts.** A single typed prompt produces 2–3 audit events (often visible as near-identical timestamps within 100ms). De-dupe by `(session_id, message.content, ±1s)` or just prefer the outputs file's `enqueue` event.

6. **Cowork CLI-subprocess sessions are mostly tool_results.** The `.claude/projects/-sessions-<processName>/<id>.jsonl` files contain Cowork→Claude-Code subprocess transcripts. The `type:user` entries there are almost entirely tool_results being passed back to the assistant — not the user typing. These files typically yield zero genuine user prompts.

7. **Subagent transcript files have no user prompts.** In `subagents/agent-*.jsonl`, the first line is the assistant's task description to the subagent; subsequent lines are the subagent's own turns. Skip entirely when searching for the user's prompts.

8. **`subagents/` lives inside the parent CLI session directory** (NOT as a sibling). Path: `<encoded-cwd>/<sessionId>/subagents/agent-*.jsonl`.

9. **The directory rename is in progress** — `local-agent-mode-sessions` → `claude-code-sessions`. Check both; the rename target is often still empty, with all data under the old name.

10. **Project names with `-validation`, `-worktree-<adjective>-<noun>-<hash>` suffixes** are separate session corpora (validation runs, worktree sessions). Include them when sweeping a project's history — they may have unique sessions not in the main project directory.

## Output format

When reporting findings to the user, group by corpus and present chronologically:

```
## Claude Code CLI (~/.claude/projects/)
1. <project>/<session>.jsonl  YYYY-MM-DD  "<verbatim excerpt>"   <use-case label>
2. ...

## Cowork (<app-data>/Claude/local-agent-mode-sessions/)
1. local_<uuid>/.../outputs/<id>.jsonl  YYYY-MM-DD  "<verbatim excerpt>"   <use-case label>
2. ...

## Coverage notes
- Files scanned: <N> CLI + <N> Cowork = <total>
- False positives excluded: <list of categories>
- Anything not covered: <e.g., subagent transcripts skipped because they don't contain user prompts>
```

End with a sentence confirming whether the search exhaustively covered all available local history (so the user doesn't have to re-ask).

## What this skill does NOT do

- Pattern mining across sessions (use `transcript-analysis` for that — it has a different purpose: finding recurring instructions / loops / failures within a project's sessions)
- Cross-session summary or retrospective reports
- Real-time monitoring
- Searching cloud-side conversation history at claude.ai (not on disk)
- Recovering deleted sessions (none of these formats retain deletes)
