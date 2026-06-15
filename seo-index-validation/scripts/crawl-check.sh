#!/usr/bin/env bash
# crawl-check.sh - headless crawl/index hygiene probe for a deployed site.
#
# Usage:   bash crawl-check.sh <domain-or-url> [extra-path ...]
# Example: bash crawl-check.sh plumagedispatch.com
#          bash crawl-check.sh https://example.com /pricing /docs/intro
#
# No authentication is needed for the HTTP checks. If GSC_ACCESS_TOKEN is set
# (an OAuth bearer token with the webmasters.readonly scope), the script also
# lists the property's sitemaps via the Search Console API.
#
# It changes nothing on the site. Exit code is always 0; findings print as
# PASS / WARN / FAIL lines plus a one-line SUMMARY so a caller (or review-loop)
# can scan them.

set -uo pipefail

MAX_URLS="${MAX_URLS:-50}"   # cap on sitemap URLs probed; a hit is reported, never silent
TIMEOUT="${TIMEOUT:-20}"
UA="${UA:-Mozilla/5.0 (compatible; seo-index-validation/1.0; +curl)}"

# guard against a non-numeric override silently making the integer tests inert
case "$MAX_URLS" in ''|*[!0-9]*) echo "WARN  MAX_URLS=\"$MAX_URLS\" is not an integer; using 50"; MAX_URLS=50;; esac
case "$TIMEOUT"  in ''|*[!0-9]*) echo "WARN  TIMEOUT=\"$TIMEOUT\" is not an integer; using 20";  TIMEOUT=20;;  esac

passes=0; warns=0; fails=0
pass() { printf 'PASS  %s\n' "$1"; passes=$((passes+1)); }
warn() { printf 'WARN  %s\n' "$1"; warns=$((warns+1)); }
fail() { printf 'FAIL  %s\n' "$1"; fails=$((fails+1)); }
info() { printf '      %s\n' "$1"; }

# curl helpers
status_redirect() { # -> "<code> <redirect_url>"
  curl -s -A "$UA" -o /dev/null -w '%{http_code} %{redirect_url}' --max-time "$TIMEOUT" "$1" 2>/dev/null || printf '000 '
}
code_of() { status_redirect "$1" | awk '{print $1}'; }
body() { curl -sL -A "$UA" --max-time "$TIMEOUT" "$1" 2>/dev/null; }
title_of() { body "$1" | tr -d '\n' | grep -oiE '<title>[^<]*</title>' | head -1 | sed -E 's#</?[Tt][Ii][Tt][Ll][Ee]>##g'; }
extract_locs() { grep -oE '<loc>[^<]+</loc>' | sed -E 's#</?loc>##g'; }

arg="${1:-}"
if [ -z "$arg" ]; then
  echo "usage: bash crawl-check.sh <domain-or-url> [extra-path ...]"
  exit 0
fi
shift || true

host="$(printf '%s' "$arg" | sed -E 's#^[a-zA-Z]+://##; s#/.*$##')"
apex="$(printf '%s' "$host" | sed -E 's/^www\.//')"
base="https://$apex"
echo "== crawl-check: $base  (max ${MAX_URLS} urls) =="

# --- 1. host canonicalization -------------------------------------------------
echo "-- host canonicalization --"
hl="$(status_redirect "http://$apex/")"; hcode="$(echo "$hl" | awk '{print $1}')"
case "$hcode" in
  301|308) pass "http://$apex/ -> $(echo "$hl" | awk '{print $2}') ($hcode)";;
  200)     warn "http://$apex/ serves 200 (no upgrade to https; expected a 301)";;
  000)     info "http://$apex/ did not respond";;
  *)       warn "http://$apex/ returned $hcode";;
esac

wl="$(status_redirect "https://www.$apex/")"; wcode="$(echo "$wl" | awk '{print $1}')"; wredir="$(echo "$wl" | awk '{print $2}')"
case "$wcode" in
  301|308) pass "https://www.$apex/ redirects to apex ($wcode)";;
  200)
    if [ -n "$wredir" ]; then
      pass "https://www.$apex/ -> $wredir"
    else
      # 200 with no server redirect: a canonical pointing to the apex still consolidates,
      # so do not overstate a FAIL. curl cannot see a client-side (JS/meta) redirect.
      wcanon="$(body "https://www.$apex/" | tr -d '\n' | grep -oiE '<link[^>]*rel=["'"'"']canonical["'"'"'][^>]*>' | head -1)"
      if printf '%s' "$wcanon" | grep -qiE "https?://$apex/"; then
        warn "https://www.$apex/ serves 200, but its canonical points to the apex (Google consolidates; a 301 www->apex is still preferable)"
      else
        fail "https://www.$apex/ serves 200 independently with no apex canonical (duplicate host; add a 301 to one canonical host)"
      fi
    fi
    ;;
  000)     pass "https://www.$apex/ does not resolve (no duplicate host)";;
  *)       info "https://www.$apex/ returned $wcode";;
