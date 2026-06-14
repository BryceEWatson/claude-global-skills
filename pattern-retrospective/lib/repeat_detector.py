#!/usr/bin/env python3
"""repeat_detector.py - fuzzy-match a candidate retrospective claim against history.

Reads project-scoped finding registries (JSONL) and compares each row's `claim`
to the new candidate using difflib.SequenceMatcher (lexical only).

Tiers:
    near-duplicate   ratio >= 0.85   exit 2 (BLOCKED)
    candidate        0.70 <= r < 0.85 exit 1 (REVIEW)
    novel            ratio < 0.70    exit 0 (NOVEL)

Errors exit 3.

Honest caveat: SequenceMatcher is lexical-only - semantic repeats with different
wording will be missed. Documented in human output footer.

Per plan: ~/.claude/plans/1-what-is-the-goofy-pearl.md Step 5.
"""

from __future__ import annotations

import argparse
import glob as _glob
import json
import sys
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable

NEAR_DUPLICATE_THRESHOLD_DEFAULT = 0.85
CANDIDATE_THRESHOLD = 0.70
CONVENTION_GLOB = "C:/Users/Bryce/Projects/*/reports/_data/retro-findings.jsonl"
REGISTRY_RELPATH = ("reports", "_data", "retro-findings.jsonl")
CLAIM_TRUNCATE = 120


def _eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def find_git_root(start: Path) -> Path | None:
    """Walk up from `start` until a directory containing `.git` is found."""
    p = start.resolve()
    while True:
        if (p / ".git").exists():
            return p
        if p.parent == p:
            return None
        p = p.parent


def resolve_registries(
    scope: str,
    project_root: Path | None,
    registries_override: list[Path] | None,
) -> list[Path]:
    """Decide which registry files to read."""
    if registries_override:
        return registries_override
    if scope == "this-project":
        if project_root is None:
            raise ValueError(
                "--project-root required when --scope this-project (or run inside a git repo)"
            )
        return [project_root.joinpath(*REGISTRY_RELPATH)]
    if scope == "all":
        return [Path(p) for p in _glob.glob(CONVENTION_GLOB)]
    raise ValueError(f"Unknown scope: {scope}")


