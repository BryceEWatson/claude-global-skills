#!/usr/bin/env python3
"""Operator-invoked recovery for a corrupt retro-findings.jsonl registry.

Usage:
    # Diagnose only (default): scan for corruption, list backups.
    python recover_from_backup.py --project-root <path>

    # Restore from newest .bak-* file.
    python recover_from_backup.py --project-root <path> --rollback-to-latest-backup

    # Drop the corrupt line (1-indexed) and keep the rest.
    python recover_from_backup.py --project-root <path> --repair <N>

Exit codes:
    0 success
    1 anything else (general error)
    5 invalid args (e.g. both flags at once)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

EXIT_OK = 0
EXIT_GENERAL = 1
EXIT_ARGS = 5


def list_backups(data_dir: Path):
    """Return list of (path, mtime) tuples for .bak-* files, newest first."""
    items = []
    for p in data_dir.glob("retro-findings.jsonl.bak-*"):
        try:
            items.append((p, p.stat().st_mtime))
        except OSError:
            continue
    items.sort(key=lambda t: t[1], reverse=True)
    return items


def scan_for_first_corruption(path: Path):
    """Stream-scan path; return (lineno, error_msg) of first bad line or None."""
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            stripped = line.rstrip("\n").rstrip("\r")
            if not stripped.strip():
                continue
            try:
                json.loads(stripped)
            except json.JSONDecodeError as e:
                return (lineno, e.msg)
    return None


def count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    n = 0
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                n += 1
    return n


def quarantine_corrupt(registry: Path):
    """Copy current (corrupt) registry to .corrupt-<unix-ts>. Return new path."""
    ts = int(time.time())
    target = registry.parent / "retro-findings.jsonl.corrupt-{}".format(ts)
    bump = 0
    while target.exists():
        bump += 1
        target = registry.parent / "retro-findings.jsonl.corrupt-{}-{}".format(ts, bump)
    with registry.open("rb") as src, target.open("wb") as dst:
        dst.write(src.read())
    return target


def atomic_copy(src: Path, dst: Path) -> None:
    tmp = dst.parent / (dst.name + ".tmp-{}".format(os.getpid()))
    try:
        with src.open("rb") as s, tmp.open("wb") as t:
            t.write(s.read())
            t.flush()
            try:
                os.fsync(t.fileno())
            except OSError:
                pass
        os.replace(tmp, dst)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="recover_from_backup.py",
        description=(
            "Diagnose / recover a corrupt "
            "<project-root>/reports/_data/retro-findings.jsonl registry."
        ),
    )
    p.add_argument("--project-root", required=True, type=str)
    p.add_argument(
        "--rollback-to-latest-backup",
        action="store_true",
        help="Restore registry from the newest .bak-* file.",
    )
    p.add_argument(
        "--repair",
        type=int,
        default=None,
        metavar="N",
        help="Drop line N (1-indexed) and keep the rest.",
    )
    return p.parse_args(argv)


def main(argv):
    args = parse_args(argv)

    if args.rollback_to_latest_backup and args.repair is not None:
        print(
            "error: --rollback-to-latest-backup and --repair are mutually exclusive",
            file=sys.stderr,
        )
        return EXIT_ARGS

    project_root = Path(args.project_root).expanduser().resolve()
    if not project_root.exists():
        print(
            "error: project-root does not exist: {}".format(project_root),
            file=sys.stderr,
        )
        return EXIT_ARGS

    data_dir = project_root / "reports" / "_data"
    registry = data_dir / "retro-findings.jsonl"

    if not data_dir.exists():
        print(
            "error: data dir does not exist: {}".format(data_dir),
            file=sys.stderr,
        )
        return EXIT_GENERAL

    if not registry.exists():
        print("no registry at {}".format(registry))
        backups = list_backups(data_dir)
        if backups:
            print("available backups (newest first):")
            for p, m in backups:
                ts_iso = datetime.fromtimestamp(m, tz=timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
                print("  {}  ({})".format(p.name, ts_iso))
        return EXIT_OK

    # --- ROLLBACK PATH ---
    if args.rollback_to_latest_backup:
        backups = list_backups(data_dir)
        if not backups:
            print("error: no .bak-* files to roll back to", file=sys.stderr)
            return EXIT_GENERAL
        latest, _mtime = backups[0]
        quarantine = quarantine_corrupt(registry)
        atomic_copy(latest, registry)
        new_count = count_lines(registry)
        print(
            "rolled back from {} ({} non-empty lines). "
            "Quarantined corrupt file at {}".format(
                latest.name, new_count, quarantine.name
            )
        )
        return EXIT_OK

    # --- REPAIR PATH ---
    if args.repair is not None:
        n = args.repair
        if n < 1:
            print("error: --repair N must be >= 1", file=sys.stderr)
            return EXIT_ARGS
        # Read all lines (do NOT json-parse; we want to drop the bad one)
        with registry.open("r", encoding="utf-8") as fh:
            all_lines = [line.rstrip("\n").rstrip("\r") for line in fh]
        if n > len(all_lines):
            print(
                "error: --repair N={} exceeds line count {}".format(n, len(all_lines)),
                file=sys.stderr,
            )
            return EXIT_ARGS
        dropped = all_lines[n - 1]
        kept = all_lines[: n - 1] + all_lines[n:]
        quarantine = quarantine_corrupt(registry)
        tmp_path = data_dir / "retro-findings.jsonl.tmp-{}".format(os.getpid())
        try:
            with tmp_path.open("w", encoding="utf-8", newline="\n") as fh:
                for line in kept:
                    fh.write(line)
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
        print(
            "repaired: dropped line {} ({} chars). Kept {} lines. "
            "Quarantined original at {}".format(
                n, len(dropped), len(kept), quarantine.name
            )
        )
        return EXIT_OK

    # --- DIAGNOSE PATH (no flags) ---
    result = scan_for_first_corruption(registry)
    if result is None:
        print("no corruption detected in {}".format(registry))
        print("non-empty line count: {}".format(count_lines(registry)))
    else:
        lineno, err_msg = result
        print("Corruption detected at line {}: {}".format(lineno, err_msg))

    backups = list_backups(data_dir)
    if backups:
        print("available backups (newest first):")
        for p, m in backups:
            ts_iso = datetime.fromtimestamp(m, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            print("  {}  ({})".format(p.name, ts_iso))
    else:
        print("no .bak-* files available")
    return EXIT_OK


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception as e:
        print("error: {}".format(e), file=sys.stderr)
        sys.exit(EXIT_GENERAL)
