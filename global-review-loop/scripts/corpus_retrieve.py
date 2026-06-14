#!/usr/bin/env python3
"""corpus_retrieve.py - cross-project corpus retrieval + false-positive filtering.

Self-contained (stdlib only). Enumerates Claude sessions across ALL registry
projects from BOTH local corpora -- the Claude Code CLI tree
(~/.claude/projects/<encoded-cwd>/*.jsonl) and the Cowork local-agent-mode tree
(sidecar local_*.json + audit.jsonl / outputs *.jsonl) -- within a --since
window, attributes each session to a project by cwd/originCwd PATH match against
registry.json (keyword only as a unique fallback), streams genuine human/
assistant turns line-by-line, and drops every documented false-positive class.

Every emitted turn/session carries an `attribution` field ('path'|'keyword'|
'none') so downstream stages can count ONLY path-attributed projects toward the
cross-project promotion bar (keyword over-attribution would manufacture false
global candidates).

Mirrors the APPROACH of Command's retroCorpus.ts / transcriptStream.ts /
chatlog.ts / scan-cowork-lam.ts / command-sessions.mjs and chat-arch's
jsonl.ts / cli.ts / classifyAutomation.ts / unwrapEnvelope.ts -- WITHOUT
importing any of that TypeScript.

Emits NDJSON (one object per line) to the REQUIRED --out file (turns or
sessions) + a coverage summary to stderr with --stats.

Privacy: emitted turns contain verbatim operator text. --out is REQUIRED and the
script REFUSES UNCONDITIONALLY (via the shared fail-closed _guards.assert_safe_out)
to write anywhere inside the ~/.claude config tree EXCEPT this skill's own
.local-state/, or inside any git working tree (walks up for a .git); it also
refuses if it cannot prove the path safe. The verbatim corpus must stay under the
skill's .local-state/.

Exit codes:
    0 ok
    2 bad time bound (non-finite --since/--until)
    5 invalid args (incl. missing --out)
    7 refused: unsafe --out (inside ~/.claude config tree or a git working tree,
      or safety could not be proven) — shared privacy guard
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Shared fail-closed privacy guard / identity helpers (single source of truth).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import _guards

EXIT_OK = 0
EXIT_BAD_BOUND = 2
EXIT_ARGS = 5
EXIT_GIT_TREE = 7

MAX_FILE_BYTES = 25 * 1024 * 1024  # files larger than this are recency-only
# In recency-only mode, also skip any single line larger than this so a huge
# SINGLE-record file (one giant JSON line, where the per-line ts skip never
# fires because there's only one line) doesn't get fully parsed. Best-effort.
MAX_LINE_BYTES = 2 * 1024 * 1024
CLEAN_CAP = 200

# ---- meta line types that are NOT genuine turns -----------------------------
META_TYPES = {
    "attachment",
    "file-history-snapshot",
    "ai-title",
    "last-prompt",
    "summary",
    "system",
}

# ---- raw-text wrapper prefixes (checked BEFORE cleaning) --------------------
RAW_NOISE_PREFIXES = (
    "<task-notification",
    "<scheduled-task",
    "<local-command-stdout",
    "<local-command-stderr",
    "<command-message",
    "<system-reminder",
    "<untrusted",
)

# ---- Command-automation first-prompt templates -----------------------------
# STRONG: unambiguous Command backend templates -- a bare prefix match is safe.
STRONG_MACHINE_PREFIXES = (
    "Facts (the only ground truth):",
    "Action to perform:",
    "Allowed actions:",
    "Action just completed:",
    "<untrusted",
    "Here is the whole fleet's state",
)
# WEAK: short, natural imperative English a real operator could plausibly type as
# a first message ("Invoke the deploy skill please"). A bare prefix match here
# would SILENTLY drop genuine cross-project friction sessions, so WEAK prefixes
# only classify as automation when a SECOND Command-structural marker co-occurs.
WEAK_MACHINE_PREFIXES = (
    "You are ranking",
    "Write one sentence instructing Claude Code",
    "The operator gave this feedback",
    "Research the following (read-only)",
    "Invoke the ",
)
# Distinctive Command-structural co-markers a real operator's opening line would
# not normally contain; used to confirm a WEAK-prefix match is truly automation.
_COMMAND_COMARKERS = (
    "Facts (the only ground truth):",
    "Allowed actions:",
    "Recent events (newest first):",
    "the whole fleet's state",
    "<untrusted",
)
# Back-compat alias (some --stats / docs reference the combined set).
MACHINE_PREFIXES = ("Project: ",) + STRONG_MACHINE_PREFIXES + WEAK_MACHINE_PREFIXES

# structural narrative-prompt discriminator: header PLUS a second anchor
_NARRATIVE_HEADER = re.compile(r"^Project: .+? \([a-z]+\)")
_NARRATIVE_ANCHOR_A = re.compile(r"Sessions: \d+ recorded")
_NARRATIVE_ANCHOR_B = re.compile(r"Recent events \(newest first\):")

# ---- cleaning regexes -------------------------------------------------------
_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
_CMDNAME_RE = re.compile(r"<command-name>\s*(.*?)\s*</command-name>", re.DOTALL)
_CMDARGS_RE = re.compile(r"<command-args>\s*(.*?)\s*</command-args>", re.DOTALL)
_WS_RE = re.compile(r"\s+")


def eprint(*a: object) -> None:
    print(*a, file=sys.stderr)


# ---------------------------------------------------------------------------
# Registry + project model
# ---------------------------------------------------------------------------
class Project:
    __slots__ = ("id", "path", "norm_paths", "keywords")

    def __init__(self, pid: str, path: str, aliases, keywords):
        self.id = pid
        self.path = path or ""
        roots = [path] if path else []
        roots.extend(aliases or [])
        self.norm_paths = [norm_path(r) for r in roots if r]
        self.keywords = [str(k).lower() for k in (keywords or []) if str(k).strip()]


def norm_path(p: str) -> str:
    """Lowercase, unify separators, strip trailing slash."""
    if not p:
        return ""
    s = str(p).replace("\\", "/").lower().rstrip("/")
    return s


def load_registry(path: Path):
    """Read registry.json .projects -> list[Project]. Never throws on bad data.

    Tolerates a UTF-8 BOM (registry.json is written by another tool)."""
    projects = []
    try:
        with path.open("r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except Exception as e:
        eprint(f"warning: could not read registry {path}: {e}")
        return projects
    for p in data.get("projects", []) or []:
        try:
            mh = p.get("matchHints") or {}
            aliases = mh.get("pathAliases") or []
            kws = list(mh.get("keywords") or []) + list(mh.get("coworkMarkers") or [])
            projects.append(Project(p.get("id", ""), p.get("path", ""), aliases, kws))
        except Exception:
            continue
    return projects


def attribute(session_cwd: str, first_text: str, projects):
    """Return (projectId, mode) where mode in {'path','keyword','none'}.

    Path-match wins (most-specific/longest); keyword fallback only when no path
    matches AND exactly one project keyword-matches.
    """
    nc = norm_path(session_cwd)
    if nc:
        best = None
        best_len = -1
        for proj in projects:
            for npth in proj.norm_paths:
                if not npth:
                    continue
                if nc == npth or nc.startswith(npth + "/"):
                    if len(npth) > best_len:
                        best_len = len(npth)
                        best = proj
        if best is not None:
            return best.id, "path"
    # keyword fallback -- unique only
    hay = (first_text or "").lower()
    hits = []
    for proj in projects:
        for kw in proj.keywords:
            if kw and kw in hay:
                hits.append(proj.id)
                break
    if len(set(hits)) == 1:
        return hits[0], "keyword"
    return "(unattributed)", "none"


# ---------------------------------------------------------------------------
# Defensive JSON + text helpers
# ---------------------------------------------------------------------------
def safe_json(line: str):
    try:
        return json.loads(line)
    except Exception:
        return None


def is_raw_noise(raw: str) -> bool:
    if not raw:
        return False
    s = raw.lstrip()
    for pfx in RAW_NOISE_PREFIXES:
        if s.startswith(pfx):
            return True
    return False


def clean_prompt_text(raw: str) -> str:
    """Prefer <command-name>+<command-args>; else strip residual tags, collapse ws, cap."""
    if not raw:
        return ""
    name_m = _CMDNAME_RE.search(raw)
    if name_m:
        name = name_m.group(1).strip()
        args_m = _CMDARGS_RE.search(raw)
        args = args_m.group(1).strip() if args_m else ""
        text = (name + (" " + args if args else "")).strip()
    else:
        text = _TAG_RE.sub(" ", raw)
    text = _WS_RE.sub(" ", text).strip()
    if len(text) > CLEAN_CAP:
        text = text[: CLEAN_CAP - 1] + "\u2026"
    return text


def _has_narrative_anchor(first_prompt: str) -> bool:
    return bool(
        _NARRATIVE_ANCHOR_A.search(first_prompt)
        or _NARRATIVE_ANCHOR_B.search(first_prompt)
    )


def is_automation(first_prompt: str) -> bool:
    """STRONG prefixes match bare; WEAK (natural-English) prefixes require a
    SECOND Command-structural co-marker so a genuine operator first message isn't
    over-filtered; 'Project: ' requires the structural narrative discriminator."""
    if not first_prompt:
        return False
    fp = first_prompt.lstrip()
    # 'Project: ' -> header + a narrative anchor
    if fp.startswith("Project: ") and _NARRATIVE_HEADER.match(fp) and _has_narrative_anchor(first_prompt):
        return True
    # STRONG templates -> a bare prefix match is enough
    for pfx in STRONG_MACHINE_PREFIXES:
        if fp.startswith(pfx):
            return True
    # WEAK (ambiguous English) -> require a co-occurring Command-structural marker
    for pfx in WEAK_MACHINE_PREFIXES:
        if fp.startswith(pfx) and (
            _has_narrative_anchor(first_prompt)
            or any(m in first_prompt for m in _COMMAND_COMARKERS)
        ):
            return True
    # structural narrative check independent of the prefix list
    if _NARRATIVE_HEADER.match(fp) and _has_narrative_anchor(first_prompt):
        return True
    return False


def parse_ts_ms(obj: dict):
    """Extract a turn's top-level ISO timestamp -> epoch ms, or None."""
    ts = obj.get("timestamp")
    if not isinstance(ts, str):
        return None
    try:
        s = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def extract_turn(obj: dict):
    """Return {'role','text'} for a genuine human/assistant turn, else None."""
    if not isinstance(obj, dict):
        return None
    top_type = obj.get("type")
    if top_type in META_TYPES:
        return None
    if top_type == "queue-operation" and obj.get("operation") != "enqueue":
        return None
    if obj.get("isMeta") is True:
        return None
    if obj.get("isSidechain") is True:
        return None
    if obj.get("isApiErrorMessage") is True:
        return None

    msg = obj.get("message")
    role = None
    content = None
    if isinstance(msg, dict):
        role = msg.get("role")
        content = msg.get("content")
    else:
        role = obj.get("role")
        content = obj.get("content")
    # Cowork enqueue verbatim sometimes carries a flat 'text'/'prompt'
    if content is None:
        content = obj.get("text") or obj.get("prompt")
    if role not in ("user", "assistant"):
        # Cowork enqueue events have no role but are user prompts
        if top_type == "queue-operation" and obj.get("operation") == "enqueue":
            role = "user"
        else:
            return None

    parts = []
    only_tool_result = True
    if isinstance(content, str):
        only_tool_result = False
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                if isinstance(block, str):
                    only_tool_result = False
                    parts.append(block)
                continue
            btype = block.get("type")
            if btype == "tool_result":
                continue  # tool output, not typed input
            only_tool_result = False
            if btype == "tool_use":
                if block.get("name") == "TodoWrite":
                    return None
                continue
            if btype == "thinking":
                continue
            if btype in ("text", None):
                t = block.get("text") or block.get("content") or ""
                if isinstance(t, str):
                    parts.append(t)
        if not parts and only_tool_result:
            return None
    else:
        return None

    raw = "\n".join(p for p in parts if p).strip()
    if not raw:
        return None
    if is_raw_noise(raw):  # raw check BEFORE cleaning
        return None
    return {"role": role, "raw": raw, "text": clean_prompt_text(raw)}


