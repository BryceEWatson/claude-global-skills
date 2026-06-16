# pattern-retrospective/lib/ — Internals for the pattern-retrospective skill

These scripts are the substep helpers called by the retrospective workflow described in [`../SKILL.md`](../SKILL.md) §14–§15. They are designed to be invoked directly from the command line by the operator running a retro (or by an agent following the SKILL.md playbook).

**Invocation pattern:**

```bash
python ~/.claude/skills/pattern-retrospective/lib/<script>.py --help
```

All scripts are stdlib-only where possible (`filelock` is the one pinned PyPI dependency, for `register_finding.py`'s per-project lock; `anthropic` is required only for `dual_llm_coder.py`).

## Scripts (full set after all phases ship)

| Script | One-line purpose |
|---|---|
| `register_finding.py` | Append a validated finding row to `<project-root>/reports/_data/retro-findings.jsonl` (atomic, with backup rotation + monotonic `finding_id` per project). |
| `follow_up_check.py` | Scan a project's pending findings (or `--all` projects via the convention glob); print a markdown table of items still `pending` or past `target_date`. |
| `repeat_detector.py` | Fuzzy-match a candidate new claim against the registry (`--scope this-project` or `--scope all`); flags near-duplicates ≥0.85, candidates 0.70–0.84, novel <0.70. |
| `krippendorff_alpha.py` | Pure-function Krippendorff's α computation (stdlib only), with 4 unit tests including a textbook fixture cross-checked once vs k-alpha.org. |
| `dual_llm_coder.py` | Run two independent Anthropic SDK calls over the same coding prompt + items, compute α, assert request_ids differ, warn if outputs are byte-identical. |
| `recover_from_backup.py` | Operator-invoked JSONL corruption recovery: detect malformed line + report line number + `--rollback-to-latest-backup` or `--repair <N>`. |
| `cowork_filter.py` | 50-line corpus enumerator + line-shape filter mirroring `chat-history-search` SKILL.md §3. This is the "stop reinventing" answer cited by SKILL.md §14. |
| `_schema.json` | JSON Schema (draft-07) for a finding row; used for validation at register time and as the migration-ready surface for the future chat-arch Narrative entity. |

## Invocation examples

One runnable command per script. Substitute `<project>` with an absolute path (e.g. `~/Projects/MyProject`).

### `register_finding.py` — append a finding at retro end

When to use: each finished finding gets one append, after repeat-detector clears it.

```bash
python ~/.claude/skills/pattern-retrospective/lib/register_finding.py \
    --project-root <project> \
    --retro-path <project>/research/studies/2026-05-23/retro.md \
    --project myproject \
    --category methodology \
    --claim "Streaming JSONL parse prevents OOM on >100MB session logs." \
    --confidence 0.72 \
    --evidence-supporting 4 --evidence-contradicting 0 \
    --proposed-action "Document streaming pattern in chat-history-search §4." \
    --target-date 2026-06-30
```

### `follow_up_check.py` — at retro start, see what's pending

When to use: first command of any new retro, before mining begins.

```bash
python ~/.claude/skills/pattern-retrospective/lib/follow_up_check.py \
    --project-root <project>
```

Use `--all` to scan every project under `~/Projects/*/reports/_data/retro-findings.jsonl`.

### `repeat_detector.py` — during mining, check a candidate claim

When to use: before drafting any new finding, to avoid re-registering a known pattern.

```bash
python ~/.claude/skills/pattern-retrospective/lib/repeat_detector.py \
    --new-claim "Edit tool silently truncates large TS files after 3+ calls." \
    --scope this-project \
    --project-root <project>
```

Add `--scope all` to fan out across every project registry.

### `krippendorff_alpha.py` — run the self-tests

When to use: sanity-check the α implementation after edits, or in CI.

```bash
python ~/.claude/skills/pattern-retrospective/lib/krippendorff_alpha.py --test
```

### `dual_llm_coder.py` — high-stakes inter-rater coding

When to use: any retro that is high-stakes (≥5 findings OR confidence ≥0.70 OR substrate change OR handoff to another system).

**Requires** the `ANTHROPIC_API_KEY` environment variable. Use `--dry-run-no-api` for an API-free smoke test (stubs both calls; verifies CLI wiring + α math without spending tokens).

```bash
ANTHROPIC_API_KEY=sk-ant-... python ~/.claude/skills/pattern-retrospective/lib/dual_llm_coder.py \
    --items <project>/research/studies/2026-05-23/subsample.jsonl \
    --coding-prompt "Label each item with one of: methodology | substrate | process | discovery."
```

Smoke-test form:

```bash
python ~/.claude/skills/pattern-retrospective/lib/dual_llm_coder.py \
    --items <project>/research/studies/2026-05-23/subsample.jsonl \
    --coding-prompt "Label each item ..." \
    --dry-run-no-api
```

### `recover_from_backup.py` — JSONL corruption diagnosis + recovery

When to use: `register_finding.py` rejects with a parse error, or a manual edit broke a row.

```bash
# 1. Diagnose: prints the malformed line number + offending content.
python ~/.claude/skills/pattern-retrospective/lib/recover_from_backup.py \
    --project-root <project>

# 2a. Rollback to most-recent backup snapshot.
python ~/.claude/skills/pattern-retrospective/lib/recover_from_backup.py \
    --project-root <project> --rollback-to-latest-backup

# 2b. Surgical repair of a specific line (e.g. line 42).
python ~/.claude/skills/pattern-retrospective/lib/recover_from_backup.py \
    --project-root <project> --repair 42
```

### `cowork_filter.py` — iterate user prompts across Cowork + CLI corpora

When to use: when mining a retro and you need the canonical "drop wrappers, keep real user turns" filter from `chat-history-search` SKILL.md §3.

```bash
python ~/.claude/skills/pattern-retrospective/lib/cowork_filter.py \
    --corpus cowork \
    --project myproject
```

Pipe into `jq`/`grep` for ad-hoc inventory; the script streams JSONL to stdout (never loads a whole transcript).

## Recovery / repair

- **Junction missing at `<project>/.claude/skills/pattern-retrospective`.** The directory junction that exposes this skill inside a project tree can be deleted by an over-eager cleanup. Recreate it (no admin required for junctions on Windows):

  ```cmd
  cmd /c mklink /J <project>\.claude\skills\pattern-retrospective %USERPROFILE%\.claude\skills\pattern-retrospective
  ```

- **`<project>/reports/_data/retro-findings.jsonl` is corrupt.** Run the recovery helper to diagnose, then either roll back to the most recent backup snapshot or surgically repair a specific line:

  ```bash
  # Diagnose (prints malformed line number).
  python ~/.claude/skills/pattern-retrospective/lib/recover_from_backup.py --project-root <project>

  # Recover.
  python ~/.claude/skills/pattern-retrospective/lib/recover_from_backup.py --project-root <project> --rollback-to-latest-backup
  # …or…
  python ~/.claude/skills/pattern-retrospective/lib/recover_from_backup.py --project-root <project> --repair <N>
  ```

## ⚠️ Known fragilities

1. **`follow_up_status` is operator-maintained only.** No automated reminder, no gate. Findings rot in `pending` if not updated. **Mitigation:** future retro-on-retro process; next retro's `follow_up_check.py` surfaces stale items.
2. **Multi-machine.** If you run retros on multiple machines, project registries diverge UNLESS the project is git-tracked. Since findings live at `<project>/reports/_data/retro-findings.jsonl`, committing the file makes the registry travel with the project.
3. **`difflib.SequenceMatcher` catches lexical repeats only.** Semantic repeats with different wording will be missed. Verification step 4 tests this; upgrade path: RapidFuzz → embeddings.
4. **Append-only.** Use `supersedes` or `follow_up_status: cancelled` to retract; never edit a row in place.
5. **Cross-project discovery uses convention.** `--scope all` globs `~/Projects/*/reports/_data/retro-findings.jsonl`. Projects outside that path need explicit `--registries path1,path2,...`.
