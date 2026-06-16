---
name: pattern-retrospective
description: Apply rigorous pattern-retrospective analysis to Claude transcripts. Triggers on writing studies, blog posts, handoff docs, retrospectives, specs, or improvement proposals that draw conclusions from past Claude sessions — phrasings like "handoff to X", "lessons from session Y", "pattern Z over time", "spec for X", "improvements to X", "audit my use of W" all match. Enforces target-system audit BEFORE specifying its requirements, streaming JSONL parsing (never load whole files), 5-tuple extraction with provenance tags, self-falsification of every cited claim including system-state claims, Bayesian confidence smoothing, and correlation-not-causation discipline. Designed to prevent the credulous-handoff failure mode where ~60% of proposed requirements turn out to already exist in the target system.
when_to_use: When retrospecting on prior Claude sessions to extract reusable patterns or specify how another system should support them. Skip if the work is purely forward-looking (no transcripts being mined). Especially load-bearing when the output is a "handoff to" or "spec for" another system you haven't read recently.
---

# Pattern-retrospective workflow

You're producing a study / blog post / handoff / spec / retrospective that draws conclusions from your past Claude sessions. Apply this rigor before publishing.

A future automation substrate could eventually automate this. Until such a system exists, you are the curator, falsifier, and substrate. Write the outputs so they're machine-readable later, in case that automation lands.

## §1 — The load-bearing meta-discipline

**Before specifying any external system's requirements, audit that system first.**

If you're writing a "handoff to X" / "spec for X" / "improvements to X" / "what X should do" document, spend 30 minutes reading X's code, locked spec, and CLAUDE.md before writing. Lead the document with a "Context: what already exists in X" section. If you can't write that section, you can't write the document.

This applies recursively. When you cite a fact about an external system, verify it the same way you'd verify any other agent's cited claim. "X already does Y" is a falsifiable claim — falsify it before relying on it.

**Why this rule exists:** a real handoff once failed this check. Roughly 60% of its proposed requirements for the target system already existed in that system's codebase — log-location discovery, a false-positive filter, external verification, aggregate metrics — none of which had been audited first. Don't repeat that.

## §2 — Storage map (always search both corpora)

Your transcripts live in two corpora. Searching only one misses ~10× the data.

**Corpus 1 — Claude Code CLI:**
- `~/.claude/projects/<encoded-cwd>/<sessionId>.jsonl` (main sessions)
- `~/.claude/projects/<encoded-cwd>/<sessionId>/subagents/agent-*.jsonl` (subagent transcripts — sibling to parent, NOT at the same level)

**Corpus 2 — Cowork (Claude Desktop local-agent-mode):**
- `%AppData%\Claude\local-agent-mode-sessions\<org>\<user>\<sessionId>\agent\local_ditto_<sessionId>\audit.jsonl` (top-level audit)
- `<local_cwd>/audit.jsonl` (per-cwd audit — PRIMARY user-prompt source)
- `<local_cwd>/.claude/projects/-sessions-<processName>/<cliSessionId>.jsonl` (CLI subprocess sessions)
- `<local_cwd>/.claude/projects/<encoded-output-path>/<sessionId>.jsonl` (output queue — CANONICAL source for typed text via `queue-operation enqueue` events)
- `<local_cwd>/.claude/projects/.../subagents/agent-*.jsonl` (subagent transcripts)

macOS path swap: `~/Library/Application Support/Claude/...`. The Cowork directory is migrating to `claude-code-sessions`; both names may coexist.

## §3 — JSONL line-shape filter (keep 3, drop 2)

Five shapes appear in `type:"user"` lines. Misclassifying the wrong ones is the primary source of false positives.

```text
KEEP — typed user prompt (CLI shape, content is an array):
  {type:"user", message:{role:"user", content:[{type:"text", text:"..."}]}, timestamp}

KEEP — typed user prompt (Cowork audit shape, content is a string):
  {type:"user", message:{role:"user", content:"..."}, _audit_timestamp}

KEEP — queue-enqueue event (Cowork output, CANONICAL):
  {type:"queue-operation", operation:"enqueue", content:"...", timestamp}

DROP — tool result wrapped as user (assistant's tool output):
  {type:"user", message:{role:"user", content:[{tool_use_id:"...", type:"tool_result"}]}}

DROP — task-notification wrapper (subagent task descriptor, NOT user-typed):
  {type:"user", message:{role:"user", content:[{type:"text", text:"<task-notification>..."}]}}
```

Also drop: text starting `<task-notification>`; cat-n format output (`\n     1\t...`); top-level `type:"attachment"`; `type:"system"`; `isSidechain:true`; `isApiErrorMessage:true`; subagent transcript root files (first line is the task description TO the subagent, not user text).

Without this filter, the term "adversarial" returned 217 hits across Cowork; with it, ~71 real user-typed deploys. Both numbers are correct — they're counting different things. Use the right one.

## §4 — Streaming parsers, never whole-file loads

