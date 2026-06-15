---
name: transcript-analysis
description: "Analyze Cowork session transcripts for recurring narrative patterns — repeated instructions, planning loops, failure patterns, decision re-litigation, and knowledge re-establishment. Produces a structured report with CLAUDE.md candidates, unresolved loops, and pattern frequency data. Use this skill when the user wants to review what keeps repeating across sessions, extract lessons from past work, audit their collaboration patterns, or identify rules that should be encoded permanently. Triggers on: transcript analysis, session analysis, narrative analysis, what keeps repeating, review our sessions, cowork analysis, pattern analysis, what should go in CLAUDE.md, extract lessons, session retrospective, retro, session patterns."
disable-model-invocation: true
---

# Transcript Analysis — Recurring Narrative Pattern Mining

Analyze all Cowork session transcripts for the current project to identify recurring patterns — repeated instructions, planning loops, failure regressions, decision re-litigation, and knowledge that has to be rebuilt each session. Produces a structured report with actionable recommendations.

## Why This Skill Exists

After enough sessions, behavioral patterns emerge that are invisible within any single conversation: rules the user keeps re-stating, mistakes that keep recurring, planning discussions that never resolve, and context that has to be rebuilt every time. This skill surfaces those patterns so the important ones can be encoded into CLAUDE.md (or skills) and the rest can be consciously accepted or resolved.

## When to Use This Skill

- The user asks "what keeps repeating across our sessions?"
- After a significant milestone (10+ sessions, major feature complete, quarterly review)
- When CLAUDE.md feels stale or incomplete
- When the user suspects the same mistakes keep happening
- Before a major architecture or workflow change (to understand current pain points)

## Inputs

- **Project folder path** — defaults to the current working directory. Used to filter sessions by `userSelectedFolders` in Cowork manifests.
- **Optional: keyword filter** — additional terms to match beyond folder path (e.g., brand names, product terms, tool names). The user can provide these or you can derive them from the project's CLAUDE.md / README.
- **Optional: date range** — restrict analysis to a specific period (e.g., "last 30 days", "since March 1")

## Prerequisites

### Cowork Transcripts

Cowork session transcripts live at (Windows):

```
%AppData%\Claude\local-agent-mode-sessions\   (current name)
%AppData%\Claude\claude-code-sessions\        (legacy name — check both)
```

The directory structure is:
```
<base>/<org-id>/<user-id>/
  local_<session-uuid>.json          ← session manifest (title, processName, timestamps, cliSessionId)
  local_<session-uuid>/
    audit.jsonl                       ← session cost, duration, turns
    .claude/projects/-sessions-<processName>/
      <cliSessionId>.jsonl            ← primary transcript
      <cliSessionId>/subagents/       ← subagent transcripts (optional)
```

### Claude Code CLI Transcripts

CLI transcripts live at (Windows):

```
%USERPROFILE%\.claude\projects\<project-slug>\<session-id>.jsonl
```

Where `<project-slug>` is the working directory path with path separators replaced by dashes (e.g., `c--Users-Bryce-Projects-MyApp`). These use the same JSONL schema as Cowork transcripts.

### Platform Detection

Use `os.path.expandvars()` for Windows env vars or `os.path.expanduser("~")` for Unix-style paths. Check both Cowork and CLI paths; analyze whichever has data.

---

## Workflow

### Phase 1: Discover and Inventory Sessions

#### Step 1.1: Locate Transcript Sources

```python
import os, glob

# Cowork transcripts (try both directory names)
appdata = os.environ.get('APPDATA', os.path.expanduser('~/.config'))
cowork_candidates = [
    os.path.join(appdata, 'Claude', 'local-agent-mode-sessions'),
    os.path.join(appdata, 'Claude', 'claude-code-sessions'),
]
cowork_base = next((c for c in cowork_candidates if os.path.isdir(c)), None)

# CLI transcripts
home = os.path.expanduser('~')
cli_base = os.path.join(home, '.claude', 'projects')
```

For Cowork: enumerate `<cowork_base>/<org-id>/<user-id>/` to find the user directory (usually one org-id and one user-id).

For CLI: enumerate project slug directories matching the current project path.

#### Step 1.2: Read All Manifests (Cowork)

For each `local_*.json` file in the user directory, extract:
- `sessionId` — unique session identifier
- `processName` — the VM process name (human-readable slug)
- `cliSessionId` — the CLI session UUID (needed to find the transcript JSONL)
- `title` — session title (most useful for filtering)
- `createdAt` — timestamp in milliseconds
- `userSelectedFolders` — array of workspace paths the session had access to
- `initialMessage` — the first user message (useful for filtering)

