---
name: seo-index-validation
description: Validate whether a DEPLOYED site is being crawled and indexed correctly, and diagnose plus fix why pages are not indexed. Use when a Google Search Console alert email arrives ("New reason preventing your pages from being indexed", "Not found (404)", "Page with redirect", "Discovered / currently not indexed", "Crawled / currently not indexed", "Soft 404"), when checking index health after a deploy, migration, or domain change, when a page will not appear in Google, or to confirm a prior indexing fix actually cleared in Search Console. It probes live HTTP (status codes, redirects, soft-404, canonical tags, sitemap) with curl, reads the GSC report via the browser, fixes at the correct layer (CDN or host setting, _redirects, a 404 page) under confirmation-gating, then schedules a lagged re-check. NOT for keyword research, content optimization, or backlinks. Triggers include "Search Console", "GSC", "not indexed", "404 in Search Console", "page with redirect", "soft 404", "validate indexing", "validate SEO", "why is my page not indexed", "did the indexing fix work".
---

# seo-index-validation

Diagnose and fix **crawl and index health** for a *deployed* site, then verify the fix in Google Search Console (GSC). The scope is deliberately narrow: the reasons Google gives for *not indexing* a page (404, redirect, soft-404, duplicate or wrong canonical, discovered-not-indexed) and the live-infrastructure causes behind them. This is **not** a keyword, content, or backlink skill. If asked for those, say so and stop.

## Operating principle

- **Reproduce before you believe.** Every diagnosis is grounded in a live HTTP probe or the GSC report, never in memory. Tag each finding *measured*, *derived*, or *assumed*.
- **Exact URLs, not buckets.** A GSC alert names a *reason* ("Not found (404)"). The load-bearing output is the *exact affected URL(s)*. Get them.
- **Separate benign from real.** Most redirects Google reports are correct canonicalization. Do not "fix" them (see the table below).
- **Automate the readable half; browse only where there is no API.** Default to curl and the Search Console API. The browser is only for the three GSC actions that have no API.
- **Confirmation-gate live changes.** Toggling a CDN or host setting, or shipping a redirect, is outward-facing. Propose, get an explicit yes, then act. Never unattended.

## What is automatable vs browser-only (verified against official docs)

| Step | curl / API? | How |
|------|-------------|-----|
| Live status, redirects, soft-404, homepage canonical presence, sitemap resolution | yes, curl | `scripts/crawl-check.sh` |
| Per-URL canonical correctness (right or duplicate canonical) | no, browser | inspect per URL in GSC; the script only confirms the homepage tag is present |
| Per-URL index status | yes, API | Search Console `urlInspection.index.inspect` (read-only; cannot live-test) |
| Submit and confirm a sitemap | yes, API | Sitemaps API `PUT` / `GET .../sites/{site}/sitemaps` |
| Enumerate the URLs behind a GSC reason bucket | no, browser | There is no index-coverage API; the per-URL list is GSC UI only |
| "Test Live URL" (fresh fetch plus rendered HTML) | no, browser | URL Inspection API returns only the already-indexed version |
| Request Indexing for ordinary pages | no, browser | Search Console API has no such method; the Indexing API is officially scoped to JobPosting and BroadcastEvent only. Use the GSC UI button (limited per day) |
| Toggle a CDN setting (e.g. Cloudflare Scrape Shield) | yes, API, gated | `PATCH .../zones/{id}/settings/{setting_id}`; needs a write-scoped token; confirmation-gated |

`gcloud` is not involved. GSC is not a Google Cloud product; it is the Search Console REST API (OAuth, where a GCP project only mints credentials). There is no official first-party Search Console CLI; every `gsc`-style CLI is third-party.

## The playbook

**1. Identify the exact URLs (browser).** Open GSC, the property, Indexing, Pages, click the reason ("Not found (404)", "Page with redirect", and so on), and read the example URLs. The alert email never lists them. If no authenticated browser is available, say so. Do not guess the list.