Cowork transcripts can hit 18MB; subagent files run 250KB × 5–40 per session. Loading whole files into LLM context will fail (this is what broke 11 of 12 sessions in the May 2026 study).

Read line-by-line. Capture only:
- the user prompt by substring match
- each subagent file's task description (first event) and final assistant message
- follow-up user turns for validation

A streaming extractor produces a complete 5-tuple per session in seconds at any file size. Reference Python: `extract_cowork_5tuple_v2.py` in `scripts/.tmp/adv-study/` if one was committed there.

Critically: **this is an LLM-context-window problem, not a Node-heap problem.** Don't conflate the two when writing about it for other systems.

## §5 — 5-tuple extraction with provenance

For each pattern invocation:

```text
{
  dispatch:    { promptText, sessionId, timestamp, cwd },
  findings:    [{ sourceSubagentId, findingNumber, description, severity?, domain? }],
  validations: [{ findingNumber, verdict: confirmed|rejected|ambiguous|no_validation,
                  userMessageRef?, inferredFromAction? }],
  actions:     [{ type: commit|edit|pr_comment|ignored, targetFinding, detail, timestamp? }],
  outcome:     { state: converged|abandoned|hit_cap|continued,
                 iterationsRun, finalUserSentiment?, shippedArtifact? }
}
```

Tag every field with provenance at write-time:
- `deterministic` — parsing-rule extraction (timestamps, sessionIds, action types, commit hashes from tool calls)
- `llm-derived` — read-and-classify (description, severity, sentiment, validation verdict)
- `inferred-from-action` — implicit (user moved on without explicit yes/no; treat as weak signal)
- `falsifier-verified` — passed an independent verification pass against ground truth

## §6 — Falsify your own claims before publishing

This is the discipline that distinguishes a real retrospective from a credulous one.

| Claim type | Verification |
|---|---|
| Commit hash | `git -C <repo> log --oneline \| grep <hash>` — exists or not |
| PR number | `gh pr view <num>` — exists, state matches claim |
| URL | WebFetch or `curl -s <url>` — page exists, content matches the citation |
| File:line reference | Read the file — line says what's claimed |
| External fact | Web search or fetch authoritative source |
| Math claim | Recompute from source numbers |
| **Claim about external system** | **Read the system's spec, CLAUDE.md, key source files. Don't speculate.** |

The May 2026 adversarial-subagent study found:
- 1 fabricated arXiv authorship (TRUE — verified externally; real authors were de Groot, Aliannejadi, Haas)
- 2 hallucinated commit hashes (`1e00c87`, `b91ced` — neither in repo, dropped)
- 1 wrong mean (claimed 1.8, recomputed as 2.18 from raw counts)

Without falsification, all four would have shipped as fact.

**Apply the same rigor to your own document.** "X already does Y" is the most-skipped category — the handoff above cited several "the target already does Z" claims that weren't checked. They turned out to be wrong about the existing state of that system.

## §7 — Bayesian confidence smoothing

When summarizing patterns across sessions:

```text
confidence = supporting / (supporting + contradicting + 2)
```

| Evidence | Confidence | Interpretation |
|---|---|---|
| 1 supporting / 0 contradicting | 0.33 | Candidate — NOT "always" |
| 2 / 0 | 0.50 | Pattern emerging |
| 6 / 0 | 0.75 | Validated — eligible for proposed-rule status |
| 6 / 1 | 0.67 | Validated stays just below the promote threshold |
| 12 / 1 | 0.80 | Strong evidence |

Never write "X always happens" from a single observation. The +2 smoothing prevents the overclaim. Match writeup confidence to the evidence smoothing.

## §8 — Correlation, not causation

When a pattern correlates with good outcomes:
- Say "correlates with" — never "causes"
- Show SE alongside the difference when the cited-sample is small (n<5)
- Don't rank patterns by correlation when the underlying sample is too thin to be significant
- Use a permutation test or Welch's t for any "this pattern → better outcomes" claim
- Disclose the test in the methodology paragraph

## §9 — Writeup structure

Every retrospective document carries:

1. **Methodology paragraph at top** — corpora searched, sample sizes, extraction protocol, key caveats. One paragraph; primes the reader's epistemics.
2. **Synoptic counters** — 4 KPI numbers (invocation count, sessions covered, falsification stats, evidence quality). Synoptic-before-detail.
3. **Ranked findings with explicit `#1`, `#2`** — rank is a claim worth showing, not implicit.
4. **Per-claim provenance tags inline** — `[deterministic]` / `[llm-derived]` / `[falsifier-verified]` / `[user-confirmed]` near every load-bearing number or claim.
5. **Significance gates** — display correlation only when |Δ|/SE > stated threshold.
6. **Explicit non-claims** — a "what this data does NOT support" section, distinct from the limitations.
7. **Limitations section** — sample size, selection bias, time window, who/what was excluded.
8. **Next experiments** — what would close each non-claim.

Progressive disclosure for high-count example lists: "Show 3 of 61 examples" pattern. Don't flood the page.

## §10 — Quality gate before publishing

Don't ship the document until every answer is "yes":

