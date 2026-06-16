#!/usr/bin/env python3
"""reconcile_global_config.py - drop already-encoded candidates against ~/.claude.

Self-contained (stdlib only). Reads the GLOBAL Claude config surface:
    ~/.claude/CLAUDE.md
    ~/.claude/skills/<name>/SKILL.md
    ~/.claude/projects/<encoded-cwd>/memory/*.md   (+ MEMORY.md)
and deterministically decides, per candidate, whether it is ALREADY ENCODED so
the skill drops it. Mirrors the APPROACH of chat-arch's configIngest discover->
parse and Command's configReader.ts (global surface) + reconcile.ts (configExists
lexical predicate, keyTokens tokenizer, config-domain stopwords, three OR'd
existence paths, semantic-dup STOP flag) WITHOUT importing the TS.

Read-only. Never throws on missing files. Tolerates a UTF-8 BOM in any config
file (those are written by other tools). Exits non-zero on an EMPTY surface so a
broken reader (wrong HOME) cannot fake-pass every candidate.

Honest limit: LEXICAL match only. A candidate reworded from an equivalent rule
already in config can survive as a false-novel; borderline scores get stop_flag.

Subcommands:
    reconcile <candidates.json> [--threshold T] [--home DIR]
    surface   [--home DIR]

Exit codes: 0 ok, 2 empty surface (broken-reader guard), 5 bad args.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_THRESHOLD = 0.6
STOP_BAND = 0.15

CONFIG_STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "for", "with",
    "add", "set", "always", "never", "ensure", "should", "must", "make",
    "use", "when", "then", "if", "claude", "md", "skill", "skills",
    "settings", "json", "config", "rule", "this", "that", "be", "is", "are",
    "you", "your", "we", "our", "i", "my", "it", "as", "by", "from", "do",
    "via", "per", "new", "global", "file", "files", "change", "fix",
}

_TOK_RE = re.compile(r"[a-z0-9][a-z0-9._-]{2,}")


def eprint(*a):
    print(*a, file=sys.stderr)


def resolve_home(home_arg):
    if home_arg:
        return Path(home_arg).expanduser()
    return Path.home() / ".claude"


def read_text(p: Path) -> str:
    try:
        # utf-8-sig tolerates a BOM written by another tool
        return p.read_text(encoding="utf-8-sig", errors="replace")
    except Exception:
        return ""


def read_global_surface(home=None) -> dict:
    """Enumerate the three surfaces. Missing/unreadable = absent (never raises)."""
    root = resolve_home(home)
    sources = []
    claude_md = ""
    skill_names = []
    skill_text_parts = []
    memory_titles = []
    memory_text_parts = []

    cm = root / "CLAUDE.md"
    if cm.exists():
        claude_md = read_text(cm)
        sources.append({"path": str(cm), "kind": "claude-md"})

    skills_dir = root / "skills"
    if skills_dir.exists():
        for d in sorted(skills_dir.iterdir()):
            if d.is_dir():
                sm = d / "SKILL.md"
                if sm.exists():
                    skill_names.append(d.name.lower())
                    skill_text_parts.append(read_text(sm))
                    sources.append({"path": str(sm), "kind": "skill"})

    # memories are PER-PROJECT; glob across all projects (case-insensitive via glob)
    projects_dir = root / "projects"
    if projects_dir.exists():
        for md in projects_dir.glob("*/memory/*.md"):
            txt = read_text(md)
            if not txt:
                continue
            first = ""
            for line in txt.splitlines():
                s = line.strip().lstrip("#").strip()
                if s:
                    first = s
                    break
            memory_titles.append(first.lower())
            memory_text_parts.append(txt)
            sources.append({"path": str(md), "kind": "memory"})

    skill_text = "\n".join(skill_text_parts)
    memory_text = "\n".join(memory_text_parts)
    blob = "\n".join([claude_md, skill_text, memory_text]).lower()
    return {
        "claude_md": claude_md,
        "skill_names": skill_names,
        "skill_text": skill_text,
        "memory_titles": memory_titles,
        "memory_text": memory_text,
        "blob": blob,
        "sources": sources,
    }


def global_config_text(home=None) -> str:
    """Convenience: the concatenated global config text (the matching corpus)."""
    return read_global_surface(home)["blob"]


def key_tokens(text: str):
    if not text:
        return set()
    toks = _TOK_RE.findall(text.lower())
    return {t for t in toks if len(t) >= 3 and t not in CONFIG_STOPWORDS}


def _skill_name_from_hint(change, target_hint):
    if target_hint and isinstance(target_hint, str):
        m = re.search(r"skill[:/]\s*([a-z0-9][a-z0-9_-]+)", target_hint.lower())
        if m:
            return m.group(1)
    m = re.search(r"skills/([a-z0-9][a-z0-9_-]+)", (change or "").lower())
    if m:
        return m.group(1)
    return None


def already_encoded(change, surface, target_hint=None, threshold=DEFAULT_THRESHOLD):
    """Three OR'd paths. Returns {encoded, frac, matched_sources, reason, stop_flag}."""
    # Path 1: skill-name exact match (highest precision)
    sk = _skill_name_from_hint(change, target_hint)
    if sk and sk in surface["skill_names"]:
        return {
            "encoded": True, "frac": 1.0,
            "matched_sources": [f"skill:{sk}"],
            "reason": f"skill '{sk}' already exists in ~/.claude/skills/",
            "stop_flag": False,
        }
    # Path 2: memory-title overlap
    ctoks = key_tokens(change)
    best_title_frac = 0.0
    for title in surface["memory_titles"]:
        ttoks = key_tokens(title)
        if not ttoks:
            continue
        frac = len(ctoks & ttoks) / max(1, len(ctoks))
        if frac > best_title_frac:
            best_title_frac = frac
    if best_title_frac >= threshold:
        return {
            "encoded": True, "frac": round(best_title_frac, 3),
            "matched_sources": ["memory-title"],
            "reason": f"memory title overlaps at {best_title_frac:.2f}",
            "stop_flag": False,
        }
    # Path 3: token overlap against the corpus blob
    if not ctoks:
        return {"encoded": False, "frac": 0.0, "matched_sources": [],
                "reason": "no key tokens in candidate", "stop_flag": False}
    blob = surface["blob"]
    present = sum(1 for t in ctoks if t in blob)
    frac = present / len(ctoks)
    if frac >= threshold:
        srcs = []
        if any(t in surface["claude_md"].lower() for t in ctoks):
            srcs.append("~/.claude/CLAUDE.md")
        if any(t in surface["skill_text"].lower() for t in ctoks):
            srcs.append("skills/*/SKILL.md")
        if any(t in surface["memory_text"].lower() for t in ctoks):
            srcs.append("memory/*.md")
        return {
            "encoded": True, "frac": round(frac, 3),
            "matched_sources": srcs or ["global-blob"],
            "reason": f"{present}/{len(ctoks)} key tokens present in global surface",
            "stop_flag": False,
        }
    stop = (threshold - STOP_BAND) <= frac < threshold
    return {
        "encoded": False, "frac": round(frac, 3), "matched_sources": [],
        "reason": f"only {present}/{len(ctoks)} key tokens present (< {threshold})",
        "stop_flag": stop,
    }