esac

# --- 2. sitemap discovery -----------------------------------------------------
echo "-- sitemap --"
sm=""
for cand in sitemap-index.xml sitemap_index.xml sitemap.xml sitemap-0.xml; do
  if [ "$(code_of "$base/$cand")" = "200" ]; then sm="$base/$cand"; break; fi
done

locs=""
if [ -n "$sm" ]; then
  pass "sitemap found: $sm"
  content="$(body "$sm")"
  if printf '%s' "$content" | grep -qi '<sitemapindex'; then
    info "sitemap is an index; following child sitemaps"
    children="$(printf '%s' "$content" | extract_locs)"
    # read line-by-line: a <loc> may legally contain spaces or glob chars
    # (*, ?, [) that an unquoted `for` would word-split or pathname-expand.
    while IFS= read -r ch; do
      [ -z "$ch" ] && continue
      child_locs="$(body "$ch" | extract_locs)"
      locs="$(printf '%s\n%s' "$locs" "$child_locs")"
    done <<EOF
$children
EOF
  else
    locs="$(printf '%s' "$content" | extract_locs)"
  fi
else
  warn "no sitemap found at the usual paths (sitemap-index.xml, sitemap.xml, sitemap-0.xml)"
fi

# fold in any extra paths passed on the command line
for p in "$@"; do
  case "$p" in
    http*) locs="$(printf '%s\n%s' "$locs" "$p")";;
    /*)    locs="$(printf '%s\n%s' "$locs" "$base$p")";;
    *)     locs="$(printf '%s\n%s' "$locs" "$base/$p")";;
  esac
done

locs="$(printf '%s\n' "$locs" | sed '/^$/d' | sort -u)"
total="$(printf '%s\n' "$locs" | sed '/^$/d' | grep -c . || true)"

# --- 3. per-URL status --------------------------------------------------------
echo "-- per-URL status (of $total) --"
n=0; bad=0
while IFS= read -r u; do
  [ -z "$u" ] && continue
  n=$((n+1))
  if [ "$n" -gt "$MAX_URLS" ]; then
    warn "URL cap reached: probed $MAX_URLS of $total sitemap URLs (raise MAX_URLS to probe more). Remaining NOT checked."
    break
  fi
  line="$(status_redirect "$u")"; code="$(echo "$line" | awk '{print $1}')"; rd="$(echo "$line" | awk '{print $2}')"
  case "$code" in
    200) : ;;  # quiet on healthy
    301|302|307|308) warn "$code  $u -> $rd  (sitemap URL should be the final 200 target, not a redirect)"; bad=$((bad+1));;
    404|410) fail "$code  $u  (sitemap lists a missing URL)"; bad=$((bad+1));;
    000) warn "no response  $u"; bad=$((bad+1));;
    *) warn "$code  $u"; bad=$((bad+1));;
  esac
done <<EOF
$locs
EOF
[ "$total" -gt 0 ] && [ "$bad" -eq 0 ] && pass "all probed sitemap URLs return 200"

# representative content URL for page-level checks: prefer a non-root path
# (the bare apex is a special case for the trailing-slash test), fall back to root.
sample="$(printf '%s\n' "$locs" | sed '/^$/d' | grep -vE '^https?://[^/]+/?$' | head -1)"
[ -z "$sample" ] && sample="$base/"

# --- 4. trailing-slash normalization -----------------------------------------
echo "-- trailing slash --"
# only meaningful for a non-root path that ends in a slash; the bare apex
# (host/) is the same resource with or without the slash, so skip it.
if printf '%s' "$sample" | grep -qE '^https?://[^/]+/.+/$'; then
  noslash="${sample%/}"
  tl="$(status_redirect "$noslash")"; tcode="$(echo "$tl" | awk '{print $1}')"
  case "$tcode" in
    301|308) pass "$noslash -> $(echo "$tl" | awk '{print $2}') ($tcode)";;
    200) warn "$noslash and $sample both serve 200 (trailing-slash duplicate; normalize with a redirect)";;
    *) info "$noslash returned $tcode";;
  esac
else
  info "no non-root trailing-slash URL available to test; skipping the slash check"
fi

# --- 5. soft-404 --------------------------------------------------------------
echo "-- soft-404 --"
# guaranteed-nonexistent path; randomize independent of date and bypass any edge cache
rand="$base/zzz-nonexistent-$$-${RANDOM:-0}${RANDOM:-0}-$(date +%s 2>/dev/null || echo 0)/"
rcode="$(curl -s -A "$UA" -H 'Cache-Control: no-cache' -o /dev/null -w '%{http_code}' --max-time "$TIMEOUT" "$rand" 2>/dev/null || echo 000)"
if [ "$rcode" = "404" ] || [ "$rcode" = "410" ]; then
  pass "an unknown URL returns $rcode (no soft-404)"
elif [ "$rcode" = "200" ]; then
  # any 200 on a URL that cannot exist is a soft-404; a correct site returns 404/410.
  home_title="$(title_of "$base/")"
  rand_title="$(title_of "$rand")"
  if [ -n "$home_title" ] && [ "$rand_title" = "$home_title" ]; then
    fail "soft-404: an unknown URL returns 200 serving the homepage (title \"$home_title\"). Add a real 404 page or remove an SPA catch-all."
  else
    fail "soft-404: an unknown URL returns 200 instead of 404/410. curl does not render JS, so confirm the served content with the GSC live test."
  fi
else
  info "an unknown URL returned $rcode"
fi

# --- 6. Cloudflare Scrape Shield (email obfuscation) --------------------------
echo "-- Cloudflare email obfuscation --"
ep_home="$(body "$base/" | grep -c 'cdn-cgi/l/email-protection' || true)"
ep_sample="$(body "$sample" | grep -c 'cdn-cgi/l/email-protection' || true)"
if [ "$ep_home" -gt 0 ] || [ "$ep_sample" -gt 0 ]; then
  fail "/cdn-cgi/l/email-protection links are injected (home:$ep_home sample:$ep_sample). Cloudflare Email Address Obfuscation (Scrape Shield) is ON; Googlebot crawls these and reports a 404. Turn it OFF in the zone's Security settings."
else
  pass "no /cdn-cgi/l/email-protection injection detected"
fi

# --- 7. canonical -------------------------------------------------------------
echo "-- canonical (homepage) --"
hc="$(body "$base/" | tr -d '\n' | grep -oiE '<link[^>]*rel=["'"'"']canonical["'"'"'][^>]*>' | head -1)"
[ -n "$hc" ] && info "$hc" || warn "no canonical tag found on the homepage"

# --- 8. optional: GSC Sitemaps API -------------------------------------------
if [ -n "${GSC_ACCESS_TOKEN:-}" ]; then
  echo "-- GSC Sitemaps API --"
  site="${GSC_SITE_URL:-sc-domain:$apex}"
  enc="$(printf '%s' "$site" | sed 's#:#%3A#g; s#/#%2F#g')"
  resp="$(curl -s -H "Authorization: Bearer $GSC_ACCESS_TOKEN" --max-time "$TIMEOUT" \
    "https://www.googleapis.com/webmasters/v3/sites/$enc/sitemaps" 2>/dev/null)"
  if printf '%s' "$resp" | grep -qi '"error"'; then
    warn "Sitemaps API call failed (check the token scope/property). Raw: $(printf '%s' "$resp" | head -c 300)"
  else
    info "Sitemaps API response (path / lastDownloaded / errors / warnings):"
    printf '%s' "$resp" | grep -oE '"(path|lastDownloaded|errors|warnings|isPending)"[^,}]*' | sed 's/^/        /'
  fi
else
  info "set GSC_ACCESS_TOKEN (webmasters.readonly) to also query the Search Console Sitemaps API"
fi

# --- summary ------------------------------------------------------------------
echo "-- summary --"
echo "SUMMARY: $passes pass, $warns warn, $fails fail. $([ "$fails" -gt 0 ] && echo 'Real defects found; see FAIL lines.' || echo 'No hard defects from the headless probe.')"
exit 0