#### Step 1.3: Filter to Project-Relevant Sessions

Match sessions where ANY of these are true:
1. `userSelectedFolders` contains the project folder path (case-insensitive)
2. `title` contains any of the project keyword filters
3. `initialMessage` contains any of the project keyword filters

**Keyword selection matters.** Use project-specific terms: product names, brand names, platform names, tool names, workflow names. Cast a wide net — it's better to include marginal sessions than miss relevant ones.

**Deriving keywords:** If the user doesn't provide keywords, scan the project's CLAUDE.md, README.md, and package.json/pyproject.toml for project names, tool names, and domain terms. Use these as the keyword filter.

#### Step 1.4: Enrich with Audit Data

For each matching Cowork session, read `<session-dir>/audit.jsonl`. Each line is a JSON object. Look for the line with `type: "result"` or containing `total_cost_usd` — it has:
- `total_cost_usd` — session cost
- `duration_ms` — wall-clock duration
- `num_turns` — conversation turns

CLI sessions don't have audit data — skip this step for CLI transcripts.

#### Step 1.5: Locate Transcripts

For Cowork sessions with a `cliSessionId`, find the transcript at:
```
<session-dir>/.claude/projects/-sessions-<processName>/<cliSessionId>.jsonl
```

Sessions without a `cliSessionId` are failed/aborted — skip them.

For CLI sessions, the transcript IS the JSONL file in the projects directory.

#### Step 1.6: Print Inventory Table

Output a table sorted by date descending:
```
Date              Title                                              Cost    Mins  Turns  Source
──────────────────────────────────────────────────────────────────────────────────────────────────
2026-03-17 09:42  Experiment review mar17                           $0.60     0.9     2   Cowork
2026-03-16 14:30  Fix auth bug                                       n/a     n/a    12   CLI
...
```

Include summary stats: total sessions, sessions with transcripts, total cost (Cowork only), total hours.

---

### Phase 2: Extract Conversation Content

For each transcript JSONL file:

#### Step 2.1: Parse Lines

Each line is a JSON object with a `type` field. Process based on type:

| `type` | Include? | Content location |
|--------|----------|-----------------|
| `user` | Yes | `message.content` (array of blocks or string) |
| `assistant` | Yes | `message.content` (array of blocks) |
| `system` | Situational | Usually skip — contains CLAUDE.md/system prompts |
| `queue-operation` | No | Internal plumbing |

**Exclude lines where:**
- `isApiErrorMessage` is `true` (API errors, not conversation)
- `isSidechain` is `true` (background operations)

#### Step 2.2: Extract Text

For content that is an array of blocks:
```python
def extract_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(
            block.get('text', '')
            for block in content
            if isinstance(block, dict) and block.get('type') == 'text'
        )
    return ''
```

#### Step 2.3: Clean and Truncate

- Strip `<scheduled-task>...</scheduled-task>` blocks (boilerplate)
- Strip `<system-reminder>...</system-reminder>` blocks (system noise)
- Strip `<ide_selection>...</ide_selection>` blocks (editor context)
- Strip `<file_contents>...</file_contents>` blocks (file dumps)
- Truncate individual messages to ~1500 chars (preserve signal, reduce volume)
- Skip messages shorter than 10 chars (empty confirmations)

#### Step 2.4: Handle Subagents (Optional)

If `<cliSessionId>/subagents/` exists, parse each `agent-*.jsonl` using the same schema. Subagent transcripts are usually short but may contain delegated decision-making. Include them in the analysis tagged as subagent content.

---

### Phase 3: Pattern Extraction

Analyze ALL extracted conversations across ALL sessions looking for six categories of recurring patterns. Use keyword matching as a first pass, then review matches for thematic clustering.

#### Category A: Repeated Instructions & Rules

Things the user keeps telling Claude that should be in CLAUDE.md or skills:

| Signal pattern | What it indicates |
|---------------|-------------------|
| "make sure", "always", "never", "remember to" | Instruction the user has given before |
| "don't forget", "please don't", "stop doing" | Correction of a repeated mistake |
| "I told you", "I said", "we discussed" | Re-establishment of a prior instruction |
| "update the state", "update session" | Process step being forgotten |
| "use the skill", "read the skill first" | Skill bypass |
| Repeated file path or format corrections | Convention not encoded |

