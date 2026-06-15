#!/usr/bin/env python3
"""Loop-closure detector for global-review-loop (Step 7).

Reads the corpus `turns.ndjson` + the ledger's `applied[]` entries and reports,
for each applied rule, the FIRST later turn whose text matches the rule's intent
(cosine >= cutoff, tsMs > appliedAt). Replaces the previous documented method
that interpolated rule + mined-corpus text into a `python -c "..."` shell string
(a shell-injection surface, since mined turns are untrusted). Here the corpus
text is only ever passed as Python data to _guards.cosine_sim -- never to a shell.

Usage:
  python loop_closure_scan.py --turns turns.ndjson --applied applied.json --json
  --applied : JSON array of {proposalId, canonicalRule|ruleSummary, appliedAt(ms)}
              (materialize it from `ledger_store.py list --full --json` -> applied[])
"""
import sys
import json
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _guards  # noqa: E402

EXIT_OK = 0
EXIT_ARGS = 5


def _reconfigure_utf8():
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8")
            except Exception:
                pass


def main() -> int:
    _reconfigure_utf8()
    ap = argparse.ArgumentParser(description="detect post-apply recurrence of applied rules")
    ap.add_argument("--turns", required=True, help="turns.ndjson from corpus_retrieve")
    ap.add_argument("--applied", required=True,
                    help="JSON array of {proposalId, canonicalRule, appliedAt}")
    ap.add_argument("--cutoff", type=float, default=0.40, help="cosine match cutoff (default 0.40)")
    ap.add_argument("--json", action="store_true", help="emit JSON array of hits")
    args = ap.parse_args()

    try:
        applied = json.loads(Path(args.applied).read_text(encoding="utf-8-sig"))
    except Exception as e:
        print(f"error: cannot read --applied ({e})", file=sys.stderr)
        return EXIT_ARGS
    if not isinstance(applied, list):
        print("error: --applied must be a JSON array of applied entries", file=sys.stderr)
        return EXIT_ARGS

    rules = []
    for a in applied:
        if not isinstance(a, dict):
            continue
        rule = (a.get("canonicalRule") or a.get("ruleSummary") or "").strip()
        pid = a.get("proposalId")
        applied_at = a.get("appliedAt")
        if rule and pid is not None and isinstance(applied_at, (int, float)):
            rules.append((pid, rule, applied_at))

    turns_path = Path(args.turns)
    if not turns_path.exists():
        print(f"error: --turns not found: {turns_path}", file=sys.stderr)
        return EXIT_ARGS

    hits = {}  # proposalId -> first post-apply recurrence hit
    with turns_path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                t = json.loads(line)
            except Exception:
                continue
            ts = t.get("tsMs")
            text = t.get("text") or ""
            if not isinstance(ts, (int, float)) or not text:
                continue
            for pid, rule, applied_at in rules:
                if pid in hits:
                    continue
                if ts > applied_at and _guards.cosine_sim(text, rule) >= args.cutoff:
                    hits[pid] = {
                        "proposalId": pid,
                        "recurredSession": t.get("sessionId") or t.get("session") or "",
                        "ts": int(ts),
                    }

    out = list(hits.values())
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        if not out:
            print("no post-apply recurrence detected")
        for h in out:
            print(f"RECURRED proposalId={h['proposalId']} "
                  f"session={h['recurredSession']} ts={h['ts']}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
