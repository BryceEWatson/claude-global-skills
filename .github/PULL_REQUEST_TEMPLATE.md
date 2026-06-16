<!-- One-line summary of what this PR changes and why: -->


## Checklist

- [ ] No `.local-state/` contents or secrets in the diff (run `git diff` and confirm).
- [ ] Tests pass locally:
  - `node --test review-loop/*.test.cjs`
  - `python -m unittest discover -s gemini-image/tests -p 'test_*.py'`
  - `python pattern-retrospective/lib/krippendorff_alpha.py --test`
- [ ] If a skill changed: its `SKILL.md` frontmatter conforms to `SKILL-SPEC.md` (`name` equals the directory name).
- [ ] No hardcoded personal absolute paths — derive from `Path.home()` / `os.homedir()`.
- [ ] `/review-loop` verdict attached (required gate — skills run via hooks and can auto-apply fixes, so a PR is a code-execution vector).
