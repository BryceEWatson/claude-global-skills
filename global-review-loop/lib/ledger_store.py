#!/usr/bin/env python3
"""ledger_store.py - the cross-run state ledger for global-review-loop.

Owns <skill-root>/.local-state/proposals-ledger.json. Writes ONLY the ledger,
NEVER ~/.claude config and NEVER a git working tree. Atomic tmp+rename,
per-file filelock, 3-backup rotation, JSON-schema validation -- mirrors
pattern-retrospective/lib/register_finding.py conventions. Mirrors chat-arch's
CorrectionPattern.id derivation + ProposedUpgrade shape WITHOUT importing the TS.

The >=2-distinct-project gate counts ONLY path-attributed evidence
(attribution=='path'); keyword/none evidence cannot satisfy the gate.

Subcommands:
    validate   --proposal-file F            check one proposal against the rules
    upsert     --proposal-file F            cross-run dedup by proposalId; corroborate
    dismiss    --proposal-id ID --reason R  do-not-resuggest
    mark-recurrence --proposal-id ID --recurred-session S --ts MS
    list       [--json] [--full] [--top N]   ranked operator panel
    panel      [--top N]                    alias of list (human)

Exit codes: 0 ok, 2 corruption, 3 missing-dep, 4 schema/gate violation,
            5 args, 6 lock contention (could not acquire the ledger lock).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import sys; from pathlib import Path; sys.path.insert(0, str(Path(__file__).resolve().parent)); import _guards

EXIT_OK = 0
EXIT_CORRUPTION = 2
EXIT_MISSING_DEP = 3
EXIT_SCHEMA = 4
EXIT_ARGS = 5
EXIT_LOCK = 6  # lock contention -- distinct from schema/gate (4)


def _reconfigure_utf8():
    """cp1252 is the Windows machine default; without this any em-dash / smart
    quote / arrow printed to stdout/stderr crashes the helper. Run at entry."""
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass

SKILL_ROOT = Path(__file__).resolve().parent.parent
LEDGER_PATH = SKILL_ROOT / ".local-state" / "proposals-ledger.json"
LOCK_PATH = SKILL_ROOT / ".local-state" / ".proposals-ledger.lock"

ALLOWED_TARGETS = {"global-claude-md", "global-skill", "memory", "settings-hook"}
SCHEMA_VERSION = 1


def eprint(*a):
    print(*a, file=sys.stderr)


def resolve_ledger_path() -> Path:
    """Always <skill-root>/.local-state/...; refuse anything under ~/.claude config
    (other than the skill dir itself) or inside a git working tree."""
    p = LEDGER_PATH.resolve()
    home_claude = (Path.home() / ".claude").resolve()
    sk = SKILL_ROOT.resolve()
    # must stay under the skill dir
    if not str(p).lower().startswith(str(sk).lower()):
        eprint("FATAL: ledger path escaped the skill dir; refusing to write.")
        sys.exit(EXIT_SCHEMA)
    # never the literal global config files
    forbidden = [home_claude / "CLAUDE.md", home_claude / "settings.json"]
    if p in [f.resolve() for f in forbidden]:
        eprint("FATAL: ledger path resolves to a global config file; refusing.")
        sys.exit(EXIT_SCHEMA)
    # never inside a git working tree (skill dirs are normally not repos; guard anyway)
    cur = p.parent
    while True:
        if (cur / ".git").exists():
            eprint(f"FATAL: ledger path is inside a git working tree ({cur}); "
                   f"refusing to write bookkeeping into a trackable/committable repo.")
            sys.exit(EXIT_SCHEMA)
        if cur.parent == cur:
            break
        cur = cur.parent
    return p


def proposal_id(canonical_rule: str) -> str:
    norm = " ".join((canonical_rule or "").lower().split())
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def now_ms() -> int:
    return int(time.time() * 1000)


def _evidence_key(e):
    """Composite dedup key for an evidence item. Using sourceRef alone makes
    every sourceRef-less item hash to None, so all but the first are dropped.
    Combine sourceRef, projectId, and a hash of the text so distinct
    path-attributed projects are never silently merged away."""
    if not isinstance(e, dict):
        return (None, None, hash(repr(e)))
    src = e.get("sourceRef") or ""
    proj = e.get("projectId") or ""
    text = e.get("text") or e.get("quote") or e.get("summary") or ""
    return (src, proj, hash(text))


def evidence_distinct_path_projects(proposal) -> set:
    """Distinct projectIds in evidence that are PATH-attributed (fail-closed)."""
    projs = set()
    for e in proposal.get("evidence", []) or []:
        if not isinstance(e, dict):
            continue
        pid = e.get("projectId")
        if not (isinstance(pid, str) and pid and pid != "(unattributed)"):
            continue
        if e.get("attribution") == "path":
            # Use the SHARED identity so this >=2-distinct-PATH-project gate
            # collapses ids exactly as claimpack/recurrence do (e.g. "MyProject"
            # and "myproject" are one project) -- inconsistent normalization
            # previously rejected a survived proposal at upsert.
            projs.add(_guards.normalize_project_id(pid))
    return projs


PROPOSAL_SCHEMA = {
    "type": "object",
    "required": ["canonicalRule", "proposedUpgrade", "evidence"],
    "properties": {
        "canonicalRule": {"type": "string"},
        "proposalId": {"type": "string"},
        "proposedUpgrade": {
            "type": "object",
            "required": ["target", "headline", "patch"],
            "properties": {
                "target": {"type": "string", "enum": sorted(ALLOWED_TARGETS)},
                "headline": {"type": "string"},
                "patch": {"type": "string"},
                "targetPath": {"type": "string"},
            },
        },
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "projectId": {"type": "string"},
                    "attribution": {"type": "string"},
                    "sourceRef": {"type": "string"},
                },
            },
        },
    },
}


def schema_validate(obj) -> list:
    """Structural shape check. Uses jsonschema when available (matches SKILL.md's
    claim); falls back to an equivalent manual check so behavior is identical
    whether or not the optional lib is importable."""
    try:
        import jsonschema
    except ImportError:
        return _manual_schema_check(obj)
    validator = jsonschema.Draft7Validator(PROPOSAL_SCHEMA)
    return [f"schema: {e.message}" for e in validator.iter_errors(obj)]


def _manual_schema_check(obj) -> list:
    errs = []
    if not isinstance(obj, dict):
        return [f"schema: expected an object, got {type(obj).__name__}"]
    for req in ("canonicalRule", "proposedUpgrade", "evidence"):
        if req not in obj:
            errs.append(f"schema: missing required field {req!r}")
    up = obj.get("proposedUpgrade")
    if up is not None and not isinstance(up, dict):
        errs.append("schema: proposedUpgrade must be an object")
    elif isinstance(up, dict):
        for req in ("target", "headline", "patch"):
            if req not in up:
                errs.append(f"schema: proposedUpgrade missing {req!r}")
    ev = obj.get("evidence")
    if ev is not None and not isinstance(ev, list):
        errs.append("schema: evidence must be an array")
    return errs


def validate_proposal(obj) -> list:
    """Return a list of violation strings (empty = valid)."""
    # OS9: a JSON array (e.g. the product proposals.json) crashes obj.get()
    # downstream with an opaque AttributeError. Catch it with a clear message.
    if isinstance(obj, list):
        return ["expected a single proposal object, got an array; "
                "split survivors into individual proposal files first"]
    if not isinstance(obj, dict):
        return [f"expected a single proposal object, got {type(obj).__name__}"]
    errs = list(schema_validate(obj))
    up = obj.get("proposedUpgrade") or {}
    tgt = up.get("target")
    if tgt not in ALLOWED_TARGETS:
        errs.append(f"target {tgt!r} not in {sorted(ALLOWED_TARGETS)}")
    if not (up.get("patch") or "").strip():
        errs.append("proposedUpgrade.patch is empty")
    if not (up.get("headline") or "").strip():
        errs.append("proposedUpgrade.headline is empty")
    if not (obj.get("canonicalRule") or "").strip():
        errs.append("canonicalRule is empty")
    projs = evidence_distinct_path_projects(obj)
    if len(projs) < 2:
        errs.append(f">=2 distinct PATH-attributed-project gate FAILED "
                    f"(got {len(projs)}: {sorted(projs)}); keyword/none evidence "
                    f"does not count toward the cross-project bar")
    return errs


def load_ledger(path: Path):
    if not path.exists():
        return {"schemaVersion": SCHEMA_VERSION, "generatedAt": now_ms(),
                "proposals": [], "dismissed": [], "applied": []}
    try:
        # utf-8-sig tolerates a BOM if some tool re-saved the file
        with path.open("r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as e:
        eprint(f"corruption: ledger is not valid JSON ({e}). "
               f"Recover from a .bak-* backup in {path.parent}")
        sys.exit(EXIT_CORRUPTION)
    data.setdefault("proposals", [])
    data.setdefault("dismissed", [])
    data.setdefault("applied", [])
    data.setdefault("schemaVersion", SCHEMA_VERSION)
    return data


def rotate_backups(data_dir: Path, keep: int = 3):
    baks = sorted(data_dir.glob("proposals-ledger.json.bak-*"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    for old in baks[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


def atomic_write(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        ts = int(time.time())
        bak = path.parent / f"proposals-ledger.json.bak-{ts}"
        bump = 0
        while bak.exists():
            bump += 1
            bak = path.parent / f"proposals-ledger.json.bak-{ts}-{bump}"
        try:
            bak.write_bytes(path.read_bytes())
        except OSError as e:
            eprint(f"warning: backup failed: {e}")
        rotate_backups(path.parent, keep=3)
    tmp = path.parent / f"proposals-ledger.json.tmp-{os.getpid()}"
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def smoothed_confidence(support: int, contra: int) -> float:
    return round(support / (support + contra + 2), 3)


def upsert_proposal(ledger, proposal) -> str:
    """Dedup by proposalId; corroborate or insert. Returns the verb."""
    rule = proposal.get("canonicalRule", "")
    pid = proposal.get("proposalId") or proposal_id(rule)
    proposal["proposalId"] = pid
    dismissed_ids = {d.get("proposalId") for d in ledger.get("dismissed", [])}
    if pid in dismissed_ids:
        return "suppressed"
    for existing in ledger["proposals"]:
        if existing.get("proposalId") == pid:
            ev = existing.setdefault("evidence", [])
            # Composite key: sourceRef alone collapses every sourceRef-less item
            # to None, so all but the first None-ref item are silently dropped --
            # losing newly-corroborated path-attributed projects and undercounting
            # distinctProjects. Key on (sourceRef, projectId, text-hash) instead.
            seen = {_evidence_key(e) for e in ev}
            for e in proposal.get("evidence", []) or []:
                k = _evidence_key(e)
                if k not in seen:
                    ev.append(e)
                    seen.add(k)
            existing["occurrenceCount"] = existing.get("occurrenceCount", 1) + 1
            existing["lastSeen"] = now_ms()
            existing["scope"] = {"kind": "global",
                                 "distinctProjects": len(evidence_distinct_path_projects(existing))}
            existing["confidence"] = smoothed_confidence(
                len(evidence_distinct_path_projects(existing)), 0)
            return "corroborated"
    proposal.setdefault("firstSeen", now_ms())
    proposal["lastSeen"] = now_ms()
    proposal.setdefault("occurrenceCount", 1)
    proposal["scope"] = {"kind": "global",
                         "distinctProjects": len(evidence_distinct_path_projects(proposal))}
    proposal["confidence"] = smoothed_confidence(
        len(evidence_distinct_path_projects(proposal)), 0)
    proposal.setdefault("loopClosure", {"recurringPostApplication": False,
                                        "alreadyEncoded": False})
    ledger["proposals"].append(proposal)
    return "inserted"


def rank_proposals(proposals):
    def key(p):
        lc = p.get("loopClosure") or {}
        recurring = 1 if lc.get("recurringPostApplication") else 0
        dp = (p.get("scope") or {}).get("distinctProjects", 0)
        return (-recurring, -dp, -p.get("occurrenceCount", 0), -p.get("lastSeen", 0))
    return sorted(proposals, key=key)


def require_deps():
    try:
        import filelock  # noqa: F401
    except ImportError:
        eprint("missing dependency: filelock. Install with: pip install --user filelock")
        sys.exit(EXIT_MISSING_DEP)


def with_lock(fn):
    from filelock import FileLock, Timeout
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(LOCK_PATH))
    try:
        with lock.acquire(timeout=30):
            return fn()
    except Timeout:
        eprint(f"error: could not acquire lock {LOCK_PATH} within 30s "
               f"(another run holds it); exit {EXIT_LOCK} = lock contention")
        sys.exit(EXIT_LOCK)


def cmd_validate(args):
    obj = json.loads(Path(args.proposal_file).read_text(encoding="utf-8-sig"))
    errs = validate_proposal(obj)
    if errs:
        eprint("INVALID proposal:")
        for e in errs:
            eprint(f"  - {e}")
        return EXIT_SCHEMA
    print("OK")
    return EXIT_OK


def cmd_upsert(args):
    require_deps()
    obj = json.loads(Path(args.proposal_file).read_text(encoding="utf-8-sig"))
    errs = validate_proposal(obj)
    if errs:
        eprint("INVALID proposal (refusing to fold):")
        for e in errs:
            eprint(f"  - {e}")
        return EXIT_SCHEMA
    path = resolve_ledger_path()

    def op():
        ledger = load_ledger(path)
        verb = upsert_proposal(ledger, obj)
        ledger["generatedAt"] = now_ms()
        if verb != "suppressed":
            atomic_write(path, ledger)
        print(f"{verb}: {obj.get('proposalId')}")
        return EXIT_OK
    return with_lock(op)


def cmd_dismiss(args):
    require_deps()
    path = resolve_ledger_path()

    def op():
        ledger = load_ledger(path)
        ids = {d.get("proposalId") for d in ledger["dismissed"]}
        if args.proposal_id in ids:
            print("already dismissed")
            return EXIT_OK
        ledger["dismissed"].append({
            "proposalId": args.proposal_id,
            "reason": args.reason,
            "dismissedAt": now_ms(),
        })
        ledger["proposals"] = [p for p in ledger["proposals"]
                               if p.get("proposalId") != args.proposal_id]
        ledger["generatedAt"] = now_ms()
        atomic_write(path, ledger)
        print(f"dismissed: {args.proposal_id}")
        return EXIT_OK
    return with_lock(op)


def cmd_mark_recurrence(args):
    require_deps()
    path = resolve_ledger_path()

    def op():
        ledger = load_ledger(path)
        applied_at = None
        for a in ledger["applied"]:
            if a.get("proposalId") == args.proposal_id:
                applied_at = a.get("appliedAt")
                break
        changed = False
        for p in ledger["proposals"]:
            if p.get("proposalId") == args.proposal_id:
                lc = p.setdefault("loopClosure", {})
                if applied_at is not None and args.ts > applied_at:
                    lc["recurringPostApplication"] = True
                    dp = (p.get("scope") or {}).get("distinctProjects", 1)
                    p["confidence"] = smoothed_confidence(dp, 1)  # drop confidence
                    # LB3: persist the durable audit-trail entry SKILL.md Step 7
                    # promises -- which session re-triggered it and when. Dedup by
                    # (session, ts) so a re-run with the same args is idempotent.
                    recs = lc.setdefault("recurrences", [])
                    entry = {"recurredSession": args.recurred_session, "ts": args.ts}
                    if not any(r.get("recurredSession") == entry["recurredSession"]
                               and r.get("ts") == entry["ts"] for r in recs):
                        recs.append(entry)
                    changed = True
                    print(f"recurringPostApplication=true for {args.proposal_id}")
                else:
                    print("recurrence not after appliedAt (or never applied); no change")
                break
        else:
            print("proposal not found")
            return EXIT_ARGS
        # OS2: only mutate the durable file (and rotate a backup) when something
        # actually changed -- a no-op recurrence must not churn backups.
        if changed:
            ledger["generatedAt"] = now_ms()
            atomic_write(path, ledger)
        return EXIT_OK
    return with_lock(op)


def cmd_list(args):
    path = resolve_ledger_path()
    ledger = load_ledger(path)
    ranked = rank_proposals(ledger["proposals"])
    if args.top:
        ranked = ranked[: args.top]
    if args.json:
        out = {
            "proposals": ranked,
            "dismissedCount": len(ledger["dismissed"]),
            "appliedCount": len(ledger["applied"]),
        }
        if getattr(args, "full", False):
            # C2: full arrays alongside the counts. Step 7 reads applied[].
            out["applied"] = [
                {
                    "proposalId": a.get("proposalId"),
                    "appliedAt": a.get("appliedAt"),
                    "canonicalRule": a.get("canonicalRule"),
                    "ruleSummary": a.get("ruleSummary"),
                }
                for a in ledger["applied"]
            ]
            out["dismissed"] = [
                {
                    "proposalId": d.get("proposalId"),
                    "reason": d.get("reason"),
                }
                for d in ledger["dismissed"]
            ]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return EXIT_OK
    print(f"=== global-review-loop ledger ({len(ledger['proposals'])} proposals, "
          f"{len(ledger['dismissed'])} dismissed, {len(ledger['applied'])} applied) ===")
    for i, p in enumerate(ranked, 1):
        lc = p.get("loopClosure") or {}
        flag = " [RECURRING-POST-APPLY]" if lc.get("recurringPostApplication") else ""
        applied = " (applied)" if (p.get("proposedUpgrade") or {}).get("applied") else ""
        up = p.get("proposedUpgrade") or {}
        dp = (p.get("scope") or {}).get("distinctProjects", 0)
        print(f"  {i}. [{dp} proj, conf {p.get('confidence')}]{flag}{applied} "
              f"{up.get('headline','')}")
        print(f"     target: {up.get('target')} :: {up.get('targetPath')}")
        print(f"     patch: {(up.get('patch') or '')[:120]}")
    return EXIT_OK


def main(argv):
    _reconfigure_utf8()
    p = argparse.ArgumentParser(prog="ledger_store.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate"); v.add_argument("--proposal-file", required=True)
    u = sub.add_parser("upsert"); u.add_argument("--proposal-file", required=True)
    d = sub.add_parser("dismiss")
    d.add_argument("--proposal-id", required=True)
    d.add_argument("--reason", required=True)
    m = sub.add_parser("mark-recurrence")
    m.add_argument("--proposal-id", required=True)
    m.add_argument("--recurred-session", required=True)
    m.add_argument("--ts", type=int, required=True)
    ls = sub.add_parser("list"); ls.add_argument("--json", action="store_true"); ls.add_argument("--full", action="store_true"); ls.add_argument("--top", type=int, default=None)
    pn = sub.add_parser("panel"); pn.add_argument("--top", type=int, default=5)
    args = p.parse_args(argv)
    if args.cmd == "validate":
        return cmd_validate(args)
    if args.cmd == "upsert":
        return cmd_upsert(args)
    if args.cmd == "dismiss":
        return cmd_dismiss(args)
    if args.cmd == "mark-recurrence":
        return cmd_mark_recurrence(args)
    if args.cmd == "list":
        return cmd_list(args)
    if args.cmd == "panel":
        args.json = False
        return cmd_list(args)
    return EXIT_ARGS


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception as e:
        eprint(f"error: {e}")
        sys.exit(1)

