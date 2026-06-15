#!/usr/bin/env python3
"""sync.py — keep this repo in sync with the live ~/.claude/skills/ runtime.

Model: LIVE-AUTHORITATIVE + CAPTURE. Skills are edited where they run
(~/.claude/skills/<name>/); this tool captures those changes back into the repo
(the durable, reviewed mirror + multi-machine source). The reverse direction
(deploy repo -> live) is the secondary path, for a fresh machine or a rollback.

Modes:
  --check     Report drift between live ~/.claude/skills/ and this repo. Read-only.
              exit 0 = in sync, exit 3 = drift found (so the drift hook can nudge),
              exit 2 = environment problem (live dir or repo not found).
  --capture   Copy live skills -> repo working tree (exclusions applied), print
              what changed + a cruft/secret pre-scan, then print the git/PR steps.
              Does NOT touch git — staging/commit/PR stays operator-driven so every
              capture still lands as a reviewed PR.
  --deploy    Copy repo skills -> live ~/.claude/skills/ (exclusions applied; never
              clobbers a live .local-state/). For a fresh machine or rollback.

A "skill" = a top-level directory containing a SKILL.md (in BOTH trees). Repo infra
(scripts/, hooks/, README.md, .gitignore, .git/) has no SKILL.md and is ignored.

Never copied in either direction: .local-state/, __pycache__/, *.pyc

stdlib only; Windows-safe (utf-8 reconfigure).
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_ENV = 2
EXIT_DRIFT = 3

EXCLUDE_DIRS = {".local-state", "__pycache__", ".git", "node_modules"}
EXCLUDE_FILE_SUFFIXES = (".pyc",)

# Cruft/secret patterns flagged (warn-only) when capturing NEW or changed files.
_SECRET_RE = re.compile(
    r"(sk-ant-[a-zA-Z0-9-]{8,}|ghp_[A-Za-z0-9]{20,}|github_pat_[0-9A-Za-z_]{22,}|"
    r"glpat-[0-9A-Za-z_-]{20,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|xox[baprs]-[A-Za-z0-9-]{10,})"
)
# Project-coupled absolute paths that shouldn't ride into a generic skill
# (~/.claude paths are fine; a hardcoded OTHER project under Projects/ is a smell).
# Matches forward-slash, backslash, AND WSL (/mnt/c/) forms on this Win11+WSL2 setup.
_PROJECT_PATH_RE = re.compile(
    r"(?:[Cc]:[\\/]|/mnt/c/)Users[\\/]Bryce[\\/]Projects[\\/](?!\*)[A-Za-z0-9._-]+"
)
_CRUFT_NAME_RE = re.compile(r"(^|/)seed_.*\.py$|(^|/)_tmp|(^|/)scratch")


def eprint(*a: object) -> None:
    print(*a, file=sys.stderr)


def live_skills_dir() -> Path:
    return (Path.home() / ".claude" / "skills").resolve()


def repo_root() -> Path:
    # scripts/sync.py -> repo root is the parent of scripts/
    env = os.environ.get("CLAUDE_GLOBAL_SKILLS_REPO")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parent.parent


def _skip(rel_parts) -> bool:
    return any(p in EXCLUDE_DIRS for p in rel_parts)


def list_skill_dirs(root: Path):
    """Top-level dirs under root that contain a SKILL.md."""
    out = {}
    if not root.exists():
        return out
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if child.name in EXCLUDE_DIRS:
            continue
        if (child / "SKILL.md").is_file():
            out[child.name] = child
    return out


def rel_files(skill_dir: Path):
    """Relative file paths under a skill dir, excluding noise."""
    files = set()
    for dirpath, dirnames, filenames in os.walk(skill_dir):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            if fn.endswith(EXCLUDE_FILE_SUFFIXES):
                continue
            full = Path(dirpath) / fn
            files.add(full.relative_to(skill_dir).as_posix())
    return files


def _read_norm(path: Path) -> bytes:
    """Read a file with line endings normalized (CRLF/CR -> LF), so a git
    autocrlf working tree (CRLF) compares equal to the LF live copy. The skills
    are all text; a content difference that is only line endings is NOT drift."""
    return path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _file_differs(a: Path, b: Path) -> bool:
    try:
        return _read_norm(a) != _read_norm(b)
    except OSError:
        return True


def diff_skill(live_dir: Path, repo_dir: Path):
    """Return (added, removed, changed) rel-paths comparing live -> repo.

    added   = in live, not in repo (would be captured)
    removed = in repo, not in live (live deleted them)
    changed = in both but content differs
    """
    live_files = rel_files(live_dir) if live_dir.exists() else set()
    repo_files = rel_files(repo_dir) if repo_dir.exists() else set()
    added = sorted(live_files - repo_files)
    removed = sorted(repo_files - live_files)
    changed = []
    for rel in sorted(live_files & repo_files):
        if _file_differs(live_dir / rel, repo_dir / rel):
            changed.append(rel)
    return added, removed, changed


def compute_drift(only=None):
    """Return a dict of per-skill drift between live and repo.

    only: if given, restrict the comparison to that single skill name (used by the
    drift hook so editing an in-sync skill doesn't nag about an unrelated one)."""
    live = list_skill_dirs(live_skills_dir())
    repo = list_skill_dirs(repo_root())
    names = sorted(set(live) | set(repo))
    if only is not None:
        names = [n for n in names if n == only]
    drift = {}
    for name in names:
        if name in live and name not in repo:
            drift[name] = {"status": "live-only (new)", "added": sorted(rel_files(live[name])),
                           "removed": [], "changed": []}
            continue
        if name in repo and name not in live:
            drift[name] = {"status": "repo-only (deleted live?)", "added": [],
                           "removed": sorted(rel_files(repo[name])), "changed": []}
            continue
        a, r, c = diff_skill(live[name], repo[name])
        if a or r or c:
            drift[name] = {"status": "changed", "added": a, "removed": r, "changed": c}
    return drift


def print_drift(drift) -> None:
    if not drift:
        print("in sync: live ~/.claude/skills/ matches the repo (no drift).")
        return
    print(f"DRIFT: {len(drift)} skill(s) differ between live and repo:\n")
    for name, d in drift.items():
        print(f"  {name}  [{d['status']}]")
        for rel in d["added"]:
            print(f"      + {rel}  (in live, not repo)")
        for rel in d["changed"]:
            print(f"      ~ {rel}  (content differs)")
        for rel in d["removed"]:
            print(f"      - {rel}  (in repo, not live)")
    print()


def cruft_scan(paths):
    """Warn-only scan of given absolute files for secrets/cruft."""
    warnings = []
    for p in paths:
        rel = p
        name = str(p).replace("\\", "/")
        if _CRUFT_NAME_RE.search(name):
            warnings.append(f"cruft-name: {name} (one-shot/scratch — exclude from the skill?)")
        try:
            text = Path(p).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _SECRET_RE.search(text):
            warnings.append(f"SECRET-LIKE: {name} (matches a token/key pattern)")
        proj_hits = sorted(set(m.group(0) for m in _PROJECT_PATH_RE.finditer(text)))
        if proj_hits:
            warnings.append(f"project-coupled path(s) in {name}: {', '.join(proj_hits[:4])}")
    return warnings


def do_check(only=None) -> int:
    if not live_skills_dir().exists():
        eprint(f"error: live skills dir not found: {live_skills_dir()}")
        return EXIT_ENV
    drift = compute_drift(only)
    print_drift(drift)
    return EXIT_DRIFT if drift else EXIT_OK


def _copy_skill(src: Path, dst: Path):
    """Copy src skill dir -> dst, applying exclusions. dst is replaced for tracked
    content but a pre-existing dst/.local-state/ is preserved (deploy safety)."""
    def ignore(dirpath, names):
        return [n for n in names if n in EXCLUDE_DIRS or n.endswith(EXCLUDE_FILE_SUFFIXES)]
    # Remove only the non-.local-state content of dst, then copy.
    if dst.exists():
        for child in dst.iterdir():
            if child.name == ".local-state":
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                try:
                    child.unlink()
                except OSError:
                    pass
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if item.name in EXCLUDE_DIRS:
            continue
        target = dst / item.name
        if item.is_dir():
            shutil.copytree(item, target, ignore=ignore, dirs_exist_ok=True)
        elif not item.name.endswith(EXCLUDE_FILE_SUFFIXES):
            shutil.copy2(item, target)


def do_capture() -> int:
    live = list_skill_dirs(live_skills_dir())
    if not live:
        eprint(f"error: no live skills found under {live_skills_dir()}")
        return EXIT_ENV
    drift = compute_drift()
    if not drift:
        print("nothing to capture: live and repo already in sync.")
        return EXIT_OK
    # Copy each drifted/new live skill into the repo (skip repo-only deletions —
    # those are surfaced for the operator to decide, not auto-deleted).
    captured = []
    new_or_changed_files = []
    for name, d in drift.items():
        if d["status"].startswith("repo-only"):
            print(f"NOTE: '{name}' exists in repo but not live — NOT deleting; "
                  f"resolve manually if it was intentionally removed.")
            continue
        src = live[name]
        dst = repo_root() / name
        _copy_skill(src, dst)
        captured.append(name)
        for rel in d["added"] + d["changed"]:
            new_or_changed_files.append(src / rel)
    print(f"\ncaptured {len(captured)} skill(s) into the repo: {', '.join(captured) or '(none)'}")
    # Cruft/secret pre-scan over the new/changed files.
    warnings = cruft_scan(new_or_changed_files)
    if warnings:
        print("\n! pre-scan warnings (review before committing):")
        for w in warnings:
            print(f"   - {w}")
    print("\nNext steps (capture stays operator-driven so it lands as a reviewed PR):")
    print("   git -C <repo> checkout -b sync/capture-<date>")
    print("   git -C <repo> add -A && git -C <repo> status")
    print("   # review the diff, then commit + open a PR; run /review-loop for the verdict")
    return EXIT_OK


def do_deploy() -> int:
    repo = list_skill_dirs(repo_root())
    if not repo:
        eprint(f"error: no skills found in repo {repo_root()}")
        return EXIT_ENV
    live_root = live_skills_dir()
    live_root.mkdir(parents=True, exist_ok=True)
    for name, src in repo.items():
        _copy_skill(src, live_root / name)
    print(f"deployed {len(repo)} skill(s) repo -> live (preserved any live .local-state/): "
          f"{', '.join(sorted(repo))}")
    return EXIT_OK


def main(argv) -> int:
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            try:
                s.reconfigure(encoding="utf-8")
            except Exception:
                pass
    p = argparse.ArgumentParser(prog="sync.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--check", action="store_true", help="report drift (read-only); exit 3 if drift")
    g.add_argument("--capture", action="store_true", help="copy live -> repo (exclusions); operator commits/PRs")
    g.add_argument("--deploy", action="store_true", help="copy repo -> live (fresh machine / rollback)")
    p.add_argument("--skill", default=None, help="with --check: limit the comparison to one skill name")
    args = p.parse_args(argv)
    if args.check:
        return do_check(args.skill)
    if args.capture:
        return do_capture()
    if args.deploy:
        return do_deploy()
    return EXIT_ENV


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:  # never crash the caller (a hook) hard
        eprint(f"error: {e}")
        sys.exit(1)