**Cluster by topic** — group related corrections together (e.g., all path-related corrections, all state-update reminders).

#### Category B: Planning Loops

Topics where the project keeps re-planning without resolving:

| Signal pattern | What it indicates |
|---------------|-------------------|
| "what should we do next", "priority", "what's next" | Re-prioritization loop |
| "let's plan", "create a prompt for next session" | Session handoff without execution |
| Session titles containing "plan", "next steps", "priorities" | Dedicated planning sessions |
| Same topic appearing in nextSteps across 3+ sessions | Unresolved planning item |

**Frequency threshold:** A topic becomes a "loop" when it appears in 3+ sessions without a resolution artifact (committed code, published output, completed task).

#### Category C: Failure & Recovery Patterns

Mistakes or system failures that repeat:

| Signal pattern | What it indicates |
|---------------|-------------------|
| "I thought we solved/fixed this" | Regression |
| "this keeps happening", "same issue", "again" | Recurring failure |
| "crashed", "doesn't work", "never responds" | Tool/system failure |
| "wrong browser", "wrong account" | Environment setup error |
| Context continuation messages | Context overflow (session was too long) |

**Track regression vs. new failure** — regressions (things that were fixed but broke again) are higher priority than new failures.

#### Category D: Decision Narratives

Key decisions and rationale that keep being re-explained:

| Signal pattern | What it indicates |
|---------------|-------------------|
| "we chose X because", "the reason is" | Decision rationale being restated |
| "pricing", "strategy", "experiment" | Strategic decision under discussion |
| "API", "integration", "platform" | Technical decision being revisited |
| Same decision topic in 3+ sessions | Decision not yet landed |

#### Category E: Knowledge Re-establishment

Context that has to be rebuilt each session:

| Signal pattern | What it indicates |
|---------------|-------------------|
| "what's our status", "where are we" | State unknown at session start |
| "where is the file", "which config" | File location unknown |
| "how does X work", "remind me" | System knowledge lost |
| Context continuation summaries | Full context rebuild |

**Count continuation messages** — each "This session is being continued from a previous conversation that ran out of context" represents a context rebuild event.

#### Category F: Emotional & Momentum Patterns

Tone shifts that recur:

| Signal pattern | What it indicates |
|---------------|-------------------|
| "looks great", "perfect", "nice" | Positive momentum |
| "frustrating", "annoying", "why does" | Frustration trigger |
| "let's just", "forget it", "move on" | Giving up on current approach |
| Session ending with task completion confirmation | Productive session |
| Session ending with "what's next?" | Momentum tapering |

---

### Phase 4: Compile Report

Write the report to a `research/studies/` directory (create if needed) in the project workspace as `{YYYY-MM-DD}_narrative-analysis.md`. If no `research/studies/` directory exists, write to the project root.

Include these sections:

#### Section 1: Session Inventory
The table from Phase 1, plus summary stats (total sessions, cost, hours, date range).

#### Section 2: Top 10 Recurring Narratives
Ranked by frequency (number of unique sessions where the pattern appears):
- **Pattern name** — short descriptive label
- **Category** — A through F
- **Session count** — how many unique sessions exhibit this pattern
- **Representative quotes** — 1-2 short verbatim excerpts from user messages, each tagged with a provenance pointer `<cliSessionId>/<lineIndex>` (see **Evidence Discipline**)
- **Recommendation** — one of:
  - "CLAUDE.md candidate" — encode as a permanent rule
  - "Skill candidate" — encode as a procedural skill or slash command
  - "Decision needed" — requires a one-time decision to stop the loop
  - "Structural" — caused by platform limitations, can only be mitigated
  - "Acceptable" — natural pattern, no action needed

#### Section 3: CLAUDE.md Candidates
Specific rules extracted from Category A, formatted as ready-to-paste CLAUDE.md entries. Each should include:
- The rule text
- Why it matters (what goes wrong without it)
- How many sessions it appeared in

#### Section 4: Skill Candidates
Patterns that are too procedural for CLAUDE.md and should be skills instead. For each:
- What the skill would do
- Which existing skill it could extend (if any)
- Why a CLAUDE.md rule is insufficient

#### Section 5: Unresolved Loops
From Category B, decisions or plans that keep cycling. For each:
- The topic
- How many sessions it's appeared in
- What would resolve it (a decision, an experiment, acceptance)

#### Section 6: Raw Pattern Log
Every pattern identified with:
- Pattern name
- Category
- Session count
- Key signal phrases

---