def iter_rows(registry: Path) -> Iterable[dict]:
    """Stream a JSONL registry, yielding parsed rows. Warns and skips malformed lines."""
    try:
        f = registry.open("r", encoding="utf-8", errors="replace")
    except FileNotFoundError:
        _eprint(f"warning: registry not found: {registry}")
        return
    with f:
        for lineno, line in enumerate(f, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                yield json.loads(stripped)
            except json.JSONDecodeError as exc:
                _eprint(f"warning: malformed JSON at {registry}:{lineno}: {exc}")
                continue


def score_claim(new_claim: str, existing_claim: str) -> float:
    a = unicodedata.normalize("NFC", new_claim).casefold()
    b = unicodedata.normalize("NFC", existing_claim).casefold()
    return SequenceMatcher(None, a, b).ratio()


def classify(ratio: float, near_threshold: float) -> str:
    if ratio >= near_threshold:
        return "near-duplicate"
    if ratio >= CANDIDATE_THRESHOLD:
        return "candidate"
    return "novel"


def collect_matches(
    new_claim: str,
    registries: list[Path],
    near_threshold: float,
) -> list[dict]:
    """Return all rows with ratio >= CANDIDATE_THRESHOLD, sorted desc by ratio."""
    matches: list[dict] = []
    for reg in registries:
        for row in iter_rows(reg):
            existing = row.get("claim")
            if not isinstance(existing, str):
                continue
            ratio = score_claim(new_claim, existing)
            if ratio < CANDIDATE_THRESHOLD:
                continue
            matches.append(
                {
                    "ratio": ratio,
                    "tier": classify(ratio, near_threshold),
                    "registry": str(reg),
                    "finding_id": row.get("finding_id"),
                    "project": row.get("project"),
                    "retro_date": row.get("retro_date"),
                    "claim": existing,
                }
            )
    matches.sort(key=lambda m: m["ratio"], reverse=True)
    return matches


def truncate(text: str, length: int = CLAIM_TRUNCATE) -> str:
    if len(text) <= length:
        return text
    return text[: length - 1] + "..."


def format_human(matches: list[dict], near_threshold: float, limit: int) -> tuple[str, int]:
    """Return (text, exit_code)."""
    near = [m for m in matches if m["tier"] == "near-duplicate"]
    cand = [m for m in matches if m["tier"] == "candidate"]
    lines: list[str] = []

    if near:
        plural = "es" if len(near) != 1 else ""
        lines.append(
            f"BLOCKED: near-duplicate found ({len(near)} match{plural} at ratio >= {near_threshold:.2f})"
        )
        exit_code = 2
        shown = matches[:limit]
    elif cand:
        plural = "s" if len(cand) != 1 else ""
        lines.append(
            f"REVIEW: {len(cand)} possible repeat{plural} at "
            f"{CANDIDATE_THRESHOLD:.2f} <= ratio < {near_threshold:.2f}"
        )
        exit_code = 1
        shown = matches[:limit]
    else:
        lines.append(f"NOVEL: no matches above {CANDIDATE_THRESHOLD:.2f}")
        return "\n".join(lines), 0

    for m in shown:
        lines.append(
            f"  [{m['tier']:14s}] ratio={m['ratio']:.3f}  {m['finding_id']}  "
            f"project={m['project']}  retro_date={m['retro_date']}"
        )
        lines.append(f"      claim: {truncate(m['claim'])}")

    lines.append("")
    lines.append(
        "Note: difflib.SequenceMatcher is lexical-only. "
        "Semantic repeats with different wording may be missed."
    )
    return "\n".join(lines), exit_code


def format_json(matches: list[dict], near_threshold: float, limit: int) -> tuple[str, int]:
    near = [m for m in matches if m["tier"] == "near-duplicate"]
    cand = [m for m in matches if m["tier"] == "candidate"]
    if near:
        verdict, exit_code = "near-duplicate", 2
    elif cand:
        verdict, exit_code = "candidate", 1
    else:
        verdict, exit_code = "novel", 0
    payload = {
        "verdict": verdict,
        "near_threshold": near_threshold,
        "candidate_threshold": CANDIDATE_THRESHOLD,
        "n_near_duplicate": len(near),
        "n_candidate": len(cand),
        "matches": matches[:limit],
        "caveat": "difflib.SequenceMatcher is lexical-only; semantic repeats may be missed.",
    }
    return json.dumps(payload, indent=2), exit_code


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="repeat_detector.py",
        description=(
            "Fuzzy-match a candidate retrospective claim against project "
            "(or all) registries."
        ),
    )
    parser.add_argument("--new-claim", required=True, help="The candidate claim text to check.")
    parser.add_argument(
        "--scope",
        choices=["this-project", "all"],
        default="this-project",
        help="Which registries to search (default: this-project).",
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help="Project root (required for --scope this-project unless cwd resolves to one).",
    )
    parser.add_argument(
        "--registries",
        type=str,
        default=None,
        help="Comma-separated explicit registry paths (overrides scope/glob).",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=NEAR_DUPLICATE_THRESHOLD_DEFAULT,
        help=(
            f"Near-duplicate ratio cutoff (default {NEAR_DUPLICATE_THRESHOLD_DEFAULT}). "
            f"Candidate cutoff is fixed at {CANDIDATE_THRESHOLD}."
        ),
    )
    parser.add_argument("--limit", type=int, default=10, help="Max matches to display (default 10).")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of human text.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.threshold < CANDIDATE_THRESHOLD:
        _eprint(
            f"warning: --threshold {args.threshold} is below the candidate cutoff "
            f"{CANDIDATE_THRESHOLD}; near-duplicate tier widened to include all candidates."
        )

    registries_override: list[Path] | None = None
    if args.registries:
        registries_override = [Path(p.strip()) for p in args.registries.split(",") if p.strip()]

    project_root = args.project_root
    if args.scope == "this-project" and project_root is None and registries_override is None:
        project_root = find_git_root(Path.cwd())
        if project_root is None:
            _eprint("error: --project-root not provided and cwd has no .git/ ancestor")
            return 3

    try:
        registries = resolve_registries(args.scope, project_root, registries_override)
    except ValueError as exc:
        _eprint(f"error: {exc}")
        return 3

    if not registries:
        _eprint("error: no registries resolved (empty glob or empty --registries)")
        return 3

    matches = collect_matches(args.new_claim, registries, args.threshold)

    if args.json:
        text, exit_code = format_json(matches, args.threshold, args.limit)
    else:
        text, exit_code = format_human(matches, args.threshold, args.limit)
    print(text)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
