#!/usr/bin/env python3
"""Register a finding row in <project-root>/reports/_data/retro-findings.jsonl.

Append-only registry; atomic tmp+rename; per-project filelock; rotates 3 backups.

Exit codes:
    0 success
    1 anything else (general error)
    2 corruption (malformed existing JSONL line)
    3 missing dep
    4 schema violation
    5 invalid args
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = SCRIPT_DIR / "_schema.json"
FINDING_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-\d{3}$")

EXIT_OK = 0
EXIT_GENERAL = 1
EXIT_CORRUPTION = 2
EXIT_MISSING_DEP = 3
EXIT_SCHEMA = 4
EXIT_ARGS = 5


def find_project_root_from_cwd() -> Path:
    """Walk up from cwd looking for a .git/ directory."""
    cur = Path.cwd().resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / ".git").exists():
            return candidate
    print(
        "error: --project-root not given and no .git/ ancestor found from cwd",
        file=sys.stderr,
    )
    sys.exit(EXIT_ARGS)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def derive_retro_date(retro_path_rel: str, project_root: Path) -> str:
    """Use file mtime of the retro doc if it exists, else today's UTC date."""
    candidates = []
    p = Path(retro_path_rel)
    if p.is_absolute():
        candidates.append(p)
    else:
        candidates.append(project_root / p)
        candidates.append(Path.cwd() / p)
    for cand in candidates:
        if cand.exists() and cand.is_file():
            mtime = datetime.fromtimestamp(cand.stat().st_mtime, tz=timezone.utc)
            return mtime.strftime("%Y-%m-%d")
    return utc_today()


def next_finding_id(existing_ids, retro_date: str) -> str:
    max_n = 0
    for fid in existing_ids:
        if fid.startswith(retro_date + "-"):
            try:
                n = int(fid.rsplit("-", 1)[-1])
                if n > max_n:
                    max_n = n
            except ValueError:
                continue
    return "{}-{:03d}".format(retro_date, max_n + 1)


def read_existing_streaming(path: Path, project_root: Path | None = None):
    """Stream the file line-by-line. Return (raw_lines, finding_ids).

    On malformed JSON, prints to stderr and sys.exit(EXIT_CORRUPTION).
    """
    raw_lines = []
    ids = []
    if not path.exists():
        return raw_lines, ids
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            stripped = line.rstrip("\n").rstrip("\r")
            if not stripped.strip():
                raw_lines.append(line.rstrip("\n"))
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError as e:
                print(
                    "corruption: line {} is not valid JSON ({})".format(lineno, e.msg),
                    file=sys.stderr,
                )
                recover_script = SCRIPT_DIR / "recover_from_backup.py"
                pr_arg = str(project_root) if project_root is not None else "<project-root>"
                print(
                    "Recover with: python {} --project-root {}".format(recover_script, pr_arg),
                    file=sys.stderr,
                )
                sys.exit(EXIT_CORRUPTION)
            raw_lines.append(stripped)
            fid = obj.get("finding_id")
            if isinstance(fid, str):
                ids.append(fid)
    return raw_lines, ids