## Evidence Discipline — Provenance, Privacy & Dedup

This skill lifts **verbatim transcript excerpts** into durable artifacts — the
report under `research/studies/` and the findings ledger — that often live in a
committed (sometimes deployed) repo. The discipline below **adapts** `story-miner`'s
mining mechanics to this prompt-only skill; where it diverges from story-miner
(which does the work in a preprocessor), that is called out. It applies whenever a
quote or finding leaves the raw transcript (Phases 3-4).

**1. Provenance-grounded quoting.** Tag every surfaced quote with a pointer to its
source line so the quote is *checkable*: `<cliSessionId>/<lineIndex>` — `cliSessionId`
because that is what names the transcript file (`<cliSessionId>.jsonl`; for a CLI
session the session id IS the file name), and `<lineIndex>` the 0-based line number
in that JSONL. **Capture the line index while parsing in Phase 2.1** — you are
already iterating the lines. story-miner additionally suffixes `#<contentHash16>`
(the first 16 hex of a SHA-256 over the first ~400 chars of that line's extracted (redacted, truncated) text, computed by
its preprocessor); replicate that suffix only if you compute the hash
programmatically, otherwise the `<cliSessionId>/<lineIndex>` pair is the floor. The
pointer turns the Anti-Pattern Checklist's "actual user messages, not paraphrases"
from an aspiration into something verifiable. *Caveat:* Phase 2 joins/strips blocks
and truncates to ~1500 chars, so a quote is verbatim against the **extracted** text
of that line, not necessarily a byte-substring of the raw line. Record the pointer
next to the quote in both the report and the ledger; a quote you cannot point to
does not go in the report.

**2. Redact before surfacing.** story-miner gates every output through a
deterministic secret/PII scanner; replicate the **posture**, not its exact regexes.
Before writing ANY verbatim excerpt, strip at least: provider tokens/keys (`sk-…`,
`ghp_…`, `github_pat_…`, `glpat-…`, `AKIA…`, `AIza…`, `xox[baprs]-…`), JWTs, PEM
private-key blocks, `Authorization:`/`Cookie:` headers, DB connection strings, and
long hex/base64 runs; and neutralize home-directory paths (`C:\Users\<name>\…`,
`/home/<name>/…`, `/Users/<name>/…`) so a username never leaks. Redaction is
best-effort — a floor, not a guarantee.

**3. Scan the output before finalizing.** After drafting the report + ledger, scan
the written files for the patterns above (a `Grep` pass suffices). On any hit,
redact and re-scan until clean. **Never finalize an unscanned report** — this is the
prompt-only analogue of story-miner's gated `--scan-dir` step (which exits non-zero
until clean).

**4. Never quote model-internal or system text.** Surface quotes only from
`user`/`assistant` **text** blocks — the qualifier is on the *block*, not the line
(one `user` line can carry hundreds of `tool_result` blocks). **From story-miner:**
never quote `thinking`/reasoning content, and cross-check the guard — a quote's
provenance must resolve to a text block, not a thinking block. **Reinforcing this
skill's own Phase 2.3 stripping:** never quote `<system-reminder>`,
`<scheduled-task>`, `<ide_selection>`, `<file_contents>`, or tool output either.

**5. Deterministic dedup before counting.** A pattern's session count is
load-bearing — it drives ranking and the 2+/3+ thresholds — so don't let one
friction, surfaced under two labels, count twice. Before counting, merge
near-identical findings deterministically: extract each finding's rare key-terms
(file paths, function names, error-class names, minus a common-word stoplist) and
treat two findings as the same pattern when they share **>= 3** rare terms
(story-miner's default `minRareTermOverlap`; drop to 2 for short, term-sparse
findings). This complements the LLM thematic clustering in Phase 3 with a
deterministic merge.

---

## Scaling Notes

### Large Session Counts (100+ sessions)

Transcripts can be large. Process in batches if context limits are hit:
1. Extract and save condensed data to a temporary `transcript-extracts.json` file first
2. Then analyze the condensed data for patterns
3. User messages are the highest-signal content — prioritize those over assistant messages for pattern detection

### Incremental Analysis

If a previous narrative analysis report exists in the project, read it first and focus on sessions that postdate it. The new report should note which sessions are new since the last analysis and highlight any patterns that have changed (new, resolved, or intensified).

---

## Intermediate Data Files

The analysis pipeline may produce intermediate files for inspection:

| File | Purpose | Cleanup? |
|------|---------|----------|
| `data/session-inventory.json` | All matching sessions with metadata and paths | Delete after report or keep for incremental runs |
| `data/transcript-extracts.json` | Condensed conversation text from all sessions | Delete after report — can be large |
| `data/user-messages.json` | User messages only (highest signal) | Delete after report |

Ask the user if they want these cleaned up after the report is generated.

---

## Findings Ledger — Tracking State Across Runs

The core mechanism for avoiding repeated work is the **findings ledger**: a JSON file that persists in the project and tracks each finding's lifecycle across analysis runs.

### Ledger Location

```
.claude/transcript-analysis/findings-ledger.json
```

This lives in the project's `.claude/` directory (not the user's `~/.claude/`) because findings are project-specific. The `.claude/` directory is the standard project-level config location for Claude Code.

### Ledger Schema

```json
{
  "version": 1,
  "project": "ShopForge",
  "runs": [
    {
      "date": "2026-03-17",
      "sessions_analyzed": 211,
      "session_date_range": ["2026-02-11", "2026-03-17"],
      "report_path": "research/studies/2026-03-17_narrative-analysis.md"
    }
  ],
  "findings": [
    {
      "id": "A_windows_file_path",
      "category": "A",
      "title": "Windows host path for file uploads",
      "status": "actioned",
      "first_seen": "2026-03-17",
      "last_seen": "2026-03-17",
      "session_count": 3,
      "action_taken": "Added to post-next-pin/SKILL.md; propagated to compositor and publish-v2 skills",
      "action_date": "2026-03-18",
      "notes": "Recurrence should drop to zero — monitor in next run",
      "quotes": [
        {"ref": "<cliSessionId>/<lineIndex>", "text": "Make sure you use the local windows file path, not your VM file path"}
      ]
    }
  ],
  "last_session_date": "2026-03-17T09:42:00Z"
}
```

### Finding Statuses

| Status | Meaning | Next run behavior |
|--------|---------|-------------------|
| `new` | Just discovered this run | Present in report as new finding |
| `acknowledged` | User has seen it but hasn't acted yet | Present as "still open" with session count delta |
| `actioned` | User encoded it in CLAUDE.md, a skill, or made a decision | Check if pattern still recurs in new sessions. If yes → `persistent`. If no → `resolved` |
| `resolved` | Was actioned AND no longer appears in new sessions | Skip entirely — only mention in summary stats |
| `persistent` | Was actioned but pattern still recurs in new sessions | Flag prominently — the fix didn't work |
| `accepted` | User decided this is fine and doesn't want to see it again | Skip entirely |
| `deferred` | User wants to revisit later | Present only in summary, not in main findings |

### Phase 0: Load Ledger (NEW — runs before Phase 1)

Before discovering sessions, check for an existing ledger:

```python
import os, json

ledger_path = os.path.join('.claude', 'transcript-analysis', 'findings-ledger.json')

if os.path.exists(ledger_path):
    with open(ledger_path) as f:
        ledger = json.load(f)
    last_run_date = ledger['runs'][-1]['date'] if ledger['runs'] else None
    last_session_date = ledger.get('last_session_date')
    known_findings = {f['id']: f for f in ledger['findings']}
    print(f"Previous run: {last_run_date}, {len(known_findings)} known findings")
else:
    ledger = {"version": 1, "project": os.path.basename(os.getcwd()), "runs": [], "findings": [], "last_session_date": None}
    last_run_date = None
    last_session_date = None
    known_findings = {}
```

**If a ledger exists with `last_session_date`:**
- In Phase 1, still discover ALL sessions (for accurate counts), but mark which are "new since last run"
- In Phase 3, analyze only new sessions for pattern detection, then merge with existing findings
- In Phase 4, generate a differential report (see below)

### Phase 3 Changes: Incremental Pattern Extraction

When a ledger exists, pattern extraction works differently:

1. **Run pattern detection on ALL sessions** (not just new ones) to get updated session counts
2. **For each detected pattern, check the ledger:**
   - If `id` matches a known finding → update `last_seen` and `session_count`
   - If the known finding is `actioned` and pattern still appears in sessions after `action_date` → set status to `persistent`
   - If the known finding is `actioned` and pattern does NOT appear in sessions after `action_date` → set status to `resolved`
   - If no match → add as a new finding with status `new`
3. **For each ledger finding NOT detected this run:**
   - If status was `new` or `acknowledged` → it may have been naturally resolved; set status to `resolved` with a note
   - If status was `actioned` → set status to `resolved` (the fix worked)
   - Leave `accepted` and `deferred` as-is

