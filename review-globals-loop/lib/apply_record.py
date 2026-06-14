#!/usr/bin/env python3
"""apply_record.py - record an operator-confirmed apply (loop-closure bookkeeping).

Appends an AppliedImprovement-shaped entry (chat-arch applied-improvement.ts
shape, verbatim) to the ledger's applied[] with appliedAt, the verbatim
ProposedUpgrade snapshot, and the files the operator reports they edited. Flips
the proposal's proposedUpgrade.applied=true / appliedAt=now.

Writes ONLY the ledger (via ledger_store helpers). NEVER edits ~/.claude.

IMPORTANT (boundary layering): this is the BOOKKEEPING half of the apply, not
the apply itself. The review-globals-loop skill has NO write tools, so it cannot
mutate global config under ANY context -- that is the primary, mechanical guard.
The actual config edit must already have been made by the OPERATOR through a
write-capable surface (update-config for hooks, the memory mechanism for
memories, an operator-confirmed edit for CLAUDE.md / a global skill). This script
ALSO requires POSITIVE proof of an attended operator before recording: the ONLY
accepted proof is an explicit --confirm-operator token. A TTY on stdin is NOT
trusted (it fails open on Windows for DEVNULL/redirected stdin). It fails closed
(no token => refuse). An unattended/watcher denylist (including the real Command
runner markers) is kept as a secondary belt-and-suspenders. A watcher run (no
token) cannot record.

Idempotent on the (proposalId, target, targetPath) triple: re-apply replaces,
never duplicates.

Exit codes: 0 ok, 3 missing-dep, 4 refused/violation, 5 args, 6 unattended.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ledger_store as L  # noqa: E402
import _guards  # noqa: E402,F401  (shared fail-closed guards; canonical safe-out/normalize/cosine)

EXIT_OK = 0
EXIT_MISSING_DEP = 3
EXIT_VIOLATION = 4
EXIT_ARGS = 5
EXIT_UNATTENDED = 6

UNATTENDED_MARKERS = (
    "CLAUDE_WATCHER", "CLAUDE_UNATTENDED", "COMMAND_WATCHER",
    "CLAUDE_REVIEW_LOOP_ACTIVE", "CI",
    # Real Command runner markers (LB9): these are what an actual Command
    # watcher/runner run sets, so a real unattended run is also blocked here.
    # The --confirm-operator token (below) is still the PRIMARY guard.
    "COMMAND_PARALLEL_SLOT", "COMMAND_ROOT", "COMMAND_PORT", "COMMAND_FIELDS",
)


def eprint(*a):
    print(*a, file=sys.stderr)


def require_human_gate(confirm_operator: bool):
    # Secondary belt-and-suspenders: a known unattended/watcher marker is a hard
    # refusal regardless of any other signal.
    for marker in UNATTENDED_MARKERS:
        if os.environ.get(marker):
            eprint(f"REFUSED: unattended marker {marker} is set. Applying a global "
                   f"config change is operator-only, and the config mutation must be "
                   f"done by the operator through a write-capable surface "
                   f"(update-config / memory / a confirmed edit) BEFORE this "
                   f"bookkeeping call. Aborting.")
            sys.exit(EXIT_UNATTENDED)

    # Primary guard: require POSITIVE proof of an attended operator. The ONLY
    # positive proof is an explicit --confirm-operator token. isatty() is NOT
    # trusted as authorization: on Windows sys.stdin.isatty() returns True for a
    # DEVNULL/redirected stdin, so an unattended/watcher run could fail OPEN and
    # record an apply (LB4/LB15). We compute is_tty ONLY as a convenience hint in
    # the refusal message -- never as authorization. Fail closed: no token =>
    # refuse, so a watcher/runner with no token can never record.
    try:
        is_tty = bool(sys.stdin) and sys.stdin.isatty()
    except Exception:
        is_tty = False
    if not confirm_operator:
        hint = ("Your stdin looks interactive, so you can likely re-run with "
                "--confirm-operator now." if is_tty else
                "stdin is not interactive (watcher/redirected); only re-run with "
                "--confirm-operator if a human is actually present.")
        eprint("REFUSED: no proof of an attended operator. Recording an apply "
               "requires the explicit --confirm-operator flag -- a TTY is NOT "
               "accepted as proof (it fails open on Windows for redirected "
               "stdin). This fails closed so an unattended watcher/runner can "
               "never record bookkeeping. Pass --confirm-operator ONLY when a "
               "human has actually made the global config edit through a "
               f"write-capable surface. {hint} Aborting.")
        sys.exit(EXIT_UNATTENDED)


def snapshot_upgrade(proposal) -> dict:
    return copy.deepcopy(proposal.get("proposedUpgrade") or {})


def record_apply(proposal_id, target_files, notes):
    path = L.resolve_ledger_path()

    def op():
        ledger = L.load_ledger(path)
        target_proposal = None
        for p in ledger["proposals"]:
            if p.get("proposalId") == proposal_id:
                target_proposal = p
                break
        if target_proposal is None:
            eprint(f"error: proposal {proposal_id} not in ledger; upsert it first.")
            return EXIT_ARGS
        up = target_proposal.get("proposedUpgrade") or {}
        triple = (proposal_id, up.get("target"), up.get("targetPath"))
        ledger["applied"] = [
            a for a in ledger["applied"]
            if (a.get("proposalId"), a.get("target"), a.get("targetPath")) != triple
        ]
        now = L.now_ms()
        ledger["applied"].append({
            "proposalId": proposal_id,
            "target": up.get("target"),
            "targetPath": up.get("targetPath"),
            "ruleSummary": target_proposal.get("canonicalRule", ""),
            "proposedUpgrade": snapshot_upgrade(target_proposal),
            "targetFiles": target_files,
            "notes": notes,
            "appliedAt": now,
        })
        up["applied"] = True
        up["appliedAt"] = now
        target_proposal["proposedUpgrade"] = up
        ledger["generatedAt"] = now
        L.atomic_write(path, ledger)
        print(f"recorded apply: {proposal_id} -> {up.get('target')} :: {up.get('targetPath')}")
        print("NOTE: this only recorded bookkeeping. The actual config edit must have "
              "been made by the operator through a write-capable surface; review-globals-loop "
              "has no write tools and never edits ~/.claude itself.")
        return EXIT_OK

    return L.with_lock(op)


def main(argv):
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")

    p = argparse.ArgumentParser(prog="apply_record.py")
    p.add_argument("--proposal-id", required=True)
    p.add_argument("--target-files", default="", help="comma-separated files the operator edited")
    p.add_argument("--notes", default=None)
    p.add_argument("--confirm-operator", action="store_true",
                   help="explicit attended-operator token; the ONLY accepted proof of "
                        "attendance (a TTY is not trusted). Always required. Pass ONLY "
                        "when a human has actually made the global config edit through a "
                        "write-capable surface.")
    args = p.parse_args(argv)

    require_human_gate(args.confirm_operator)
    L.require_deps()

    files = [s.strip() for s in (args.target_files or "").split(",") if s.strip()]
    return record_apply(args.proposal_id, files, args.notes)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception as e:
        eprint(f"error: {e}")
        sys.exit(1)