def reconcile(candidates, surface=None, threshold=DEFAULT_THRESHOLD, home=None):
    if surface is None:
        surface = read_global_surface(home)
    out = []
    for c in candidates:
        cid = c.get("id") or c.get("proposalId") or ""
        change = c.get("change") or c.get("canonicalRule") or c.get("proposedChange") or ""
        hint = c.get("targetHint") or (c.get("proposedUpgrade") or {}).get("target")
        verdict = already_encoded(change, surface, hint, threshold)
        verdict["id"] = cid
        # OS6: echo the candidate body so reconciled.json is self-contained for
        # the next step (no second read of the input candidates required).
        verdict["candidate"] = {
            "id": cid,
            "summary": c.get("summary", ""),
            "proposedChange": c.get("proposedChange") or change,
            "evidence": c.get("evidence", []),
            "distinctProjectCount": c.get("distinctProjectCount"),
            "scope": c.get("scope"),
        }
        out.append(verdict)
    return out


def main(argv):
    # C4: machine default is cp1252; reconfigure so non-ASCII (em-dash, smart
    # quotes, arrows) in config text printed to stdout/stderr does not crash.
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="reconcile_global_config.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("reconcile")
    r.add_argument("candidates")
    r.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    r.add_argument("--home", default=None)
    s = sub.add_parser("surface")
    s.add_argument("--home", default=None)
    args = parser.parse_args(argv)

    surface = read_global_surface(args.home)
    if not surface["blob"].strip():
        eprint("WARN: empty global surface (broken reader or wrong HOME). "
               "Refusing to pass every candidate as novel.")
        return 2

    if args.cmd == "surface":
        print(json.dumps({
            "n_sources": len(surface["sources"]),
            "skill_names": surface["skill_names"],
            "n_memory_files": len(surface["memory_titles"]),
            "claude_md_chars": len(surface["claude_md"]),
            "sources": surface["sources"],
        }, ensure_ascii=False, indent=2))
        return 0

    # reconcile
    try:
        with open(args.candidates, "r", encoding="utf-8") as fh:
            cands = json.load(fh)
    except Exception as e:
        eprint(f"error: cannot read {args.candidates}: {e}")
        return 5
    # accept either a bare list or the recurrence_promote output shape
    if isinstance(cands, dict) and "globalCandidates" in cands:
        cands = cands["globalCandidates"]
    if not isinstance(cands, list):
        eprint("error: candidates must be a JSON array")
        return 5

    results = reconcile(cands, surface, args.threshold)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    dropped = [x for x in results if x["encoded"]]
    flagged = [x for x in results if x["stop_flag"]]
    eprint(f"reconcile: {len(results)} candidates, {len(dropped)} already-encoded "
           f"(dropped), {len(flagged)} borderline (stop_flag).")
    eprint("NOTE: lexical match only; reworded duplicates can slip through.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception as e:
        eprint(f"error: {e}")
        sys.exit(1)

