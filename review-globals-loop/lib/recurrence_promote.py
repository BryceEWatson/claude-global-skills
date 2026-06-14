#!/usr/bin/env python3
"""recurrence_promote.py - cross-project recurrence promotion.

Reads a fleet-wide friction list (JSON array of SurfacedFriction objects, each
with scope:[repoId] and scopeAttribution:{repoId: 'path'|'keyword'|...}),
clusters them by the SHARED _guards.cosine_sim similarity (token + char-3gram
cosine) over summary+proposedChange (single-link agglomerative via union-find),
computes the DISTINCT-PATH-PROJECT count per cluster (distinct repoIds across
members' scope that were PATH-attributed -- keyword-attributed repoIds are
EXCLUDED so a chat that merely mentions another project cannot manufacture a
false global candidate), applies the >=N-distinct-projects promotion rule, and
emits ranked global candidates.

Clustering uses _guards.cosine_sim (the single fleet similarity metric, shared
with claimpack/ledger dedup) so the canonical cross-repo paraphrase pair
(cosine 0.478) clusters above the 0.40 bar while unrelated pairs (~0.11) stay
apart -- pure Jaccard at 0.38 missed that pair (Jaccard 0.375) and produced
zero global candidates.

Attribution source of truth, in priority order, per (friction, repoId):
  1. friction['scopeAttribution'][repoId]  (explicit map)
  2. the attribution of any evidence item whose projectId == repoId
  3. otherwise treated as NON-path (excluded from the count) -- fail closed.

Honest caveat: similarity clustering only. Two semantically-identical frictions
worded with no shared tokens/char-grams will still NOT cluster. The project
COUNT is measured; "same friction across repos" is derived.

Exit codes: 0 ok, 5 bad args.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _guards  # noqa: E402  (shared safety + identity + similarity helpers)

DEFAULT_THRESHOLD = 0.40
DEFAULT_MIN_PROJECTS = 2


class UnionFind:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


def _blob(f) -> str:
    return f"{f.get('summary','')} {f.get('proposedChange','')}"


def cluster(frictions, threshold=DEFAULT_THRESHOLD):
    """Single-link agglomerative over _guards.cosine_sim; returns list of index-lists."""
    n = len(frictions)
    blobs = [_blob(f) for f in frictions]
    uf = UnionFind(n)
    for i in range(n):
        for j in range(i + 1, n):
            if _guards.cosine_sim(blobs[i], blobs[j]) >= threshold:
                uf.union(i, j)
    groups = {}
    for i in range(n):
        groups.setdefault(uf.find(i), []).append(i)
    return list(groups.values())


def _evidence_attr_for(friction, repo_norm):
    """Find an attribution for repo (normalized id) among evidence items."""
    for e in friction.get("evidence", []) or []:
        if not isinstance(e, dict):
            continue
        if _guards.normalize_project_id(e.get("projectId")) == repo_norm:
            a = e.get("attribution")
            if a:
                return a
    return None


def path_scope_projects(friction):
    """Distinct NORMALIZED repoIds in a friction's scope that are PATH-attributed.

    Fail-closed: a repoId is counted ONLY when it is explicitly path-attributed
    (via scopeAttribution or a path-attributed evidence item). Keyword/none/
    unknown are excluded so keyword over-attribution cannot inflate the count.
    Ids are normalized via _guards.normalize_project_id so this distinct count
    agrees with claimpack's pack count and the ledger's >=2-project gate.
    """
    scope = friction.get("scope") or []
    attr_map = friction.get("scopeAttribution") or {}
    # normalize the attr_map keys so 'path' lookup survives case/whitespace drift
    norm_attr = {_guards.normalize_project_id(k): v for k, v in attr_map.items()}
    out = set()
    for s in scope:
        if not (isinstance(s, str) and s and s != "(unattributed)"):
            continue
        sn = _guards.normalize_project_id(s)
        if not sn:
            continue
        mode = norm_attr.get(sn) or _evidence_attr_for(friction, sn)
        if mode == "path":
            out.add(sn)
    return out


def all_scope_projects(friction):
    """All distinct repoIds in scope (for reporting only -- NOT the count)."""
    scope = friction.get("scope") or []
    out = set()
    for s in scope:
        if isinstance(s, str) and s and s != "(unattributed)":
            out.add(s)
    return out


def distinct_path_projects(members, frictions):
    projs = set()
    occ = 0
    for idx in members:
        f = frictions[idx]
        projs |= path_scope_projects(f)
        occ += 1
    return projs, occ


def near_miss_pairs(frictions, low=DEFAULT_THRESHOLD - 0.10, high=DEFAULT_THRESHOLD):
    """Surface near-misses just under the cluster threshold for human promotion review.

    The band is [threshold-0.10, threshold) on the SAME _guards.cosine_sim metric
    used by cluster(): pairs from DISTINCT path-attributed projects that sit just
    below the bar, so the SKILL workflow can surface them rather than silently
    dropping a near-identical friction.
    """
    n = len(frictions)
    blobs = [_blob(f) for f in frictions]
    out = []
    for i in range(n):
        for j in range(i + 1, n):
            r = _guards.cosine_sim(blobs[i], blobs[j])
            if low <= r < high:
                pi = path_scope_projects(frictions[i])
                pj = path_scope_projects(frictions[j])
                if pi and pj and pi != pj:
                    out.append({
                        "ratio": round(r, 3),
                        "a": frictions[i].get("summary", "")[:80],
                        "b": frictions[j].get("summary", "")[:80],
                    })
    out.sort(key=lambda m: -m["ratio"])
    return out[:20]


def promote(frictions, min_projects=DEFAULT_MIN_PROJECTS, threshold=DEFAULT_THRESHOLD):
    clusters = cluster(frictions, threshold)
    results = []
    for members in clusters:
        projs, occ = distinct_path_projects(members, frictions)
        dpc = len(projs)
        rep = frictions[members[0]]
        verdict = "global-candidate" if dpc >= min_projects else "per-repo (1-project leftover)"
        # merge evidence across members (only path-attributed projects count, but
        # keep all evidence for the human-readable pack)
        evidence = []
        all_projs = set()
        for idx in members:
            evidence.extend(frictions[idx].get("evidence", []) or [])
            all_projs |= all_scope_projects(frictions[idx])
        results.append({
            "summary": rep.get("summary", ""),
            "proposedChange": rep.get("proposedChange", ""),
            "targetHint": rep.get("targetHint", ""),
            "distinctProjects": sorted(projs),            # PATH-attributed only
            "distinctProjectCount": dpc,                  # the bar is on THIS
            "allScopeProjects": sorted(all_projs),        # incl. keyword (report only)
            "occurrenceCount": occ,
            "memberCount": len(members),
            "verdict": verdict,
            "evidence": evidence,
        })
    # rank: distinctProjectCount desc, then occurrences desc
    results.sort(key=lambda r: (-r["distinctProjectCount"], -r["occurrenceCount"]))
    return results


def parse_args(argv):
    p = argparse.ArgumentParser(prog="recurrence_promote.py")
    p.add_argument("--frictions", required=True, help="path to fleet-frictions.json")
    p.add_argument("--min-projects", type=int, default=DEFAULT_MIN_PROJECTS)
    p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    p.add_argument("--json", action="store_true")
    return p.parse_args(argv)


def main(argv):
    # C4: machine default is cp1252; em-dash / smart-quote / arrow in summaries
    # would crash the helper on its first real turn without this.
    for s in (sys.stdout, sys.stderr):
        if hasattr(s, "reconfigure"):
            s.reconfigure(encoding="utf-8")
    args = parse_args(argv)
    try:
        with open(args.frictions, "r", encoding="utf-8") as fh:
            frictions = json.load(fh)
    except Exception as e:
        print(f"error: cannot read {args.frictions}: {e}", file=sys.stderr)
        return 5
    if not isinstance(frictions, list):
        print("error: frictions file must be a JSON array", file=sys.stderr)
        return 5

    results = promote(frictions, args.min_projects, args.threshold)
    candidates = [r for r in results if r["verdict"] == "global-candidate"]
    leftovers = [r for r in results if r["verdict"] != "global-candidate"]
    near_low = round(args.threshold - 0.10, 4)
    near = near_miss_pairs(frictions, near_low, args.threshold)

    if args.json:
        print(json.dumps({
            "minProjects": args.min_projects,
            "threshold": args.threshold,
            "nearMissBand": [near_low, args.threshold],
            "globalCandidates": candidates,
            "oneProjectLeftovers": leftovers,
            "nearMissPairs": near,
            "caveat": "SIMILARITY clustering only (_guards.cosine_sim token+char-3gram "
                      "cosine); semantically-identical frictions with no shared tokens or "
                      "char-grams will NOT cluster. distinctProjectCount counts ONLY "
                      "path-attributed projects (ids normalized) and is measured; 'same "
                      "friction across repos' is derived. 1-project leftovers belong to "
                      "transcript-analysis (per-project CLAUDE.md) or /signal-scan "
                      "(per-repo signal), not this skill.",
        }, ensure_ascii=False, indent=2))
    else:
        print(f"Promotion rule: >= {args.min_projects} distinct PATH-attributed projects "
              f"(cluster threshold {args.threshold})")
        print(f"GLOBAL CANDIDATES ({len(candidates)}):")
        for i, c in enumerate(candidates, 1):
            print(f"  {i}. [{c['distinctProjectCount']} path-projects, {c['occurrenceCount']} occ] "
                  f"{c['summary']}")
            print(f"     projects: {', '.join(c['distinctProjects'])}")
        if leftovers:
            print(f"\n1-PROJECT LEFTOVERS ({len(leftovers)}) -> transcript-analysis / "
                  f"/signal-scan (NOT global):")
            for c in leftovers[:10]:
                print(f"  - {c['summary'][:70]}  (path-projects: "
                      f"{', '.join(c['distinctProjects']) or 'none'})")
        if near:
            print(f"\nNear-miss pairs ({near_low}<=r<{args.threshold}, different projects) "
                  f"for human promotion review:")
            for m in near[:10]:
                print(f"  r={m['ratio']}  {m['a']}  ~~  {m['b']}")
        print("\nNote: cosine-similarity clustering (_guards.cosine_sim); reworded "
              "duplicates with no shared tokens/char-grams may still be missed. "
              "Count is PATH-attributed projects only (keyword excluded); 'same friction' "
              "is derived.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