def rotate_backups(data_dir: Path, keep: int = 3) -> None:
    bak_files = sorted(
        data_dir.glob("retro-findings.jsonl.bak-*"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in bak_files[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="register_finding.py",
        description=(
            "Append a finding row to "
            "<project-root>/reports/_data/retro-findings.jsonl"
        ),
    )
    p.add_argument(
        "--project-root",
        type=str,
        default=None,
        help="Project root path. Defaults to nearest .git/ ancestor of cwd.",
    )
    p.add_argument("--retro-path", required=True, type=str)
    p.add_argument("--project", required=True, type=str)
    p.add_argument("--category", required=True, type=str)
    p.add_argument("--claim", required=True, type=str)
    p.add_argument("--confidence", required=True, type=float)
    p.add_argument("--evidence-supporting", required=True, type=int)
    p.add_argument("--evidence-contradicting", required=True, type=int)
    p.add_argument("--proposed-action", required=True, type=str)
    p.add_argument(
        "--target-date",
        type=str,
        default=None,
        help="YYYY-MM-DD, or empty string / omit for null.",
    )
    p.add_argument("--evidence-summary", type=str, default=None)
    p.add_argument(
        "--follow-up-status",
        type=str,
        default="pending",
        choices=[
            "pending",
            "in-progress",
            "shipped",
            "abandoned",
            "superseded",
            "cancelled",
        ],
    )
    p.add_argument("--follow-up-notes", type=str, default=None)
    p.add_argument(
        "--supersedes",
        type=str,
        default=None,
        help="Another finding's id; must match YYYY-MM-DD-NNN.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the row to stdout; do NOT append.",
    )
    return p.parse_args(argv)


def main(argv):
    args = parse_args(argv)

    if args.project_root:
        project_root = Path(args.project_root).expanduser().resolve()
    else:
        project_root = find_project_root_from_cwd()

    if not project_root.exists():
        print(
            "error: project-root does not exist: {}".format(project_root),
            file=sys.stderr,
        )
        return EXIT_ARGS

    if args.supersedes is not None and args.supersedes != "":
        if not FINDING_ID_RE.match(args.supersedes):
            print(
                "error: --supersedes must match YYYY-MM-DD-NNN",
                file=sys.stderr,
            )
            return EXIT_ARGS

    try:
        from jsonschema import Draft7Validator
    except ImportError:
        print(
            "missing dependency: jsonschema. Install with:\n"
            "  pip install --user jsonschema",
            file=sys.stderr,
        )
        return EXIT_MISSING_DEP
    try:
        from filelock import FileLock, Timeout
    except ImportError:
        print(
            "missing dependency: filelock. Install with:\n"
            "  pip install --user filelock",
            file=sys.stderr,
        )
        return EXIT_MISSING_DEP

    if not SCHEMA_PATH.exists():
        print("error: schema not found at {}".format(SCHEMA_PATH), file=sys.stderr)
        return EXIT_GENERAL
    try:
        with SCHEMA_PATH.open("r", encoding="utf-8") as fh:
            schema = json.load(fh)
    except json.JSONDecodeError as e:
        print("error: schema is invalid JSON: {}".format(e), file=sys.stderr)
        return EXIT_GENERAL
    validator = Draft7Validator(schema)

    data_dir = project_root / "reports" / "_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    registry = data_dir / "retro-findings.jsonl"
    lock_path = data_dir / ".retro-findings.lock"

    retro_date = derive_retro_date(args.retro_path, project_root)
    if args.target_date is None or args.target_date == "":
        target_date_value = None
    else:
        try:
            datetime.strptime(args.target_date, "%Y-%m-%d")
        except ValueError:
            print(
                "error: --target-date must be YYYY-MM-DD, got: {!r}".format(
                    args.target_date
                ),
                file=sys.stderr,
            )
            return EXIT_ARGS
        target_date_value = args.target_date

    lock = FileLock(str(lock_path))
    try:
        with lock.acquire(timeout=30):
            raw_lines, existing_ids = read_existing_streaming(registry, project_root)
            finding_id = next_finding_id(existing_ids, retro_date)

            row = {
                "finding_id": finding_id,
                "retro_date": retro_date,
                "retro_path": args.retro_path,
                "project": args.project,
                "category": args.category,
                "claim": args.claim,
                "confidence": args.confidence,
                "evidence_supporting": args.evidence_supporting,
                "evidence_contradicting": args.evidence_contradicting,
                "proposed_action": args.proposed_action,
                "target_date": target_date_value,
                "follow_up_status": args.follow_up_status,
                "appended_at": utc_now_iso(),
                "appended_by": os.environ.get(
                    "CLAUDE_AGENT_ID", "register_finding.py"
                ),
            }
            if args.evidence_summary is not None:
                row["evidence_summary"] = args.evidence_summary
            if args.follow_up_notes is not None:
                row["follow_up_notes"] = args.follow_up_notes
            if args.supersedes is not None and args.supersedes != "":
                row["supersedes"] = args.supersedes

            errors = sorted(validator.iter_errors(row), key=lambda e: list(e.absolute_path))
            if errors:
                print("schema violation:", file=sys.stderr)
                for err in errors:
                    loc = "/".join(str(part) for part in err.absolute_path) or "<root>"
                    print("  - {}: {}".format(loc, err.message), file=sys.stderr)
                return EXIT_SCHEMA

            row_line = json.dumps(row, ensure_ascii=False, sort_keys=True)

            if args.dry_run:
                print(row_line)
                return EXIT_OK

            if registry.exists():
                ts = int(time.time())
                bak = data_dir / "retro-findings.jsonl.bak-{}".format(ts)
                bump = 0
                while bak.exists():
                    bump += 1
                    bak = data_dir / "retro-findings.jsonl.bak-{}-{}".format(ts, bump)
                try:
                    with registry.open("rb") as src, bak.open("wb") as dst:
                        dst.write(src.read())
                except OSError as e:
                    print("warning: backup failed: {}".format(e), file=sys.stderr)
                rotate_backups(data_dir, keep=3)

            tmp_path = data_dir / "retro-findings.jsonl.tmp-{}".format(os.getpid())
            try:
                with tmp_path.open("w", encoding="utf-8", newline="\n") as fh:
                    for line in raw_lines:
                        fh.write(line)
                        fh.write("\n")
                    fh.write(row_line)
                    fh.write("\n")
                    fh.flush()
                    try:
                        os.fsync(fh.fileno())
                    except OSError:
                        pass
                os.replace(tmp_path, registry)
            finally:
                if tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass

            print(finding_id)
            return EXIT_OK
    except Timeout:
        print(
            "error: could not acquire lock at {} within 30s".format(lock_path),
            file=sys.stderr,
        )
        return EXIT_GENERAL


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception as e:
        print("error: {}".format(e), file=sys.stderr)
        sys.exit(EXIT_GENERAL)
