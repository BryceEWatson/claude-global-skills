#!/usr/bin/env python3
"""Tests for register_finding.py -- confidence must agree with its own counts.

The defect these cover: `--confidence` was accepted verbatim, so a row could
store 0.9 next to `supporting=3 / contradicting=0` whose own documented formula
(SKILL.md sec. 7) yields 0.6. The counts and the confidence live in the same
row, so a disagreement makes the row self-contradicting and silently overstates
the evidence to every later reader.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

LIB = Path(__file__).resolve().parent.parent / "lib"
sys.path.insert(0, str(LIB))

import register_finding as rf  # noqa: E402

SCRIPT = LIB / "register_finding.py"

try:  # register_finding.py needs these to get past its own dependency check
    import filelock  # noqa: F401
    import jsonschema  # noqa: F401

    MISSING_DEPS = None
except ImportError as exc:  # pragma: no cover - environment-dependent
    MISSING_DEPS = "needs optional deps ({}): pip install -r requirements-optional.txt".format(exc.name)

# Skipping keeps the suite runnable without the optional deps, but a silent skip
# in CI would let the headline confidence gate go unexercised behind a green
# check. CI sets this so a missing dep is a hard failure there.
if MISSING_DEPS is not None and os.environ.get("REQUIRE_OPTIONAL_DEPS") == "1":
    raise RuntimeError(
        "REQUIRE_OPTIONAL_DEPS=1 but " + MISSING_DEPS + " -- refusing to skip "
        "these tests silently."
    )

needs_deps = unittest.skipIf(MISSING_DEPS is not None, MISSING_DEPS or "")

EXIT_OK = 0
EXIT_ARGS = 5

BASE = [
    "--retro-path", "research/studies/r.md",
    "--project", "demo",
    "--category", "demo",
    "--claim", "A claim long enough to satisfy the schema minLength.",
    "--proposed-action", "do the thing",
]


def _reject_constant(name):
    raise AssertionError("row contains non-standard JSON constant: {}".format(name))


def invoke(project_root, *extra, dry_run=True):
    argv = [sys.executable, str(SCRIPT), "--project-root", str(project_root)]
    argv += BASE + list(extra)
    if dry_run:
        argv.append("--dry-run")
    return subprocess.run(argv, capture_output=True, text=True)


class TestSmoothedConfidence(unittest.TestCase):
    """The formula itself, against SKILL.md sec. 7's published table."""

    def test_published_table(self):
        cases = [(1, 0, 0.33), (2, 0, 0.50), (6, 0, 0.75), (6, 1, 0.67), (12, 1, 0.80)]
        for supporting, contradicting, expected in cases:
            with self.subTest(s=supporting, c=contradicting):
                got = round(rf.smoothed_confidence(supporting, contradicting), 2)
                self.assertAlmostEqual(got, expected, places=2)

    def test_zero_evidence_is_zero_not_a_crash(self):
        self.assertEqual(rf.smoothed_confidence(0, 0), 0.0)

    def test_smoothing_prevents_certainty(self):
        """The +2 prior is why a single observation can never read as 'always'."""
        self.assertLess(rf.smoothed_confidence(1, 0), 0.5)
        self.assertLess(rf.smoothed_confidence(1000, 0), 1.0)


