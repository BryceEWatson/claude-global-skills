#!/usr/bin/env python3
"""sync.py — repo-authoritative cross-runtime sync for global skills.

Model: REPO-AUTHORITATIVE (canonical) + materialized deploy. This repository is the
source of truth. Each skill deploys to one or more *targets* (products):

    claude -> ~/.claude/skills   (override: $CLAUDE_SKILLS_DIR)
    codex  -> ~/.codex/skills    (override: $CODEX_SKILLS_DIR)

A skill declares its targets via a top-level `targets:` key in SKILL.md frontmatter
(absent -> [claude], which keeps every pre-existing Claude-only skill working
unchanged). Shared content lives ONCE in the skill directory; files that belong to a
single target live under <skill>/overlays/<target>/ and are *added* (additive-only)
onto the shared content when deploying to that target. A small {{SKILL_HOME}} token
in shared text files expands to the target's install path so one shared SKILL.md
produces a product-correct command in each install.

Modes:
  --check     Report drift between each target's live copy and the repo-materialized
              expected output (live vs forward-materialize, LF-normalized). Read-only.
              exit 0 = in sync, 3 = drift, 2 = environment problem, 1 = config error.
  --deploy    Materialize repo -> live for each skill's declared targets. Never
              clobbers a live .local-state/; only touches <home>/<skill>/, so sibling
              and unrelated installed skills (and other product homes) are untouched.
  --capture   Reverse-map ONE target's live copy -> repo (requires --target). Refuses
              on divergence, on an unclassifiable live file, or when the capture
              would ADD private content to the repo (see the privacy gate below);
              writes file-by-file (never wipes the skill dir), leaving other
              targets' overlays untouched. Does NOT touch git — staging/commit/PR
              stays operator-driven.

Capture privacy gate: capture is the ONE direction that can leak, because it pulls
the live tree into a public repo, and the live tree accumulates operator-private
detail (absolute home paths, emails, session ids, client codenames) that the
published copy deliberately generalizes. Lines that capture would ADD are scanned
before anything is written, and the whole skill is refused if any match. Generic
shapes are built in; literal names live in a gitignored `.capture-private-terms` at
the repo root (one per line, case-insensitive substrings), because listing them in
tracked source would publish the very names the gate withholds. A term already
present in the repo's copy of a file is sanctioned, so a skill that legitimately
names the terms it redacts does not trip the gate.

Default target set (--check / --deploy, no --target): each skill's declared targets
whose live home directory already exists. This is symmetric across check and deploy
(so deploy->check round-trips cleanly) and never creates or nags about a product home
that isn't present — with ONE deploy-only exception: a bare `--deploy` always installs
the baseline `claude` target (creating ~/.claude/skills if absent) so a first run on a
fresh machine actually installs something instead of exiting 0 having done nothing.
`codex` stays opt-in (deployed by default only if ~/.codex/skills already exists). An
explicit --target forces that target (and, for deploy, creates its home). --target
both = {claude, codex}; invalid for --capture.

A "skill" = a top-level directory containing a SKILL.md. Repo infra (scripts/, hooks/,
docs/, README.md, .git/) has no SKILL.md and is ignored.

Never copied in either direction: .local-state/, __pycache__/, *.pyc, and the repo's
own overlays/ directory (its contents are layered per-target, never installed as-is).

stdlib only; Windows-safe (utf-8 reconfigure; CRLF/LF-normalized comparisons).
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_ENV = 2
EXIT_DRIFT = 3

EXCLUDE_DIRS = {".local-state", "__pycache__", ".git", "node_modules"}
EXCLUDE_FILE_SUFFIXES = (".pyc",)
OVERLAY_DIR = "overlays"

VALID_TARGETS = ("claude", "codex")
SKILL_HOME_TOKEN = "{{SKILL_HOME}}"

# Cruft/secret patterns flagged (warn-only) when capturing NEW or changed files.
_SECRET_RE = re.compile(
    r"(sk-ant-[a-zA-Z0-9-]{8,}|ghp_[A-Za-z0-9]{20,}|github_pat_[0-9A-Za-z_]{22,}|"
    r"glpat-[0-9A-Za-z_-]{20,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|xox[baprs]-[A-Za-z0-9-]{10,})"
)
# Project-coupled absolute paths that shouldn't ride into a generic skill
# (~/.claude and ~/.codex paths are fine; a hardcoded OTHER project under Projects/
# is a smell). Matches forward-slash, backslash, AND WSL (/mnt/c/) forms.
_PROJECT_PATH_RE = re.compile(
    r"(?:[Cc]:[\\/]|/mnt/c/)Users[\\/][^\\/]+[\\/]Projects[\\/](?!\*)[A-Za-z0-9._-]+"
)
_CRUFT_NAME_RE = re.compile(r"(^|/)seed_.*\.py$|(^|/)_tmp|(^|/)scratch")


def eprint(*a: object) -> None:
    print(*a, file=sys.stderr)


# --------------------------------------------------------------------------- #
# Environment / roots
# --------------------------------------------------------------------------- #
def repo_root() -> Path:
    # scripts/sync.py -> repo root is the parent of scripts/
    env = os.environ.get("CLAUDE_GLOBAL_SKILLS_REPO")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parent.parent


def target_home(target: str) -> Path:
    """Live skills directory for a target. Env-overridable for hermetic tests."""
    if target == "claude":
        env = os.environ.get("CLAUDE_SKILLS_DIR")
        return Path(env).resolve() if env else (Path.home() / ".claude" / "skills").resolve()
    if target == "codex":
        env = os.environ.get("CODEX_SKILLS_DIR")
        return Path(env).resolve() if env else (Path.home() / ".codex" / "skills").resolve()
    raise ValueError(f"unknown target: {target!r}")


def skill_home_expansion(target: str, name: str) -> str:
    """The literal string {{SKILL_HOME}} expands to for a target install. $HOME stays
    literal so the emitted command runs in both bash and PowerShell."""
    sub = ".codex" if target == "codex" else ".claude"
    return f"$HOME/{sub}/skills/{name}"


# --------------------------------------------------------------------------- #
# Byte helpers (line-ending normalized so a CRLF working tree compares equal to
# an LF live copy — a content difference that is only line endings is NOT drift).
# --------------------------------------------------------------------------- #
def _norm_bytes(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _read_norm(path: Path) -> bytes:
    return _norm_bytes(path.read_bytes())


def _as_text(data: bytes) -> str | None:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def substitute(data: bytes, target: str, name: str) -> bytes:
    """Forward: expand {{SKILL_HOME}} for a target. Text files only; binaries pass."""
    text = _as_text(data)
    if text is None or SKILL_HOME_TOKEN not in text:
        return data
    return text.replace(SKILL_HOME_TOKEN, skill_home_expansion(target, name)).encode("utf-8")


def unsubstitute(data: bytes, target: str, name: str) -> bytes:
    """Reverse (for capture): fold a target's expanded install path back to the token.
    Anchored to the exact expansion string; safe because shared files may not contain
    a literal expansion (see check_no_literal_expansion / the capture guard)."""
    text = _as_text(data)
    if text is None:
        return data
    exp = skill_home_expansion(target, name)
    if exp not in text:
        return data
    return text.replace(exp, SKILL_HOME_TOKEN).encode("utf-8")


# --------------------------------------------------------------------------- #
# Frontmatter targets
# --------------------------------------------------------------------------- #
def _parse_targets_value(rest: str) -> list[str]:
    rest = rest.strip()
    if rest.startswith("["):
        close = rest.find("]")
        if close == -1:
            raise ValueError(f"malformed `targets:` flow list (no closing ]): {rest!r}")
        inner = rest[1:close]
        return [p.strip().strip('"').strip("'") for p in inner.split(",") if p.strip()]
    # scalar (single target) — strip an inline comment
    hpos = rest.find("#")
    if hpos != -1:
        rest = rest[:hpos].strip()
    rest = rest.strip().strip('"').strip("'")
    return [rest] if rest else []


def read_frontmatter_targets(skill_md: Path) -> list[str]:
    """Parse the LEADING fenced YAML frontmatter block for `targets:`.

    Absent key -> ['claude'] (silent, backward-compatible default).
    Present-but-malformed or unknown value -> ValueError (fail loud; never a silent
    degrade to Claude-only, which would hide a dropped Codex target).
    """
    try:
        # utf-8-sig so a leading BOM (PowerShell's `Out-File -Encoding utf8`
        # emits one) can't defeat the `---` fence check below and silently
        # degrade a `targets: [claude, codex]` skill back to Claude-only.
        text = _read_norm(skill_md).decode("utf-8-sig", errors="replace")
    except OSError:
        return ["claude"]
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return ["claude"]
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return ["claude"]
    fm = lines[1:end]

    targets: list[str] | None = None
    i = 0
    while i < len(fm):
        m = re.match(r"^targets\s*:\s*(.*)$", fm[i])
        if not m:
            i += 1
            continue
        rest = m.group(1).strip()
        if rest:
            targets = _parse_targets_value(rest)
        else:
            block: list[str] = []
            j = i + 1
            while j < len(fm):
                line = fm[j]
                if line.strip() == "":
                    j += 1
                    continue
                bm = re.match(r"^\s*-\s*(.+?)\s*$", line)
                if not bm:
                    break
                val = bm.group(1).strip()
                hpos = val.find("#")
                if hpos != -1:
                    val = val[:hpos].strip()
                block.append(val.strip().strip('"').strip("'"))
                j += 1
            targets = block
        break

    if targets is None:
        return ["claude"]
    norm = [t.strip().lower() for t in targets if t.strip()]
    if not norm:
        raise ValueError(f"{skill_md}: `targets:` present but empty (use e.g. [claude, codex])")
    bad = [t for t in norm if t not in VALID_TARGETS]
    if bad:
        raise ValueError(
            f"{skill_md}: unknown target(s) {bad}; allowed: {list(VALID_TARGETS)}"
        )
    seen: list[str] = []
    for t in norm:
        if t not in seen:
            seen.append(t)
    return seen


# --------------------------------------------------------------------------- #
# File enumeration
# --------------------------------------------------------------------------- #
def _walk_rel(root: Path, skip_overlay_root: bool = False) -> set[str]:
    files: set[str] = set()
    if not root.exists():
        return files
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        if skip_overlay_root and rel_dir == Path("."):
            dirnames[:] = [d for d in dirnames if d != OVERLAY_DIR]
        for fn in filenames:
            if fn.endswith(EXCLUDE_FILE_SUFFIXES):
                continue
            files.add((Path(dirpath) / fn).relative_to(root).as_posix())
    return files


def shared_rel_files(skill_dir: Path) -> set[str]:
    """Files that are shared across targets (excludes the repo's overlays/ dir)."""
    return _walk_rel(skill_dir, skip_overlay_root=True)


def overlay_rel_files(skill_dir: Path, target: str) -> set[str]:
    """Files under <skill>/overlays/<target>/, relative to that overlay root."""
    return _walk_rel(skill_dir / OVERLAY_DIR / target)


def live_rel_files(live_dir: Path) -> set[str]:
    return _walk_rel(live_dir)


def list_skill_dirs(root: Path) -> dict[str, Path]:
    """Top-level dirs under root that contain a SKILL.md."""
    out: dict[str, Path] = {}
    if not root.exists():
        return out
    for child in sorted(root.iterdir()):
        if not child.is_dir() or child.name in EXCLUDE_DIRS:
            continue
        if (child / "SKILL.md").is_file():
            out[child.name] = child
    return out


# --------------------------------------------------------------------------- #
# Materialization (forward)
# --------------------------------------------------------------------------- #
def materialize(skill_dir: Path, target: str, name: str) -> dict[str, bytes]:
    """The exact file set a target should receive = shared + that target's overlay,
    with {{SKILL_HOME}} expanded. Overlays are additive-only: an overlay path that
    shadows a shared path is a hard error (keeps the divergence guard well-defined)."""
    out: dict[str, bytes] = {}
    for rel in shared_rel_files(skill_dir):
        out[rel] = substitute((skill_dir / rel).read_bytes(), target, name)
    ov_base = skill_dir / OVERLAY_DIR / target
    for rel in overlay_rel_files(skill_dir, target):
        if rel in out:
            raise ValueError(
                f"{name}: overlay '{OVERLAY_DIR}/{target}/{rel}' shadows shared file "
                f"'{rel}' — overlays are additive-only"
            )
        out[rel] = substitute((ov_base / rel).read_bytes(), target, name)
    return out


def check_no_literal_expansion(skill_dir: Path, name: str) -> list[str]:
    """Warn if a shared file contains a literal {{SKILL_HOME}} expansion instead of
    the token — the token must be the only per-target representation so capture's
    reverse pass is unambiguous."""
    warnings: list[str] = []
    exps = [(t, skill_home_expansion(t, name)) for t in VALID_TARGETS]
    for rel in sorted(shared_rel_files(skill_dir)):
        text = _as_text((skill_dir / rel).read_bytes())
        if text is None:
            continue
        for _t, exp in exps:
            if exp in text:
                warnings.append(
                    f"{name}/{rel} contains literal '{exp}' — use {SKILL_HOME_TOKEN} instead"
                )
    return warnings


# --------------------------------------------------------------------------- #
# Target resolution
# --------------------------------------------------------------------------- #
def resolve_targets(arg_target):
    """None -> per-skill default (declared ∩ homes-that-exist). 'both' -> all."""
    if arg_target in (None, "default"):
        return None
    if arg_target == "both":
        return list(VALID_TARGETS)
    return [arg_target]


def effective_targets(declared: list[str], requested, *, bootstrap: bool = False) -> list[str]:
    """Resolve which of a skill's declared targets to act on.

    requested is None (the default set) -> declared targets whose live home already
    exists, so check and deploy stay symmetric (deploy -> check round-trips) and we
    never nag about a product that isn't installed. EXCEPTION: with bootstrap=True
    (default `--deploy`, no --target) the baseline `claude` target is always included
    if declared, even when ~/.claude/skills does not exist yet — otherwise a bare
    `python scripts/sync.py --deploy` on a fresh machine would select nothing and exit
    0 having installed nothing, breaking the documented first-install path. `claude` is
    the repo's baseline product; a missing claude home is created on deploy. `codex`
    stays opt-in (only if its home already exists) since we can't assume it's installed.
    """
    if requested is None:
        return [t for t in declared
                if target_home(t).exists() or (bootstrap and t == "claude")]
    return [t for t in requested if t in declared]


# --------------------------------------------------------------------------- #
# Drift (check)
# --------------------------------------------------------------------------- #
def diff_target(name: str, skill_dir: Path, target: str):
    """(added, removed, changed) comparing materialized(expected) -> live[target].

    added   = expected but missing in live (deploy would add)
    removed = in live but not expected (deploy would remove)
    changed = present in both, content differs (LF-normalized)
    """
    expected = materialize(skill_dir, target, name)
    live_dir = target_home(target) / name
    live_files = live_rel_files(live_dir)
    exp_files = set(expected)
    added = sorted(exp_files - live_files)
    removed = sorted(live_files - exp_files)
    changed = []
    for rel in sorted(exp_files & live_files):
        if _norm_bytes(expected[rel]) != _read_norm(live_dir / rel):
            changed.append(rel)
    return added, removed, changed


def _unmanaged_live_skills(requested, repo: dict[str, Path]) -> list[tuple[str, str]]:
    """Skills present in a consulted live home but not in the repo (informational)."""
    out = []
    targets = list(VALID_TARGETS) if requested is None else requested
    for t in targets:
        home = target_home(t)
        if not home.exists():
            continue
        for lname in list_skill_dirs(home):
            if lname not in repo:
                out.append((lname, t))
    return out


def print_drift(drift, unmanaged=None, warnings=None, notes=None) -> None:
    for w in warnings or []:
        print(f"! warning: {w}")
    for n in notes or []:
        print(f"note: {n}")
    if not drift:
        print("in sync: live target(s) match the repo-materialized output (no drift).")
    else:
        print(f"DRIFT: {len(drift)} skill/target pair(s) differ:\n")
        for (name, target), d in drift.items():
            print(f"  {name} [{target}]")
            for rel in d["added"]:
                print(f"      + {rel}  (expected, missing in live)")
            for rel in d["changed"]:
                print(f"      ~ {rel}  (content differs)")
            for rel in d["removed"]:
                print(f"      - {rel}  (in live, not expected — deploy would remove)")
        print()
    if unmanaged:
        labels = sorted({f"{n} [{t}]" for n, t in unmanaged})
        shown = ", ".join(labels[:12]) + (" …" if len(labels) > 12 else "")
        print(f"note: {len(labels)} unmanaged live-only skill(s) not tracked by the "
              f"repo (left untouched): {shown}")


def do_check(arg_target, only=None) -> int:
    requested = resolve_targets(arg_target)
    repo = list_skill_dirs(repo_root())
    names = sorted(repo)
    if only:
        names = [n for n in names if n == only]

    pairs: list[tuple[str, str]] = []
    homes_consulted: set[str] = set()
    warnings: list[str] = []
    notes: list[str] = []
    for name in names:
        try:
            declared = read_frontmatter_targets(repo[name] / "SKILL.md")
        except ValueError as e:
            eprint(f"error: {e}")
            return EXIT_ERROR
        eff = effective_targets(declared, requested)
        for t in eff:
            homes_consulted.add(t)
            pairs.append((name, t))
        if requested is None:
            for t in declared:
                if t not in eff:
                    notes.append(f"{name}: declares '{t}' but its home is absent — "
                                 f"run --deploy --target {t} to install")

    existing = [t for t in homes_consulted if target_home(t).exists()]
    if homes_consulted and not existing:
        eprint(f"error: no live skills home found for target(s): {sorted(homes_consulted)}")
        return EXIT_ENV

    drift = {}
    for name, t in pairs:
        if not target_home(t).exists():
            continue
        warnings += check_no_literal_expansion(repo[name], name)
        try:
            a, r, c = diff_target(name, repo[name], t)
        except ValueError as e:
            eprint(f"error: {e}")
            return EXIT_ERROR
        if a or r or c:
            drift[(name, t)] = {"added": a, "removed": r, "changed": c}

    unmanaged = _unmanaged_live_skills(requested, repo) if only is None else []
    print_drift(drift, unmanaged, sorted(set(warnings)), notes)
    return EXIT_DRIFT if drift else EXIT_OK


# --------------------------------------------------------------------------- #
# Deploy
# --------------------------------------------------------------------------- #
def _write_materialized(dst: Path, files: dict[str, bytes]) -> None:
    """Replace non-.local-state content of dst, then write the materialized files."""
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
    for rel, data in files.items():
        path = dst / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def do_deploy(arg_target, only=None) -> int:
    requested = resolve_targets(arg_target)
    repo = list_skill_dirs(repo_root())
    if not repo:
        eprint(f"error: no skills found in repo {repo_root()}")
        return EXIT_ENV
    names = sorted(repo)
    if only:
        names = [n for n in names if n == only]

    deployed: dict[str, list[str]] = {}
    skipped_notes: list[str] = []
    warnings: list[str] = []
    incomplete: list[str] = []
    for name in names:
        try:
            declared = read_frontmatter_targets(repo[name] / "SKILL.md")
        except ValueError as e:
            eprint(f"error: {e}")
            return EXIT_ERROR
        eff = effective_targets(declared, requested, bootstrap=True)
        warnings += check_no_literal_expansion(repo[name], name)
        # Materialize all targets first so an overlay-shadow error can't half-deploy.
        try:
            mats = {t: materialize(repo[name], t, name) for t in eff}
        except ValueError as e:
            eprint(f"error: {e}")
            return EXIT_ERROR
        for t, files in mats.items():
            home = target_home(t)
            home.mkdir(parents=True, exist_ok=True)
            _write_materialized(home / name, files)
            deployed.setdefault(t, []).append(name)
            # Verify the write actually applied — _write_materialized tolerates a
            # locked/undeletable child (Windows), so a deploy that couldn't fully
            # apply must be surfaced loudly, not reported as a silent success.
            a, r, c = diff_target(name, repo[name], t)
            if a or r or c:
                incomplete.append(f"{name} [{t}]: deploy did not fully apply "
                                  f"({len(a) + len(r) + len(c)} residual file(s); a live "
                                  f"file may be locked or undeletable) — re-run or check")
        if requested is None:
            for t in declared:
                if t not in eff:
                    skipped_notes.append(
                        f"{name}: declares '{t}' but its home is absent — "
                        f"run --deploy --target {t} to install there")

    for w in sorted(set(warnings)):
        print(f"! warning: {w}")
    for t in VALID_TARGETS:
        if deployed.get(t):
            print(f"deployed {len(deployed[t])} skill(s) -> {target_home(t)}: "
                  f"{', '.join(sorted(deployed[t]))}")
    for n in skipped_notes:
        print(f"note: {n}")
    for w in incomplete:
        eprint(f"! deploy-incomplete: {w}")
    if not deployed:
        print("nothing deployed (no skill declares an available target).")
    # A deploy that could not fully apply is not a success — report it as drift so
    # tooling (and the operator) notice rather than trusting a stale live copy.
    return EXIT_DRIFT if incomplete else EXIT_OK


# --------------------------------------------------------------------------- #
# Capture (reverse)
# --------------------------------------------------------------------------- #
def _repo_shared_image(skill_dir: Path) -> dict[str, bytes]:
    return {rel: _read_norm(skill_dir / rel) for rel in shared_rel_files(skill_dir)}


def _reverse_shared_image(skill_dir: Path, target: str, live_dir: Path) -> dict[str, bytes]:
    """Reverse-materialize a live copy's SHARED region into token form (normalized).
    Overlay files and files with no repo-shared counterpart are excluded."""
    shared_manifest = shared_rel_files(skill_dir)
    overlay_manifest = overlay_rel_files(skill_dir, target)
    name = skill_dir.name
    img: dict[str, bytes] = {}
    for rel in live_rel_files(live_dir):
        if rel in overlay_manifest or rel not in shared_manifest:
            continue
        data = (live_dir / rel).read_bytes()
        img[rel] = _norm_bytes(unsubstitute(data, target, name))
    return img


# Capture pulls the LIVE copy back into a PUBLIC repo, so it is the one direction
# that can leak. The live tree accumulates operator-private detail (real client
# codenames, absolute home paths, session ids) that the published copy deliberately
# generalizes; without this gate a single `--capture` silently republishes all of it.
PRIVATE_PATTERNS = [
    (re.compile(r"[a-z]:[\\/]users[\\/][a-z0-9._-]+", re.I), "absolute home path"),
    (re.compile(r"/home/[a-z0-9._-]+/|/mnt/c/users/[a-z0-9._-]+", re.I), "absolute home path"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}"), "email address"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "session uuid"),
    (re.compile(r"sk-ant-|ghp_|github_pat_|AKIA[0-9A-Z]{16}|BEGIN [A-Z ]*PRIVATE KEY"), "secret-shaped"),
]


PRIVATE_TERMS_FILE = ".capture-private-terms"


def load_private_terms(root: Path):
    """Operator-supplied literal terms that must never enter the repo.

    One term per line in a gitignored `.capture-private-terms` at the repo root;
    `#` comments and blanks ignored; matched case-insensitively as substrings.
    This is where client codenames and project aliases belong -- listing them in
    the tracked source would publish the very names the gate exists to withhold,
    and one operator's private terms are not another's.
    """
    path = root / PRIVATE_TERMS_FILE
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    terms = []
    for line in raw.replace("\r\n", "\n").split("\n"):
        line = line.strip()
        if line and not line.startswith("#"):
            terms.append(line)
    return terms


def _new_private_lines(dest: Path, new_bytes: bytes, terms=()):
    """Private-looking lines that capture would ADD to `dest`.

    Only NEW lines are scanned. A token already present in the repo copy is
    sanctioned by definition -- `weekly-work-log` legitimately names the client
    codenames it redacts, and re-flagging those would make the gate unusable.
    Returns [(label, matched_text, line), ...].
    """
    try:
        old = dest.read_text(encoding="utf-8", errors="replace") if dest.exists() else ""
    except OSError:
        old = ""
    already = set(old.replace("\r\n", "\n").split("\n"))
    text = new_bytes.decode("utf-8", errors="replace").replace("\r\n", "\n")

    lowered_terms = [(t, t.lower()) for t in terms]

    found = []
    for line in text.split("\n"):
        if line in already:
            continue
        hit = None
        for rx, label in PRIVATE_PATTERNS:
            m = rx.search(line)
            if m:
                hit = (label, m.group(0))
                break
        if hit is None:
            low = line.lower()
            for original, needle in lowered_terms:
                if needle in low:
                    hit = ("private term", original)
                    break
        if hit is not None:
            found.append((hit[0], hit[1], line.strip()[:100]))
    return found


def _capture_existing(name, skill_dir, target, live_dir):
    """Returns (ok, message, changed_files). Enforces the divergence guard, then
    classifies each live file to shared/ or overlays/<target>/ (refuse if neither)."""
    declared = read_frontmatter_targets(skill_dir / "SKILL.md")
    repo_shared_now = _repo_shared_image(skill_dir)
    shared_T = _reverse_shared_image(skill_dir, target, live_dir)

    for t2 in declared:
        if t2 == target:
            continue
        h2 = target_home(t2)
        live2 = h2 / name
        if not h2.exists() or not live2.exists():
            continue
        shared_T2 = _reverse_shared_image(skill_dir, t2, live2)
        if shared_T2 == repo_shared_now or shared_T2 == shared_T:
            continue
        conflicts = sorted(
            rel for rel in set(shared_T) | set(shared_T2)
            if shared_T.get(rel) != shared_T2.get(rel)
        )
        return (False,
                f"{name}: DIVERGENCE between live[{target}] and live[{t2}] in shared "
                f"file(s) {conflicts[:6]} — refusing to capture; reconcile manually.",
                [])

    shared_manifest = shared_rel_files(skill_dir)
    overlay_manifest = overlay_rel_files(skill_dir, target)
    live_files = live_rel_files(live_dir)
    writes: list[tuple[Path, bytes]] = []
    changed: list[Path] = []
    unclassifiable: list[str] = []
    for rel in sorted(live_files):
        data = (live_dir / rel).read_bytes()
        if rel in overlay_manifest:
            dest = skill_dir / OVERLAY_DIR / target / rel
        elif rel in shared_manifest:
            dest = skill_dir / rel
        else:
            unclassifiable.append(rel)
            continue
        new_bytes = unsubstitute(data, target, name)
        if dest.exists() and _norm_bytes(dest.read_bytes()) == _norm_bytes(new_bytes):
            continue  # no real change
        writes.append((dest, new_bytes))
        changed.append(dest)

    if unclassifiable:
        return (False,
                f"{name}: unclassifiable live file(s) {unclassifiable[:6]} — not in "
                f"shared content or overlays/{target}/. Place them explicitly in the "
                f"repo (shared vs overlays/<target>/) before capturing.",
                [])

    terms = load_private_terms(repo_root())
    leaks = []
    for dest, data in writes:
        for label, matched, line in _new_private_lines(dest, data, terms):
            leaks.append(f"    {dest.name} [{label}] {matched!r} in: {line}")
    if leaks:
        shown = "\n".join(leaks[:8])
        more = f"\n    ... and {len(leaks) - 8} more" if len(leaks) > 8 else ""
        return (False,
                f"{name}: capture would ADD private content to this repo:\n{shown}{more}\n"
                f"  The live copy keeps operator-private detail the published copy "
                f"generalizes. Generalize these lines in the LIVE file first, then "
                f"re-run --capture. Nothing was written.",
                [])

    # Deletions: a repo file that belongs to THIS target's materialized set (shared
    # content or this target's overlay) but is absent from the live copy is a live
    # deletion. Without representing it, capture would leave the repo file in place and
    # report the skill "already in sync" with exit 0, while --check keeps flagging the
    # same missing-live drift forever. Removing an OVERLAY file only affects this target
    # and is always safe. Removing a SHARED file affects every target; if any OTHER
    # declared target still has that file live, deleting it from the repo would corrupt
    # that target's next deploy — so refuse rather than silently break the sibling.
    overlay_deletes: list[Path] = []
    shared_deletes: list[Path] = []
    unsafe_shared: list[tuple[str, str]] = []  # (rel, other_target_still_holding_it)
    for rel in sorted(overlay_manifest - live_files):
        overlay_deletes.append(skill_dir / OVERLAY_DIR / target / rel)
    for rel in sorted(shared_manifest - live_files):
        holders = []
        for t2 in declared:
            if t2 == target:
                continue
            h2 = target_home(t2)
            live2 = h2 / name
            if h2.exists() and live2.exists() and rel in live_rel_files(live2):
                holders.append(t2)
        if holders:
            unsafe_shared.append((rel, holders[0]))
        else:
            shared_deletes.append(skill_dir / rel)

    if unsafe_shared:
        rels = [r for r, _ in unsafe_shared][:6]
        holder = unsafe_shared[0][1]
        return (False,
                f"{name}: shared file(s) {rels} were deleted in live[{target}] but "
                f"live[{holder}] still has them — removing them from the repo would "
                f"corrupt live[{holder}]. Delete them there too, or reconcile manually.",
                [])

    deletes = overlay_deletes + shared_deletes
    if not writes and not deletes:
        return (True, "", [])

    for dest, data in writes:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    for dest in deletes:
        try:
            dest.unlink()
        except OSError:
            pass
        _prune_empty_dirs(dest.parent, skill_dir)
        changed.append(dest)
    return (True, "", changed)


def _prune_empty_dirs(start: Path, stop: Path) -> None:
    """Remove now-empty directories from `start` upward, never removing `stop` itself
    or anything above it (so a captured deletion doesn't leave empty scaffolding)."""
    try:
        stop = stop.resolve()
        cur = start.resolve()
    except OSError:
        return
    while cur != stop and stop in cur.parents:
        try:
            next(cur.iterdir())
            return  # not empty
        except StopIteration:
            pass
        except OSError:
            return
        parent = cur.parent
        try:
            cur.rmdir()
        except OSError:
            return
        cur = parent


def do_capture(arg_target, only=None) -> int:
    if arg_target in (None, "default", "both"):
        eprint("error: --capture requires an explicit single --target (claude|codex).")
        return EXIT_ERROR
    target = arg_target
    home = target_home(target)
    if not home.exists():
        eprint(f"error: live home for target '{target}' not found: {home}")
        return EXIT_ENV

    repo = list_skill_dirs(repo_root())
    live = list_skill_dirs(home)
    names = sorted(set(repo) | set(live))
    if only:
        names = [n for n in names if n == only]

    captured: list[str] = []
    refused: list[str] = []
    notes: list[str] = []
    changed_files: list[Path] = []
    for name in names:
        if name not in live:
            if name in repo:
                declared = read_frontmatter_targets(repo[name] / "SKILL.md")
                if target in declared:
                    notes.append(f"{name}: declares '{target}' but has no live copy "
                                 f"there — not deleting; deploy it or resolve manually.")
            continue
        if name not in repo:
            notes.append(f"{name}: live-only under {target} — the repo is canonical; "
                         f"add it to the repo (shared + overlays/<target>/) manually.")
            continue
        try:
            declared = read_frontmatter_targets(repo[name] / "SKILL.md")
        except ValueError as e:
            eprint(f"error: {e}")
            return EXIT_ERROR
        if target not in declared:
            notes.append(f"{name}: live[{target}] exists but the repo skill doesn't "
                         f"declare '{target}' — add it to targets first.")
            continue
        lit = check_no_literal_expansion(repo[name], name)
        if lit:
            refused.append(f"{name}: shared file has a literal SKILL_HOME expansion "
                           f"({lit[0]}) — fix to use the token before capture.")
            continue
        ok, msg, files = _capture_existing(name, repo[name], target, live[name])
        if ok:
            if files:
                captured.append(name)
                changed_files += files
            else:
                notes.append(f"{name}: already in sync (nothing to capture).")
        else:
            refused.append(msg)

    for r in refused:
        print(f"REFUSED: {r}")
    if captured:
        print(f"\ncaptured {len(captured)} skill(s) into the repo (target {target}): "
              f"{', '.join(sorted(captured))}")
    else:
        print(f"\nnothing captured (target {target}).")
    for n in notes:
        print(f"note: {n}")

    # Capture represents live deletions as repo-file removals (intended). Surface
    # them explicitly: a removal is recoverable via git for a tracked file, but a
    # brand-new UNTRACKED repo file (added, not yet committed or deployed) would be
    # lost silently. Let the operator confirm each removal is a real live deletion.
    deleted = [p for p in changed_files if not Path(p).exists()]
    if deleted:
        print("\n! capture REMOVED repo file(s) — confirm each is an intended live "
              "deletion, not un-deployed work (untracked files are unrecoverable):")
        root = repo_root()
        for d in deleted:
            try:
                rel = Path(d).resolve().relative_to(root.resolve())
            except ValueError:
                rel = d
            print(f"   - {rel}")

    warnings = cruft_scan(changed_files)
    if warnings:
        print("\n! pre-scan warnings (review before committing):")
        for w in warnings:
            print(f"   - {w}")
    if captured:
        print("\nNext steps (capture stays operator-driven so it lands as a reviewed PR):")
        print("   git -C <repo> checkout -b sync/capture-<date>")
        print("   git -C <repo> add -A && git -C <repo> status")
        print("   # review the diff, then commit + open a PR; run /review-loop for the verdict")
    return EXIT_ERROR if refused else EXIT_OK


def cruft_scan(paths) -> list[str]:
    """Warn-only scan of given files for secrets/cruft/project-coupling."""
    warnings = []
    for p in paths:
        name = str(p).replace("\\", "/")
        if _CRUFT_NAME_RE.search(name):
            warnings.append(f"cruft-name: {name} (one-shot/scratch — exclude from the skill?)")
        try:
            text = Path(p).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _SECRET_RE.search(text):
            warnings.append(f"SECRET-LIKE: {name} (matches a token/key pattern)")
        proj_hits = sorted({m.group(0) for m in _PROJECT_PATH_RE.finditer(text)})
        if proj_hits:
            warnings.append(f"project-coupled path(s) in {name}: {', '.join(proj_hits[:4])}")
    return warnings


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
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
    g.add_argument("--capture", action="store_true", help="copy live -> repo for one --target; operator commits/PRs")
    g.add_argument("--deploy", action="store_true", help="materialize repo -> live for declared targets")
    p.add_argument("--target", choices=("claude", "codex", "both"), default=None,
                   help="claude|codex|both. --check/--deploy default: declared targets whose "
                        "home exists. --capture: required, single target.")
    p.add_argument("--skill", default=None, help="limit the operation to one skill name")
    args = p.parse_args(argv)
    if args.check:
        return do_check(args.target, args.skill)
    if args.capture:
        return do_capture(args.target, args.skill)
    if args.deploy:
        return do_deploy(args.target, args.skill)
    return EXIT_ENV


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:  # never crash the caller (a hook) hard
        eprint(f"error: {e}")
        sys.exit(1)