1. Have I audited every external system I make claims about?
2. Have I falsified every cited commit / URL / file-ref / number / system-claim?
3. Have I applied confidence smoothing to summary claims?
4. Have I tagged every claim with its provenance?
5. Have I disclosed correlation-not-causation where relevant?
6. Have I included a "what the data doesn't support" section?
7. Have I verified that nothing I'm proposing already exists in the target system?
8. Would a falsifier-agent reading just my document be able to refute any specific claim with a Read or git or web-fetch?

If any answer is "no," the document isn't ready. The recursive falsifier-on-self is item 7 — it's the discipline that would have caught the May handoff.

## §11 — Output location

Save outputs into structured locations so they're addressable if automation lands later:

- **Retrospectives / studies** → `research/studies/YYYY-MM-DD_<topic>.md`
- **Handoffs / specs** → `_planning/<topic>.md` (in the repo of the target system, not this one)
- **5-tuple extractions** → `research/studies/_data/<study-id>/5-tuples/<timestamp>_<sessionId>.json`
- **Inventory tables** → `research/studies/_data/<study-id>/inventory.csv` or `.json`

If such automation ships later, these become seed data. Keep them machine-readable.

## §12 — Self-check: am I being credulous right now?

If a document feels like it's writing itself faster than you can verify its claims, you're probably being credulous. Symptoms:

- You haven't opened a single file in the target system in the past hour
- You're enumerating "phases" for another system without having read its existing phases
- You're citing version numbers, commit hashes, or file paths without checking
- The document is feeling clean and confident at a suspicious clip
- You're describing failure modes you encountered as if they're the target system's required features

Slow down. Run §6 falsifier on your own draft. Audit any external systems mentioned in §1. Re-check the math. Confident-and-clean is not the same as correct.

## §13 — Connection to a future automation substrate

This workflow is what a future automation substrate could eventually automate. Until then:

- You are the **curator** — deciding what's worth retrospecting
- You are the **falsifier** — verifying citations
- You are the **substrate** — your written outputs are the durable record

When such a substrate lands, a curator-agent + falsifier-agent + provenance-tagged record could absorb these responsibilities. Until then, the discipline is yours to enforce manually. Write outputs in a shape that maps cleanly to a future substrate (the 5-tuple structure in §5 was designed for this).

## §14 — Substep contracts (the "stop reinventing" answer):

- **For corpus enumeration + line-shape filtering:** use the `lib/cowork_filter.py` helper (shipped with this skill). Mirrors `chat-history-search`'s §3 filter rules; iterates Cowork + CLI corpora; yields parseable prompts. (`chat-history-search` itself currently outputs human-readable markdown only; the helper is the programmatic-consumption answer until chat-history-search adds `--output-format jsonl` — separate proposal.)
- **For the adversarial review pass:** manual operator step — after retro draft lands, operator runs `/review-loop` against the commit that landed the report. Pattern-retrospective does NOT automate this invocation; the skill's workflow ends with "Report drafted at `<path>`; commit and run `/review-loop` for adversarial review." Do NOT copy review-loop's agent definitions into this skill — drifts toward Option B merge (user rejected).
- **For cross-iteration finding dedup:** use the ≥0.85 threshold from review-loop's drift-guard (same value, embedded in `lib/repeat_detector.py`).
- **For provenance:** use the 5-tuple structure from §5 (existing).

## §15 — Process discipline added in 2026-05:

- **Before mining:** write 200-word reflexive bias memo at `<project-root>/research/studies/<study-id>/_methodology.md`. Pre-register 2-3 expected patterns.
- **At retro start:**
  ```bash
  python ~/.claude/skills/pattern-retrospective/lib/follow_up_check.py \
      --project-root <path-to-project>
  ```
  Review pending findings; update `follow_up_status` before proceeding.
- **During mining** for each new finding:
  ```bash
  python ~/.claude/skills/pattern-retrospective/lib/repeat_detector.py \
      --new-claim "<text>" \
      --scope this-project \
      --project-root <path>
  ```
  Optionally rerun with `--scope all` for cross-project repeats.
- **At retro end** for each finding:
  ```bash
  python ~/.claude/skills/pattern-retrospective/lib/register_finding.py \
      --project-root <path> \
      --retro-path <path-to-retro.md> \
      --project <slug> \
      --category <tag> \
      --claim "<text>" \
      --confidence 0.71 \
      --evidence-supporting 5 --evidence-contradicting 0 \
      --proposed-action "<text>" \
      --target-date 2026-06-30
  ```
- **High-stakes retro** (≥5 findings OR conf≥0.70 OR substrate change OR handoff):
  **Requires** `ANTHROPIC_API_KEY` env var. Use `--dry-run-no-api` to smoke-test without calling the API.
  ```bash
  python ~/.claude/skills/pattern-retrospective/lib/dual_llm_coder.py \
      --items <subsample.jsonl> --coding-prompt "<text>"
  ```
  Block publish at α<0.80; mark exploratory at 0.67-0.79; publish at ≥0.80.