@needs_deps
class TestConfidenceGate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_omitted_confidence_is_computed_from_counts(self):
        res = invoke(self.root, "--evidence-supporting", "3", "--evidence-contradicting", "0")
        self.assertEqual(res.returncode, EXIT_OK, res.stderr)
        self.assertEqual(json.loads(res.stdout)["confidence"], 0.6)

    def test_computed_confidence_is_rounded_for_storage(self):
        res = invoke(self.root, "--evidence-supporting", "10", "--evidence-contradicting", "0")
        self.assertEqual(res.returncode, EXIT_OK, res.stderr)
        self.assertEqual(json.loads(res.stdout)["confidence"], 0.83)

    def test_mismatch_is_refused(self):
        """The exact shape of the two bad rows found in a real ledger: 0.9 beside 3/0."""
        res = invoke(
            self.root,
            "--evidence-supporting", "3",
            "--evidence-contradicting", "0",
            "--confidence", "0.9",
        )
        self.assertEqual(res.returncode, EXIT_ARGS)
        self.assertEqual(res.stdout.strip(), "", "must not emit a row it refused")
        self.assertIn("contradicts the evidence counts", res.stderr)
        self.assertIn("0.6", res.stderr, "must show the caller the expected value")

    def test_second_real_world_mismatch_is_refused(self):
        res = invoke(
            self.root,
            "--evidence-supporting", "3",
            "--evidence-contradicting", "0",
            "--confidence", "0.95",
        )
        self.assertEqual(res.returncode, EXIT_ARGS)

    def test_agreeing_confidence_is_accepted(self):
        res = invoke(
            self.root,
            "--evidence-supporting", "6",
            "--evidence-contradicting", "0",
            "--confidence", "0.75",
        )
        self.assertEqual(res.returncode, EXIT_OK, res.stderr)
        self.assertEqual(json.loads(res.stdout)["confidence"], 0.75)

    def test_two_decimal_rounding_is_within_tolerance(self):
        """10/12 = 0.8333; a caller storing 0.83 agrees and must not be refused."""
        res = invoke(
            self.root,
            "--evidence-supporting", "10",
            "--evidence-contradicting", "0",
            "--confidence", "0.83",
        )
        self.assertEqual(res.returncode, EXIT_OK, res.stderr)
        self.assertEqual(json.loads(res.stdout)["confidence"], 0.83)

    def test_just_outside_tolerance_is_refused(self):
        res = invoke(
            self.root,
            "--evidence-supporting", "10",
            "--evidence-contradicting", "0",
            "--confidence", "0.84",
        )
        self.assertEqual(res.returncode, EXIT_ARGS)

    def test_contradicting_evidence_lowers_the_bar(self):
        """6/1 -> 0.67, so 0.75 (the 6/0 answer) must now be refused."""
        ok = invoke(
            self.root,
            "--evidence-supporting", "6",
            "--evidence-contradicting", "1",
            "--confidence", "0.67",
        )
        self.assertEqual(ok.returncode, EXIT_OK, ok.stderr)
        bad = invoke(
            self.root,
            "--evidence-supporting", "6",
            "--evidence-contradicting", "1",
            "--confidence", "0.75",
        )
        self.assertEqual(bad.returncode, EXIT_ARGS)

    def test_nan_confidence_is_refused(self):
        """Every comparison against NaN is False, so it slid past the gate AND the
        schema's min/max, then serialized as bare `NaN` -- not valid JSON."""
        res = invoke(
            self.root,
            "--evidence-supporting", "3",
            "--evidence-contradicting", "0",
            "--confidence", "nan",
        )
        self.assertEqual(res.returncode, EXIT_ARGS)
        self.assertIn("finite", res.stderr)
        self.assertNotIn("NaN", res.stdout)

    def test_infinite_confidence_is_refused(self):
        # `--confidence=-inf`, not `--confidence -inf`: argparse reads a bare
        # `-inf` as an option name and bails with its own exit 2 before the
        # gate runs. The `=` form is what reaches the code under test.
        for value in ("--confidence=inf", "--confidence=-inf"):
            with self.subTest(value=value):
                res = invoke(
                    self.root,
                    "--evidence-supporting", "3",
                    "--evidence-contradicting", "0",
                    value,
                )
                self.assertEqual(res.returncode, EXIT_ARGS, res.stderr)
                self.assertIn("finite", res.stderr)

    def test_emitted_row_is_strict_json(self):
        """json.dumps happily writes NaN/Infinity; a stored row must never contain them."""
        res = invoke(self.root, "--evidence-supporting", "3", "--evidence-contradicting", "0")
        self.assertEqual(res.returncode, EXIT_OK, res.stderr)
        json.loads(res.stdout, parse_constant=_reject_constant)

    def test_exact_tolerance_boundary_is_accepted(self):
        """6/0 -> 0.75; 0.755 is exactly 0.005 away, and the docs promise 'within 0.005'."""
        res = invoke(
            self.root,
            "--evidence-supporting", "6",
            "--evidence-contradicting", "0",
            "--confidence", "0.755",
        )
        self.assertEqual(res.returncode, EXIT_OK, res.stderr)
        self.assertEqual(
            json.loads(res.stdout)["confidence"], 0.75,
            "a value that merely passed the check must not be stored verbatim",
        )

    def test_supplied_confidence_is_never_stored_verbatim(self):
        """--confidence is an assertion to check, not a value to keep."""
        res = invoke(
            self.root,
            "--evidence-supporting", "10",
            "--evidence-contradicting", "0",
            "--confidence", "0.834",
        )
        self.assertEqual(res.returncode, EXIT_OK, res.stderr)
        self.assertEqual(json.loads(res.stdout)["confidence"], 0.83)

    def test_negative_counts_are_refused_before_dividing(self):
        """s=-2, c=0 would make the denominator zero."""
        res = invoke(
            self.root, "--evidence-supporting", "-2", "--evidence-contradicting", "0"
        )
        self.assertEqual(res.returncode, EXIT_ARGS)
        self.assertIn(">= 0", res.stderr)
        self.assertNotIn("Traceback", res.stderr)


