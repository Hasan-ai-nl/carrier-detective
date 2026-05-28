# Carrier Detective v2 — Robustness Upgrade Design

**Status:** Approved for implementation
**Date:** 2026-05-27
**Context:** Post-spike hardening. v1 was an 8h throwaway that shipped; this is the data-driven upgrade.

---

## 1. Goal

Improve real-world reliability of `detect(url)` without changing its contract or the spike's spirit (single Python file, single HTML file, no Docker, no separate infrastructure). The output JSON shape is unchanged; only the path from URL to JSON gets stronger.

The bar for "improvement" is: **every change must be backed by a verified failure of v1 that the change measurably fixes.** Speculative changes are out.

## 2. Verified failure modes in v1

Empirically observed today on the running v1 system:

| Failure | Cause | Today's example |
|---|---|---|
| HTTP 400 / 403 / 422 from `httpx` | Server rejects on TLS fingerprint or other deep bot signal | douglas.nl (400), decathlon.fr (403), decathlon.de (403), fnac.fr (422 via Jina, timeout via httpx) |
| Cloudflare interstitial returned as 200 | Server returns 200 OK with a 2 KB challenge body — v1 happily scans it and finds zero carriers | douglas.nl with chrome131 impersonation returned 200 + 2 KB |
| One impersonation works where another fails | DataDome / Akamai fingerprint specific TLS profiles | decathlon.fr: chrome131 → 403, **safari17_0 → 200 + 608 KB real content** |
| First-time scan of a bot-blocked shop | No cached fallback; user sees cryptic error | Any first-time bot-blocked URL |
| Sub-page discovery yields too few candidates | Homepage anchor-stem scoring misses pages | douglas.nl: anchor scan returns 0 candidates |
| User cannot distinguish error types | Single "Scan failed" string regardless of cause | DNS, 4xx, timeout, and "0-carriers-but-pages-reached" all look the same in v1 |

## 3. Architecture

### 3.1 Fetch cascade (per URL)

The cascade must track *why* each tier failed so the caller can produce a correctly-classified error response (§3.7). A response gets a label:

- `OK` — passed status check and `is_real_content()` gate
- `BLOCKED` — 4xx response OR 2xx-but-failed-content-gate (likely interstitial)
- `TIMEOUT` — request timed out
- `SERVER_ERR` — 5xx response (server side issue, not bot block)
- `NETWORK` — TLS error, connection refused, etc.

```text
fetch_real_content(url) -> (content_html, source_tag, links, fail_labels) | None

  fail_labels = []   # accumulates labels per failed attempt for error classification

  Step A — local, TLS-impersonated:
    for impersonate in ['chrome131', 'safari17_0']:
      try:
        r = curl_cffi.get(url, impersonate=impersonate, timeout=10,
                          headers={'accept-language': locale_for_url(url)},
                          follow_redirects=True, max_redirects=10)
      except TimeoutError:    fail_labels.append('TIMEOUT'); continue
      except (ConnectError, SSLError, ...): fail_labels.append('NETWORK'); continue

      if 500 <= r.status_code < 600:        fail_labels.append('SERVER_ERR'); continue
      if not (200 <= r.status_code < 300):  fail_labels.append('BLOCKED'); continue
      if not is_real_content(r.text):       fail_labels.append('BLOCKED'); continue
      return (r.text, 'local', None, fail_labels)

  Step B — Firecrawl fallback:
    try:
      r = firecrawl.scrape(url, formats=['html', 'links'])
    except RateLimitError: fail_labels.append('FC_RATE'); return None, fail_labels
    except FirecrawlError: fail_labels.append('FC_FAIL'); return None, fail_labels

    if r.html and is_real_content(r.html):
      return (r.html, 'firecrawl', r.links, fail_labels)
    fail_labels.append('BLOCKED')
    return None, fail_labels
```

`curl_cffi`'s default redirect cap is 30; we lower it to 10 to bound runaway redirect chains.

**Order rationale:** chrome131 first (most common UA, fastest happy-path), safari17_0 second (catches the DataDome-style detection that fingerprints chrome specifically — verified on decathlon.fr). The two impersonations cost an extra ~2s in the worst case where the first one passes the status check but fails the content gate.