# ---------------------------------------------------------------------------
# CLI corpus enumeration
# ---------------------------------------------------------------------------
def cli_first_lines(fp: Path, n: int = 40):
    """Read up to n lines (for cwd + first prompt), defensively."""
    out = []
    try:
        with fp.open("r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= n:
                    break
                out.append(line)
    except Exception:
        return []
    return out


def session_cwd_from_lines(lines):
    for line in lines:
        obj = safe_json(line.strip())
        if isinstance(obj, dict):
            cwd = obj.get("cwd")
            if isinstance(cwd, str) and cwd:
                return cwd
    return ""


def session_first_prompt_from_lines(lines):
    for line in lines:
        obj = safe_json(line.strip())
        if obj is None:
            continue
        turn = extract_turn(obj)
        if turn and turn["role"] == "user":
            return turn["text"]
    return ""


def enumerate_cli_sessions(projects_dir: Path, since_ms, stats):
    if not projects_dir.exists():
        return
    for enc_dir in sorted(projects_dir.iterdir()):
        if not enc_dir.is_dir():
            continue
        # skip CLI subprocess noise dirs
        if "-sessions-" in enc_dir.name:
            continue
        for fp in sorted(enc_dir.glob("*.jsonl")):
            stats["cli_files"] += 1
            try:
                st = fp.stat()
            except OSError:
                continue
            mtime_ms = int(st.st_mtime * 1000)
            if since_ms is not None and mtime_ms < since_ms:
                continue
            lines = cli_first_lines(fp)
            cwd = session_cwd_from_lines(lines)
            first_prompt = session_first_prompt_from_lines(lines)
            yield {
                "source": "cli",
                "sessionId": fp.stem,
                "filePath": str(fp),
                "cwd": cwd,
                "firstPrompt": first_prompt,
                "lastActivityMs": mtime_ms,
                "title": "",
                "size": st.st_size,
            }


def stream_cli_turns(fp: Path, since_ms, until_ms, size):
    """Yield genuine turns from a CLI jsonl, line-by-line."""
    recency_only = size > MAX_FILE_BYTES
    try:
        with fp.open("r", encoding="utf-8", errors="replace") as fh:
            for idx, line in enumerate(fh):
                line = line.strip()
                if not line:
                    continue
                # recency-only: skip a pathologically huge single record so a
                # one-giant-line file is capped too (per-line ts skip can't help).
                if recency_only and len(line) > MAX_LINE_BYTES:
                    continue
                obj = safe_json(line)
                if obj is None:
                    continue
                turn = extract_turn(obj)
                if turn is None:
                    continue
                ts = parse_ts_ms(obj)
                if recency_only and ts is None:
                    continue
                if ts is not None:
                    if since_ms is not None and ts < since_ms:
                        continue
                    if until_ms is not None and ts > until_ms:
                        continue
                yield {"role": turn["role"], "text": turn["text"], "tsMs": ts, "turn": idx}
    except Exception:
        return


# ---------------------------------------------------------------------------
# Cowork corpus enumeration
# ---------------------------------------------------------------------------
def enumerate_cowork_sessions(sidecar_dirs, audit_dir: Path, since_ms, stats):
    seen = set()
    for sdir in sidecar_dirs:
        if not sdir.exists():
            continue
        for sidecar in sdir.rglob("local_*.json"):
            stats["cowork_files"] += 1
            obj = None
            try:
                with sidecar.open("r", encoding="utf-8-sig", errors="replace") as fh:
                    obj = json.load(fh)
            except Exception:
                continue
            if not isinstance(obj, dict):
                continue
            sid = obj.get("sessionId") or sidecar.stem
            if sid in seen:
                continue
            seen.add(sid)
            cwd = obj.get("originCwd") or obj.get("cwd") or ""
            title = obj.get("title") or ""
            la = obj.get("lastActivityAt")
            la_ms = None
            if isinstance(la, (int, float)):
                la_ms = int(la if la > 1e12 else la * 1000)
            elif isinstance(la, str):
                try:
                    dt = datetime.fromisoformat(la.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    la_ms = int(dt.timestamp() * 1000)
                except Exception:
                    la_ms = None
            if la_ms is None:
                try:
                    la_ms = int(sidecar.stat().st_mtime * 1000)
                except OSError:
                    la_ms = 0
            if since_ms is not None and la_ms < since_ms:
                continue
            # locate transcript under audit_dir/<org>/<user>/local_<sid>/
            transcript = locate_cowork_transcript(audit_dir, sidecar, sid)
            # populate firstPrompt from the first GENUINE user turn so the
            # automation discriminator + keyword attribution actually fire on
            # the Cowork corpus (mirrors the CLI path). Cron titles short-circuit.
            if title.lstrip().startswith("<scheduled-task"):
                first_prompt = title  # cron -> will be classified out
            else:
                first_prompt = cowork_first_prompt(transcript)
            # recency-only size cap: stat the transcript files (Cowork was
            # previously hardcoded to 0, exempting it from the cap).
            size = cowork_transcript_size(transcript)
            yield {
                "source": "cowork",
                "sessionId": sid,
                "filePath": str(transcript) if transcript else "",
                "cwd": cwd,
                "firstPrompt": first_prompt,
                "lastActivityMs": la_ms,
                "title": title,
                "size": size,
            }


def locate_cowork_transcript(audit_dir: Path, sidecar: Path, sid: str):
    """Join sidecar's <org>/<user> path onto the audit tree + local_<sid>.

    The real transcript dirs are named `local_<sessionId>`, NOT the bare
    sessionId -- looking up the bare id misses every Cowork session.
    """
    if not audit_dir.exists():
        return None
    dir_name = f"local_{sid}"
    # sidecar path is .../claude-code-sessions/<org>/<user>/local_*.json
    try:
        org_user = sidecar.parent  # <user>
        user = org_user.name
        org = org_user.parent.name
        cand = audit_dir / org / user / dir_name
        if cand.exists():
            return cand
    except Exception:
        pass
    # fallback: search by transcript dir name (local_<sid>)
    for cand in audit_dir.rglob(dir_name):
        if cand.is_dir():
            return cand
    return None


def cowork_first_prompt(transcript_dir):
    """First GENUINE user turn from a Cowork transcript dir, '' if none.

    Prefer outputs enqueue events; fall back to audit.jsonl. Mirrors the CLI
    `session_first_prompt_from_lines` semantics (uses extract_turn so the same
    false-positive classes are dropped).
    """
    if not transcript_dir or not transcript_dir.exists():
        return ""
    sources = sorted(transcript_dir.glob("outputs/**/*.jsonl"))
    audit = transcript_dir / "audit.jsonl"
    if audit.exists():
        sources.append(audit)
    for fp in sources:
        try:
            with fp.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    obj = safe_json(line)
                    if obj is None:
                        continue
                    turn = extract_turn(obj)
                    if turn and turn["role"] == "user":
                        return turn["text"]
        except Exception:
            continue
    return ""


def cowork_transcript_size(transcript_dir):
    """Total bytes of a Cowork transcript's turn files (outputs + audit.jsonl),
    for the recency-only cap. 0 when the transcript can't be located/stat'd."""
    if not transcript_dir or not transcript_dir.exists():
        return 0
    total = 0
    files = list(transcript_dir.glob("outputs/**/*.jsonl"))
    audit = transcript_dir / "audit.jsonl"
    if audit.exists():
        files.append(audit)
    for fp in files:
        try:
            total += fp.stat().st_size
        except OSError:
            continue
    return total


def stream_cowork_turns(transcript_dir: Path, since_ms, until_ms, size=0):
    """Prefer outputs enqueue verbatim; fall back to audit.jsonl. De-dupe.

    Turn ordinals are a single monotonic counter owned here, incremented per
    YIELDED turn -- so sourceRefs are unique and ordered across all output
    files of the session (line-position + per-file resets previously produced
    non-monotonic, colliding turn numbers).
    """
    if not transcript_dir or not transcript_dir.exists():
        return
    recency_only = size > MAX_FILE_BYTES
    emitted = set()  # (normalized_text, ts//1000) dedupe keys
    turn_no = 0
    outputs = list(transcript_dir.glob("outputs/**/*.jsonl"))
    if outputs:
        files = sorted(outputs)
    else:
        files = []
        audit = transcript_dir / "audit.jsonl"
        if audit.exists():
            files = [audit]
    for f in files:
        for t in _stream_jsonl_turns(f, since_ms, until_ms, recency_only):
            key = (_norm_dedupe(t["text"]), (t["tsMs"] or 0) // 1000)
            if key in emitted:
                continue
            emitted.add(key)
            t["turn"] = turn_no
            turn_no += 1
            yield t


def _norm_dedupe(text: str) -> str:
    return _WS_RE.sub(" ", (text or "").lower()).strip()


def _stream_jsonl_turns(fp: Path, since_ms, until_ms, recency_only=False):
    """Yield genuine turns from one jsonl file. `turn` is left unset here --
    stream_cowork_turns assigns the monotonic ordinal per yielded turn."""
    try:
        with fp.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                # recency-only: skip a pathologically huge single record so a
                # one-giant-line file is capped too (per-line ts skip can't help).
                if recency_only and len(line) > MAX_LINE_BYTES:
                    continue
                obj = safe_json(line)
                if obj is None:
                    continue
                turn = extract_turn(obj)
                if turn is None:
                    continue
                ts = parse_ts_ms(obj)
                if recency_only and ts is None:
                    continue
                if ts is not None:
                    if since_ms is not None and ts < since_ms:
                        continue
                    if until_ms is not None and ts > until_ms:
                        continue
                yield {"role": turn["role"], "text": turn["text"], "tsMs": ts}
    except Exception:
        return


# ---------------------------------------------------------------------------
# Time-bound parsing
# ---------------------------------------------------------------------------
def parse_date_ms(s: str, end_of_day: bool = False):
    if s is None:
        return None
    try:
        dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        eprint(f"error: bad date {s!r}; expected YYYY-MM-DD")
        sys.exit(EXIT_BAD_BOUND)
    ms = int(dt.timestamp() * 1000)
    if end_of_day:
        ms += 24 * 3600 * 1000 - 1
    return ms


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def default_paths():
    home = Path.home()
    cli = home / ".claude" / "projects"
    roam = home / "AppData" / "Roaming" / "Claude"
    sidecars = [roam / "claude-code-sessions", roam / "local-agent-mode-sessions"]
    audit = roam / "local-agent-mode-sessions"
    return cli, sidecars, audit


def parse_args(argv):
    p = argparse.ArgumentParser(prog="corpus_retrieve.py")
    p.add_argument("--since", default=None, help="YYYY-MM-DD lower bound on last-activity")
    p.add_argument("--until", default=None, help="YYYY-MM-DD upper bound (turn-level)")
    p.add_argument("--registry", default="C:/Users/Bryce/Projects/Command/registry.json")
    p.add_argument("--project", default=None, help="restrict to one projectId")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--turns", action="store_true")
    mode.add_argument("--sessions", action="store_true")
    p.add_argument("--include-automated", action="store_true")
    # Promotion corpus default: keep keyword + unattributed sessions for INSPECTION,
    # but every turn is stamped with `attribution` so the promotion bar counts
    # only path-attributed projects. Use --path-only to drop non-path turns.
    p.add_argument("--path-only", action="store_true",
                   help="emit ONLY path-attributed turns/sessions")
    p.add_argument("--exclude-unattributed", action="store_true")
    p.add_argument("--stats", action="store_true")
    p.add_argument("--out", default=None, required=True,
                   help="REQUIRED: write NDJSON here; refuses a git-tree or "
                        "~/.claude config-tree path, except this skill's "
                        ".local-state/ (privacy guard)")
    p.add_argument("--cli-dir", default=None)
    p.add_argument("--sidecar-dir", action="append", default=None)
    p.add_argument("--audit-dir", default=None)
    return p.parse_args(argv)


def main(argv):
    # C4: machine default is cp1252 -> reconfigure both streams to utf-8 at the
    # very start so em-dash / smart-quote / arrow in verbatim corpus and the
    # stats summary don't crash with UnicodeEncodeError on the first real turn.
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")

    args = parse_args(argv)
    emit_sessions = bool(args.sessions) and not args.turns

    # privacy guard runs UNCONDITIONALLY on the resolved --out (which is required).
    # Fail-closed shared guard: refuses ~/.claude (except this skill's
    # .local-state/) and any git working tree, and refuses on any error (exit 7).
    out_path = _guards.assert_safe_out(args.out)

    since_ms = parse_date_ms(args.since) if args.since else None
    until_ms = parse_date_ms(args.until, end_of_day=True) if args.until else None

    cli_dir, sidecar_dirs, audit_dir = default_paths()
    if args.cli_dir:
        cli_dir = Path(args.cli_dir)
    if args.sidecar_dir:
        sidecar_dirs = [Path(s) for s in args.sidecar_dir]
    if args.audit_dir:
        audit_dir = Path(args.audit_dir)

    projects = load_registry(Path(args.registry))

    stats = {
        "cli_files": 0,
        "cowork_files": 0,
        "sessions_kept": 0,
        "sessions_dropped_automation": 0,
        "sessions_unattributed": 0,
        "sessions_keyword": 0,
        "turns_emitted": 0,
        "by_project": {},
    }

    def handle_session(sess):
        pid, mode = attribute(sess["cwd"], sess["firstPrompt"], projects)
        sess["projectId"] = pid
        sess["attribution"] = mode
        # classify automation on the first genuine prompt
        fp = sess.get("firstPrompt") or sess.get("title") or ""
        automated = is_automation(fp) or fp.lstrip().startswith("<scheduled-task")
        sess["automated"] = automated
        if automated and not args.include_automated:
            stats["sessions_dropped_automation"] += 1
            return None
        if mode == "keyword":
            stats["sessions_keyword"] += 1
        if pid == "(unattributed)":
            stats["sessions_unattributed"] += 1
            if args.exclude_unattributed:
                return None
        # path-only mode: drop anything not path-attributed
        if args.path_only and mode != "path":
            return None
        if args.project and pid != args.project:
            return None
        stats["sessions_kept"] += 1
        stats["by_project"][pid] = stats["by_project"].get(pid, 0) + 1
        return sess

    # --out is required and already passed the privacy guard; write to the
    # guard-resolved path (utf-8).
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_fh = open(out_path, "w", encoding="utf-8", newline="\n")
    out = out_fh

    def emit(obj):
        out.write(json.dumps(obj, ensure_ascii=False) + "\n")

    try:
        # CLI corpus
        for sess in enumerate_cli_sessions(cli_dir, since_ms, stats):
            kept = handle_session(sess)
            if kept is None:
                continue
            if emit_sessions:
                emit({
                    "projectId": kept["projectId"], "source": "cli",
                    "sessionId": kept["sessionId"], "filePath": kept["filePath"],
                    "firstPrompt": kept["firstPrompt"], "lastActivityMs": kept["lastActivityMs"],
                    "automated": kept["automated"], "attribution": kept["attribution"],
                })
            else:
                for t in stream_cli_turns(Path(kept["filePath"]), since_ms, until_ms, kept.get("size", 0)):
                    stats["turns_emitted"] += 1
                    emit({
                        "projectId": kept["projectId"], "attribution": kept["attribution"],
                        "source": "cli", "sessionId": kept["sessionId"],
                        "filePath": kept["filePath"],
                        "role": t["role"], "text": t["text"], "tsMs": t["tsMs"],
                        "sourceRef": f'{kept["sessionId"]}#{t["turn"]}',
                    })

        # Cowork corpus
        for sess in enumerate_cowork_sessions(sidecar_dirs, audit_dir, since_ms, stats):
            kept = handle_session(sess)
            if kept is None:
                continue
            if emit_sessions:
                emit({
                    "projectId": kept["projectId"], "source": "cowork",
                    "sessionId": kept["sessionId"], "filePath": kept["filePath"],
                    "firstPrompt": kept["firstPrompt"], "lastActivityMs": kept["lastActivityMs"],
                    "automated": kept["automated"], "attribution": kept["attribution"],
                })
            else:
                tdir = Path(kept["filePath"]) if kept["filePath"] else None
                if tdir is None:
                    continue
                for t in stream_cowork_turns(tdir, since_ms, until_ms, kept.get("size", 0)):
                    stats["turns_emitted"] += 1
                    emit({
                        "projectId": kept["projectId"], "attribution": kept["attribution"],
                        "source": "cowork", "sessionId": kept["sessionId"],
                        "filePath": kept["filePath"],
                        "role": t["role"], "text": t["text"], "tsMs": t["tsMs"],
                        "sourceRef": f'{kept["sessionId"]}#{t["turn"]}',
                    })
    finally:
        if out_fh is not None:
            out_fh.close()

    if args.stats:
        eprint("=== corpus_retrieve coverage ===")
        eprint(f"  CLI files scanned    : {stats['cli_files']}")
        eprint(f"  Cowork files scanned : {stats['cowork_files']}")
        eprint(f"  sessions kept        : {stats['sessions_kept']}")
        eprint(f"  dropped (automation) : {stats['sessions_dropped_automation']}")
        eprint(f"  keyword-attributed   : {stats['sessions_keyword']} "
               f"(NOT counted toward the cross-project bar)")
        eprint(f"  unattributed         : {stats['sessions_unattributed']}")
        eprint(f"  turns emitted        : {stats['turns_emitted']}")
        top = sorted(stats["by_project"].items(), key=lambda kv: -kv[1])[:15]
        eprint("  kept sessions by project (top 15):")
        for pid, n in top:
            eprint(f"    {pid:30s} {n}")
        eprint("  filtered classes: tool_result, <task-notification>/<scheduled-task>/"
               "<local-command>/<system-reminder>/<command-message>/<untrusted>, "
               "TodoWrite, isMeta, isSidechain/isApiErrorMessage, META_TYPES, "
               "subagent files, Cowork audit/outputs dupes.")
        eprint("  NOTE: file counts are a PROXY; 'sessions kept' is the genuine-session "
               "count; only PATH-attributed projects count toward promotion.")

    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception as e:
        eprint(f"error: {e}")
        sys.exit(1)