@needs_deps
class TestUncommittedWarning(unittest.TestCase):
    """A tracked-but-uncommitted row is discarded by any working-tree revert.

    Observed twice in one 13-hour window: rows were appended to the working tree
    of a repo with many concurrent agent worktrees, and a `git restore` put the
    file back to its committed state.
    """

    def _git(self, cwd, *args):
        return subprocess.run(
            ["git", *args], cwd=str(cwd), capture_output=True, text=True
        )

    def _repo(self, d):
        root = Path(d)
        self._git(root, "init", "-q")
        self._git(root, "config", "user.email", "t@example.com")
        self._git(root, "config", "user.name", "t")
        return root

    def _register(self, root):
        return invoke(
            root, "--evidence-supporting", "3", "--evidence-contradicting", "0",
            dry_run=False,
        )

    def test_warns_when_tracked_and_uncommitted(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._repo(d)
            reg = root / "reports" / "_data" / "retro-findings.jsonl"
            reg.parent.mkdir(parents=True)
            reg.write_text("", encoding="utf-8")
            self._git(root, "add", "-A")
            self._git(root, "commit", "-qm", "seed")

            res = self._register(root)
        self.assertEqual(res.returncode, EXIT_OK, res.stderr)
        self.assertIn("NOT committed", res.stderr)
        self.assertIn("git restore", res.stderr.replace("`", ""))

    def test_silent_when_the_registry_is_untracked(self):
        """An untracked file is not what a revert touches."""
        with tempfile.TemporaryDirectory() as d:
            root = self._repo(d)
            (root / "seed.txt").write_text("x", encoding="utf-8")
            self._git(root, "add", "-A")
            self._git(root, "commit", "-qm", "seed")

            res = self._register(root)
        self.assertEqual(res.returncode, EXIT_OK, res.stderr)
        self.assertNotIn("NOT committed", res.stderr)

    def test_silent_outside_a_git_repo(self):
        with tempfile.TemporaryDirectory() as d:
            res = self._register(Path(d))
        self.assertEqual(res.returncode, EXIT_OK, res.stderr)
        self.assertNotIn("NOT committed", res.stderr)

    def test_warning_never_changes_the_exit_code(self):
        """It is advice, not a gate -- callers keying on exit status must not break."""
        with tempfile.TemporaryDirectory() as d:
            root = self._repo(d)
            reg = root / "reports" / "_data" / "retro-findings.jsonl"
            reg.parent.mkdir(parents=True)
            reg.write_text("", encoding="utf-8")
            self._git(root, "add", "-A")
            self._git(root, "commit", "-qm", "seed")
            res = self._register(root)
        self.assertEqual(res.returncode, EXIT_OK)
        self.assertTrue(res.stdout.strip(), "the finding_id still goes to stdout")

    def test_dry_run_does_not_warn(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._repo(d)
            res = invoke(
                root, "--evidence-supporting", "3", "--evidence-contradicting", "0"
            )
        self.assertNotIn("NOT committed", res.stderr)


@needs_deps
class TestExistingRowsUntouched(unittest.TestCase):
    def test_registering_does_not_rewrite_prior_rows(self):
        """A pre-existing row whose confidence disagrees with its counts stays as written."""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            data_dir = root / "reports" / "_data"
            data_dir.mkdir(parents=True)
            registry = data_dir / "retro-findings.jsonl"
            legacy = {
                "finding_id": "2026-06-02-001",
                "retro_date": "2026-06-02",
                "retro_path": "research/studies/old.md",
                "project": "demo",
                "category": "demo",
                "claim": "A legacy claim registered before the gate existed.",
                "confidence": 0.9,
                "evidence_supporting": 3,
                "evidence_contradicting": 0,
                "proposed_action": "do the thing",
                "target_date": None,
                "follow_up_status": "pending",
                "appended_at": "2026-06-02T00:00:00Z",
                "appended_by": "test",
            }
            legacy_line = json.dumps(legacy, ensure_ascii=False, sort_keys=True)
            registry.write_text(legacy_line + "\n", encoding="utf-8")

            res = invoke(
                root,
                "--evidence-supporting", "3",
                "--evidence-contradicting", "0",
                dry_run=False,
            )
            self.assertEqual(res.returncode, EXIT_OK, res.stderr)

            lines = registry.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(lines[0], legacy_line, "legacy row must be byte-identical")
            self.assertEqual(json.loads(lines[0])["confidence"], 0.9)
            self.assertEqual(json.loads(lines[1])["confidence"], 0.6)


if __name__ == "__main__":
    unittest.main()