**`fail_labels` consumption:** When `fetch_real_content` returns `None`, the caller derives the error code from the labels (checked in order; first match wins):

1. Any `NETWORK` label whose underlying exception is a DNS resolution error → `DNS_FAIL`
2. Any `SERVER_ERR` label → `UNREACHABLE` (don't blame the shop for bot-blocking when its server is broken)
3. Any `FC_RATE` label → `BOT_BLOCKED` with `error_detail` = "Firecrawl rate limit reached; try again in a few minutes"
4. All labels are `TIMEOUT` → `TIMEOUT`
5. Any `BLOCKED` label → `BOT_BLOCKED`
6. Else → `UNREACHABLE`

### 3.2 Content-quality gate (`is_real_content`)

A response passes the gate iff **all** are true:

- `len(html) >= 10_000` bytes.
- None of these substrings appear in the first 5 KB of `html`:
  - `Just a moment` (Cloudflare)
  - `Checking your browser` (older Cloudflare)
  - `cf-challenge` (Cloudflare element id)
  - `Attention Required! | Cloudflare`
  - `enable JavaScript and cookies` (Cloudflare "you have been blocked")
  - `Access denied` (generic block page)
  - `Pardon Our Interruption` (DataDome)
  - `Please verify you are a human` (PerimeterX / HUMAN)
  - `verify you are not a bot` (various)

Threshold 10 KB chosen because:

- Verified interstitials in this session were 2–5 KB.
- Smallest real shop homepage in our test set (hema.nl) was 526 KB.
- Smallest real shipping sub-page seen was 19 KB (Firecrawl on decathlon.fr).
- 10 KB is a conservative floor that catches all observed interstitials and excludes no observed real content.

Marker list is intentionally short (high-precision) to avoid rejecting legitimate help-articles that happen to contain "Access denied" in a quoted error message. If implementation reveals false rejections, narrow the substring checks rather than expand the list.

### 3.3 Per-TLD locale headers

Use `tldextract` (already a transitive dep via wappalyzer) to get the effective TLD, then map. Multi-segment TLDs like `.co.uk` are handled correctly because tldextract resolves them.

```python
LOCALE_BY_TLD = {
  'nl':    'nl-NL,nl;q=0.9,en;q=0.8',
  'de':    'de-DE,de;q=0.9,en;q=0.8',
  'fr':    'fr-FR,fr;q=0.9,en;q=0.8',
  'it':    'it-IT,it;q=0.9,en;q=0.8',
  'es':    'es-ES,es;q=0.9,en;q=0.8',
  'be':    'nl-BE,fr-BE;q=0.9,nl;q=0.8,fr;q=0.7',
  'co.uk': 'en-GB,en;q=0.9',
  'uk':    'en-GB,en;q=0.9',
}
LOCALE_DEFAULT = 'en-US,en;q=0.9'

def locale_for_url(url):
    ext = tldextract.extract(url)
    return LOCALE_BY_TLD.get(ext.suffix, LOCALE_DEFAULT)
```

`tldextract.extract('https://www.amazon.co.uk').suffix == 'co.uk'` — verified by the library's existing usage in wappalyzer. Default applies for any TLD not listed. Native-language Accept-Language increases the chance the server serves localized content, which often has more carrier text.

### 3.4 Discovery

```text
discover_candidates(homepage_html, source_tag, firecrawl_links=None) -> list[url]

  1. Anchor-stem scoring (existing logic from v1):
     - parse anchors with BeautifulSoup
     - score = stem-hits-in-href + stem-hits-in-anchor-text
     - keep top 5 same-origin candidates

  2. Sitemap discovery:
     - GET /sitemap.xml; parse with namespace wildcard
     - on sitemap-index, fetch each child sitemap (capped at 5 children)
     - filter <loc> entries through stem scoring; keep top 5

  3. /robots.txt scan:
     - GET /robots.txt; extract every "Sitemap:" URL
     - dedupe vs step 2's sitemap; add to candidates if new

  4. Firecrawl links augmentation (only when source_tag == 'firecrawl'):
     - filter the .links array by stem-scoring
     - take top 5

  5. Always include /

  6. Deduplicate by path.
     - Cap at 8 (v1 default) when homepage source_tag == 'local'  — free fetches
     - Cap at 6 when source_tag == 'firecrawl'                    — credit-discipline
```

The cap difference reflects that Firecrawl-mode pays per-page and the marginal-page-yield drops after the top-stem-scored 6. Local-mode keeps v1's 8 because the fetches are free.

### 3.5 Per-candidate matching

```
match_carriers(content, source_tag) → dict[carrier_name → meta]

  If source_tag == 'local':
    # Full Wappalyzer pipeline works on curl_cffi HTML
    techs = wappalyzer.analyze(url=u, scan_type='fast')[u_key]
    + text-pattern matcher on the same HTML (catches Wappalyzer's `text`-field gap)

  If source_tag == 'firecrawl':
    # Wappalyzer.analyze_from_response() returns empty on Firecrawl-cleaned HTML
    # (verified empirically). Use text-pattern matcher only.
    text-pattern matcher only
```

Both paths use the same `WAPPALYZER_SHIPPING_CATS` category filter and the same `tech_db` subset (cats 99 + 107 + implies/requires referenced techs).

### 3.6 Cache fallback

When `fetch_real_content()` returns `None` for the homepage:

```sql
SELECT result_json, scanned_at FROM scans
 WHERE shop = ?
   AND json_array_length(json_extract(result_json, '$.carriers')) > 0
 ORDER BY id DESC
 LIMIT 1
```

SQLite JSON1 functions (`json_extract`, `json_array_length`) ship with Python's stdlib `sqlite3` since SQLite 3.38 (Python 3.11+). Our target is Python 3.12+, so JSON1 is guaranteed available.

If a row is returned, parse the JSON, add `cached: true` and `cached_at: <scanned_at>` fields to the carriers payload, and return as the result. Frontend renders a "Cached from N days ago" badge in place of the duration meter.

If no cached row exists, return the structured error per §3.7.

### 3.7 Error taxonomy

Error response shape:

```json
{
  "shop": "<url>",
  "duration_s": 0.0,
  "pages_scanned": [],
  "carriers": [],
  "error": "<code from table below>",
  "error_detail": "<human-readable description>"
}
```

Codes:

| `error` code | When raised | Frontend friendly copy |
| --- | --- | --- |
| `DNS_FAIL` | DNS resolution failed (no IP for hostname) | "We couldn't find that domain. Check the spelling." |
| `BOT_BLOCKED` | All fetch attempts produced 4xx or interstitial-flagged content | "This shop blocks automated scans. We weren't able to read its pages." |
| `TIMEOUT` | All fetch attempts timed out before producing content | "The shop didn't respond in time. Try again later." |
| `UNREACHABLE` | Other network failure (TLS error, connection refused, etc.) | "We couldn't reach the shop." |
| `INVALID_URL` | URL fails scheme validation | "URL must start with http:// or https://" |

Frontend uses `error` (the code string) to pick copy + visual state; `error_detail` is shown as monospace technical detail below the friendly copy. The outer card carries the `data-error` attribute (preserved from v1 for test contract).

## 4. Frontend changes

### 4.1 New states

In addition to v1's "loading" / "success" / "error", add:

- **Success-with-fallback:** rendered when `cached: true` is in the response. Same carrier cards as success state, but with a badge `cached from N days ago` next to the meta line, in muted color.
- **Per-carrier source indicator:** each signal chip gains a hover tooltip with the source tag (`local` / `firecrawl`). Visual: identical chip, only the hover string changes. No new visible chrome.

### 4.2 Error card redesign

The single "Scan failed" card splits into one of the five copies in §3.7. Same `data-error` attribute (preserves test contract). Underneath the friendly copy, technical detail in monospace muted text.

### 4.3 Disabled changes

Drop, per audit findings:
- ❌ "Verified" badge / cross-validation visualization (no second source we trust enough to use as ground truth)
- ❌ "Model-extracted" carrier indicator (Firecrawl LLM extract dropped from architecture)

## 5. Components NOT in this design (and why)

| Rejected component | Reason |
|---|---|
| Firecrawl JSON / LLM extract | Empirically: 6 false positives + 4 false negatives across 5 control shops; evidence quotes are synthesized, not verbatim. Adding it as a tier would inject hallucinated carriers. |
| Cross-validation between local and LLM | No second source we trust as ground truth — both layers would introduce errors a coin-flip could resolve. |
| Jina Reader | Tested with 7 parameter combinations; results inconsistent (douglas.nl returned empty markdown then 50 KB on retry — non-determinism). Firecrawl is more reliable for the same role and the user already has the API key. |
| Playwright fallback | Verified that SPA shops (Shopify Hydrogen, allbirds, gymshark, aboutyou) all server-side-render plenty of HTML. SPAs aren't the gap; bot detection is. |
| Confidence-model rewrite (provenance scoring) | The "Bring" false-positive that triggered this concern turned out to be from a too-permissive verification regex, not from Wappalyzer's actual pattern (`\b(?<!-)UPS\b`-style word boundaries are precise). |
| Retry with exponential backoff | Multi-impersonation already gives us two attempts. Time is better spent escalating to Firecrawl than retrying the same impersonation. |
| Cookie-session persistence across pages | Not observed to fix anything in our test set; adds complexity. Defer until a failure shop demonstrates need. |

## 6. Cost model

| Scenario | Firecrawl credits used |
|---|---|
| Shop accessible via local cascade (most shops) | 0 |
| Shop blocked locally, Firecrawl works on homepage + 5 candidates | ~6 |
| Shop blocked everywhere, falls back to cache | 0–1 (one Firecrawl attempt before fallback) |
| Shop where Firecrawl `map` is invoked (only when discovery yields <3 candidates) | +5 (one map call) |

Assume v2 runs ~100 scans/week initially. Maximum credit usage upper-bounded by `100 × 7 = 700` credits/week (worst-case all bot-blocked). Realistic estimate based on today's failure rate (~30%): `30 × 6 + 70 × 0 = ~180` credits/week.

## 7. Backwards compatibility

- **JSON output shape:** structure unchanged; new optional fields added (`cached`, `cached_at`, `error_detail`). Existing v1 fields (`shop`, `duration_s`, `pages_scanned`, `carriers`, `error`) all present.
- **Semantic change in `error` field:** v1 wrote a human-readable string here (e.g., `"shop unreachable: HTTP 400"`). v2 writes a stable code from §3.7 (`"BOT_BLOCKED"`) and moves the human string to `error_detail`. A v1 frontend running against v2 backend would show `BOT_BLOCKED` as the visible error message — functionally OK, visually ugly. Frontend update (Slice 5) ships with backend update; no mixed-version exposure.
- **sqlite schema:** unchanged. `scans(id, shop, scanned_at, result_json)` — new fields added inside `result_json` payload, not as new columns.
- **Existing CUT-block in main.py:** preserved (Wappalyzer-next bugs still apply). New CUT entries appended.
- **Frontend `data-carrier` and `data-error`:** preserved (test hooks).

## 8. Implementation order

Vertical slices, each independently testable. After each slice, re-run the v1 acceptance gates (M1–M5 + final-acceptance fixtures) to confirm no regression.

Gates below are framed as **classes of behavior** rather than specific shops, because bot-detection state shifts day-to-day. The reference shops named are *examples of the behavior class*, swappable for any shop in the same class at implementation time.

1. **Slice 1 — curl_cffi multi-impersonation + content gate.** Replace `httpx.Client` usage in `_discover_sitemap`, `_discover_anchors`, `_fetch_html`; add `is_real_content()` validator + `fail_labels` accumulator. **Gate:** (a) bever.nl baseline still returns PostNL × 4 multi-page signals (no regression on accessible shops); (b) on a shop where v1's chrome131 receives 403, v2's safari17_0 tier produces 2xx + real content (≥10 KB body, no interstitial markers) and detection runs through. Reference: decathlon.fr today; substitute equivalent if it shifts. (c) interstitial-disguised-as-200 is detected: a response with 200 + 2 KB body is rejected by `is_real_content()`.
2. **Slice 2 — Firecrawl scrape fallback.** Add `firecrawl-py` import (env-loaded API key), add Step B of `fetch_real_content()`, wire text-only matching path for Firecrawl content. **Gate:** under a temporary debug flag that force-disables the local cascade, scanning a known-bot-blocked shop returns 2xx + real-content HTML from Firecrawl, and the text-matcher extracts ≥1 carrier from at least one of the discovered candidate pages. Remove the debug flag after gate verification.
3. **Slice 3 — Discovery augmentation.** Add `/robots.txt` sitemap discovery, sitemap-index recursion, Firecrawl-links augmentation. **Gate:** on a shop whose homepage has fewer than 3 stem-matching anchors (today's example: douglas.nl, but the gate is the *class* not the shop), v2 discovery yields ≥3 candidates after augmentation. If douglas.nl no longer exhibits this property at implementation time, pick another shop in the class.
4. **Slice 4 — Cache fallback + error taxonomy.** Implement `cached: true` path; structured error codes per §3.7. **Gate:** (a) `https://does-not-exist-xxxxx.invalid` returns `error: DNS_FAIL`; (b) a 4xx-only shop with no prior cache returns `BOT_BLOCKED`; (c) the same shop, after we manually seed sqlite with a successful prior scan, returns the cached carriers with `cached: true` and `cached_at: <iso>` set.
5. **Slice 5 — Frontend updates.** Cache badge, error-card copies per §3.7, source-indicator tooltips. **Gate:** Playwright drives the form for each of the five `error` codes (using deliberately-induced inputs) and screenshots show 5 visually distinct error states; the cached badge is visible after the seeded-cache scenario; M3/M4 v1 acceptance gates still pass (bever.nl real flow + invalid-domain failure flow).

Total budget: ~10 hours. No slice longer than 2.5 hours.

## 9. Known limitations (acknowledged in product, not papered over)

- Shops where **both** local impersonations AND Firecrawl fail (fnac.fr today) → honest error, no recovery.
- Shops that reach successfully but never mention carriers on accessible pages (aboutyou.nl) → honest 0-carriers result.
- First-time scan of a bot-blocked shop with no cache → honest error.
- Firecrawl rate-limit or outage → cascade degrades to local-only; bot-blocked shops fail until Firecrawl returns. We surface this in the error_detail.
- v2 is still a single-shot scanner — no scheduled re-scans, no proactive cache refresh. Carriers change over time; we don't track that.

## 10. Open questions

Non-blocking, but worth surfacing for honesty:

1. **Firecrawl fire-rate after multi-impersonation.** I verified one chrome→safari escalation works on decathlon.fr. Unknown what fraction of bot-blocked shops are unlocked by safari17_0 alone vs still need Firecrawl. After Slice 1 ships, we can measure this empirically on a 10-shop bot-blocked panel and decide whether to expand the impersonation list or accept the Firecrawl spend.
2. **10 KB content threshold tuning.** Verified safe on homepages (smallest observed real homepage was 526 KB). May reject legitimate sub-pages that are genuinely small (e.g., a 5 KB returns-policy page). The interstitial-marker check is the precision layer; the size threshold is a backup. If implementation reveals false rejections on sub-pages, narrow the size check to homepage-only and rely on markers for sub-pages.
3. **Wappalyzer-on-Firecrawl-HTML evidence is n=1.** Verified that `analyze_from_response()` returns `[]` on Firecrawl's HTML for decathlon.fr's `/modes-livraison` page. I haven't tested whether this is systematic (Firecrawl strips Wappalyzer's DOM patterns) or page-specific (DOM patterns happen not to match on that particular page). Implementation should probe ≥3 Firecrawl pages and confirm before committing to text-only matching path.
4. **Cache staleness policy.** A cached scan from 90 days ago may be wrong (the shop switched carriers). v2 displays "cached from N days" but doesn't expire. Out of scope for this slice; track for v3.
5. **Concurrent-scan rate limit on Firecrawl.** API plan limits are unknown to me; first heavy-use day may surprise us. Mitigation already in §3.1 fail-label handling (`FC_RATE` → `BOT_BLOCKED` with detail).
6. **decathlon.fr behavior may be DataDome-fingerprint-list update away from breaking again.** Today's `safari17_0 → 200` could be `safari17_0 → 403` next week. Real ongoing risk: external bot detection is adversarial. Mitigation: the cascade has Firecrawl as a deeper tier; if local impersonations stop working broadly, Firecrawl spend goes up but the system keeps working.