### Phase 4 Changes: Differential Report

When generating the report with an existing ledger, the report format changes:

```markdown
# Transcript Analysis — {date} (Incremental)

**Previous run:** {last_run_date} | **New sessions since:** {new_session_count}
**Total:** {total_sessions} sessions ({total_cost}) | **Known findings:** {known_count}

## What Changed Since Last Run

### New Findings ({count})
{findings with status "new" — full detail as before}

### Persistent Findings ({count}) — Action Taken But Pattern Continues
{findings with status "persistent" — these need attention, the fix didn't work}

### Still Open ({count})
{findings with status "acknowledged" — show session count delta}

### Resolved ({count})
{one-line summary of each resolved finding — just the title and what fixed it}

### Deferred ({count})
{one-line summary of deferred findings}

## Updated CLAUDE.md Candidates
{only NEW candidates not already in the ledger}

## Full Pattern Log
{complete pattern table as before, but with status column from ledger}
```

### Phase 5: Update Ledger (NEW)

After generating the report, update the ledger:

```python
# Add this run to the runs array
ledger['runs'].append({
    'date': today,
    'sessions_analyzed': total_sessions,
    'session_date_range': [earliest_date, latest_date],
    'report_path': report_path
})
ledger['last_session_date'] = latest_session_timestamp

# Write updated ledger
os.makedirs(os.path.dirname(ledger_path), exist_ok=True)
with open(ledger_path, 'w') as f:
    json.dump(ledger, f, indent=2)
```

### User Triage Workflow

After each run, present the user with new and persistent findings for triage:

```
New findings to triage:
  1. [A] Pinterest board name validation (3 sessions)  →  action / acknowledge / accept / defer
  2. [C] QA skill timeout on large packs (2 sessions)  →  action / acknowledge / accept / defer

Persistent findings (action didn't resolve):
  3. [A] Hero image cropping — actioned 2026-03-18, still appears in 2 new sessions
     → re-investigate / accept / defer
```

The user's triage choices update the ledger immediately. This is the mechanism that prevents the same findings from being presented as "new" on every run.

---

## Seeding the Ledger from an Existing Report

If there's already a narrative analysis report but no ledger (as is the case for ShopForge right now), Phase 0 should detect this:

1. Look for `research/studies/*_narrative-analysis.md` files
2. If found but no ledger exists, offer to **seed the ledger** from the report
3. Parse the report's pattern log (Section 6) to extract finding IDs, categories, session counts, and quotes
4. Create ledger entries with status `acknowledged` (since the user has seen them but hasn't triaged)
5. Set `last_session_date` from the report's date range
6. Present the seeded findings for triage before proceeding with new analysis

This bootstrapping step only happens once — after that, the ledger is authoritative.

---

## Output Contract

After running this skill, the following artifacts exist:

1. **`.claude/transcript-analysis/findings-ledger.json`** — persistent finding state (the primary artifact)
2. **`research/studies/{date}_narrative-analysis.md`** — human-readable report for this run

The ledger is the source of truth. The report is a snapshot view derived from it.

---

## What This Skill Does NOT Do

- **No transcript summarization** — this finds patterns ACROSS sessions, not summaries OF sessions
- **No code analysis** — this analyzes conversation dynamics, not codebase changes
- **No prescriptive refactoring** — it identifies what repeats, not how to fix it (though it recommends CLAUDE.md vs skill vs decision)
- **No real-time monitoring** — this is a batch retrospective, not a live session watcher

---

## Anti-Pattern Checklist

Before finalizing the report, verify:

- [ ] Patterns are cross-session (appear in 2+ sessions), not one-off observations
- [ ] Representative quotes are actual user messages, not paraphrases
- [ ] Session counts reflect unique sessions, not total message occurrences
- [ ] CLAUDE.md candidates are rules the user stated, not inferences about what they might want
- [ ] Unresolved loops are genuinely unresolved (check for resolution artifacts in later sessions)
- [ ] The report distinguishes between "user keeps saying this" (Category A) and "this keeps failing" (Category C)
- [ ] Every surfaced quote has a provenance pointer (`<cliSessionId>/<lineIndex>`) that resolves to a verbatim `user`/`assistant` text block — never thinking, system, or tool content
- [ ] The report and ledger were redacted and scanned for secrets/PII and home-directory paths before finalizing
- [ ] Near-identical findings were deduped by shared rare terms (>= 3) before counting, so no single friction is double-counted across labels
