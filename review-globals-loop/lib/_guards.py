"""Shared safety + identity helpers for review-globals-loop — the SINGLE source of
truth so the privacy guard, the project-id normalization, and the similarity
metric are IDENTICAL across every helper (corpus_retrieve, claimpack,
ledger_store, recurrence_promote). Duplicating these per-file is what let an
inconsistent/incorrect guard slip through review, so they live here once.

Import from a sibling in lib/:        import _guards
Import from scripts/ (one level up):  sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib")); import _guards
"""
import sys
import re
import math
from pathlib import Path
from collections import Counter

# Distinct exit code for an unsafe output destination (privacy guard refusal).
EXIT_UNSAFE_OUT = 7


def _home_claude() -> Path:
    return (Path.home() / ".claude").resolve()


def skill_local_state() -> Path:
    """The ONLY path under ~/.claude this skill is allowed to write to."""
    return (_home_claude() / "skills" / "review-globals-loop" / ".local-state").resolve()


def _die(msg: str):
    sys.stderr.write("FATAL(guard): " + msg + "\n")
    sys.exit(EXIT_UNSAFE_OUT)


def assert_safe_out(out_path) -> Path:
    """Fail-CLOSED privacy guard for any --out destination.

    Refuses to write:
      * anywhere inside the ~/.claude config tree (CLAUDE.md, settings.json,
        skills/, memories) EXCEPT this skill's own .local-state/ scratch, and
      * anywhere inside a git working tree (walk up for a .git entry).
    On ANY error while evaluating safety it REFUSES (a boundary check that cannot
    prove safety must not assume it). Returns the resolved Path when safe.
    """
    try:
        p = Path(out_path).resolve()
        home_claude = _home_claude()
        local_state = skill_local_state()

        within_home_claude = (p == home_claude) or (home_claude in p.parents)
        within_local_state = (p == local_state) or (local_state in p.parents)
        if within_home_claude and not within_local_state:
            _die(
                "refusing --out inside the ~/.claude config tree: %s "
                "(only %s and below is writable)" % (p, local_state)
            )

        # The skill's own .local-state/ scratch is the one explicitly-sanctioned
        # write area; exempt it from the git-tree refusal too. Otherwise a
        # git-versioned ~ (dotfiles repo) or ~/.claude would lock out the whole
        # pipeline (corpus_retrieve + claimpack) with no escape hatch.
        if within_local_state:
            # Non-silent: if an ancestor IS a git working tree, the private corpus
            # could be committed. .local-state ships a .gitignore, but warn anyway
            # so the exemption is never a silent leak path.
            for anc in [p] + list(p.parents):
                if (anc / ".git").exists():
                    sys.stderr.write(
                        "WARN(guard): writing private corpus under a git working "
                        "tree (%s/.git). .local-state has a .gitignore, but ensure "
                        "~/.claude is not a committed/remoted repo.\n" % anc
                    )
                    break
            return p

        for anc in [p] + list(p.parents):
            if (anc / ".git").exists():
                _die("refusing --out inside a git working tree (%s/.git): %s" % (anc, p))

        return p
    except SystemExit:
        raise
    except Exception as e:  # fail-closed: cannot prove safe -> refuse
        _die("could not verify --out safety (%r); refusing" % (e,))


def normalize_project_id(pid) -> str:
    """Canonical project-id identity used by the recurrence count, the claims-pack
    distinct-project count, and the ledger >=2-distinct-project gate. They MUST
    agree or a 'survived' proposal can be rejected at upsert."""
    return (pid or "").strip().lower()


# --- similarity: token + word-boundary char-3gram cosine (robust to wording) ---
_WORD = re.compile(r"[a-z0-9]+")


def _feature_vector(text) -> Counter:
    s = (text or "").lower()
    toks = _WORD.findall(s)
    feats = Counter("w:" + t for t in toks)
    compact = " ".join(toks)
    for i in range(len(compact) - 2):
        feats["c:" + compact[i:i + 3]] += 1
    return feats


def cosine_sim(a, b) -> float:
    """Cosine similarity over the token+char-3gram feature space. Identical text
    -> 1.0; unrelated -> ~0. Used for cross-repo friction clustering and within-set
    dedup so both match the fleet 'cosine' constant."""
    fa, fb = _feature_vector(a), _feature_vector(b)
    if not fa or not fb:
        return 0.0
    common = set(fa) & set(fb)
    dot = sum(fa[k] * fb[k] for k in common)
    na = math.sqrt(sum(v * v for v in fa.values()))
    nb = math.sqrt(sum(v * v for v in fb.values()))
    return dot / (na * nb) if na and nb else 0.0