**2. Reproduce live (curl, headless).** Run `bash ~/.claude/skills/seo-index-validation/scripts/crawl-check.sh <domain>` (use the full path; the skill is not invoked from its own directory). It checks status and redirect for each sitemap URL (up to a `MAX_URLS` cap, default 50; it WARNs when the cap truncates the list), plus http to https, www vs apex duplication (softened when a canonical already consolidates), trailing-slash normalization, soft-404 (any unknown URL returning 200 instead of 404 or 410), the homepage canonical tag's presence, and the Cloudflare `/cdn-cgi/l/email-protection` Scrape Shield artifact. With `GSC_ACCESS_TOKEN` set it also lists the property's sitemaps via the Search Console API. Read its report. Note that curl does not render JavaScript, so a clean curl result on a JS-heavy page still warrants the GSC live test in step 5.

**3. Classify benign vs real.**

| Reason or symptom | Usually | Action |
|-------------------|---------|--------|
| `http://` to `https://` 301 | benign (correct) | none |
| `/path` to `/path/` 308 trailing slash | benign | none |
| Discovered or Crawled, currently not indexed | benign on a young or low-authority site | request indexing, then wait; not a defect |
| `/cdn-cgi/l/email-protection` 404 | real | disable Cloudflare Email Obfuscation (Scrape Shield) |
| Soft-404 (homepage served at 200 for unknown URLs) | real | add a real 404 page; remove any SPA catch-all |
| `www` host serves 200 independently | real (duplicate) | 301 www to apex, or apex to www, pick one |
| Genuinely removed or renamed URL 404 | real | restore, redirect, or 404 it intentionally |

**4. Fix at the correct layer (confirmation-gated).** Decide CDN or host setting (e.g. Cloudflare Scrape Shield, a redirect rule) vs repo (`public/_redirects`, `src/pages/404.*`). Live and security changes need an explicit yes and a write-scoped token; an analytics token is typically read-only. Note that the deploy may be decoupled from git (for example a manual `wrangler pages deploy`), so a repo fix is not live until the site is rebuilt and redeployed. Confirm what is actually serving before claiming a fix shipped.

**5. Verify.** (a) Re-run `crawl-check.sh`; the artifact or symptom should be gone (headless, immediate). (b) In GSC URL Inspection, run **Test Live URL** on an affected page (browser), open "View tested page", and confirm the offending pattern is absent. The *report* still lags; the live test is the earliest authoritative Google-side signal.

**6. Nudge recrawl.** Confirm the sitemap is submitted and healthy (Sitemaps API or GSC). For affected pages, use **Request Indexing** in GSC URL Inspection (browser, limited per day). For a 404 whose links you removed, do not click "Validate Fix" if the URL itself still returns 404 (for example a permanent CDN endpoint); removing the links plus natural drop-off is cleaner, and validation can log a cosmetic "Failed".

**7. Schedule a lagged re-check.** GSC report refresh and recrawl take days to weeks. Create a one-time scheduled task (via the `schedule` skill or the scheduled-tasks tool) about two weeks out that re-runs `crawl-check.sh` (always works headless) and checks the GSC reason count (browser), degrading gracefully and saying so if the browser is unavailable. Do not claim the fix "worked" from the live test alone; the report must clear.

## Compose, do not duplicate

- Hand the terminal "benign vs real" or "the fix worked" judgment to **`/review-loop --mode claim`** to falsify against the curl output and GSC screenshots.
- Use the **`schedule`** skill for step 7, and the **Claude-in-Chrome** MCP (load via ToolSearch) for the browser steps.
- Honor the global rules: claims-rigor (tag measured, derived, assumed; exact URLs not buckets), confirmation-gating for live changes, and no em-dashes in any site copy the skill produces.

## Helper

`~/.claude/skills/seo-index-validation/scripts/crawl-check.sh <domain> [path ...]` is a headless live probe that needs no auth (call it by full path; the skill is not invoked from its own directory). If `GSC_ACCESS_TOKEN` (an OAuth bearer with the `webmasters.readonly` scope) is exported, it also lists the property's sitemaps via the Search Console API. Findings print as PASS / WARN / FAIL plus a one-line SUMMARY. It reads the homepage canonical for presence only, and does not render JavaScript, so pair a clean result with the GSC live test for JS-heavy pages. Run it first in any indexing investigation.
