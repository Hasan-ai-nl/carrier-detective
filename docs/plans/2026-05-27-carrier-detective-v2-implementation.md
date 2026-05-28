# Carrier Detective v2 Implementation Plan

> **For agentic workers:** Implement task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> **Verification model:** This codebase follows the spike-spirit "gate-based" QA from v1 (see `context/TASK.md`). Each slice ends with a verification gate (Playwright + curl + log inspection), not unit tests. **No git in this repo** — skip commit steps.

**Goal:** Harden v1's `detect(url)` with TLS-impersonated fetching, Firecrawl fallback, augmented discovery, cached failover, and a structured error taxonomy — all in the existing single Python file + single HTML file.

**Architecture:** Three-tier fetch cascade (`curl_cffi[chrome131]` → `curl_cffi[safari17_0]` → Firecrawl scrape) with a content-quality gate per attempt and `fail_labels` tracking for accurate error classification. Same Wappalyzer + text-pattern matcher consumes whatever content tier wins. Sqlite acts as both the existing scan history and a cache-fallback layer when all fetch tiers fail.

**Tech Stack:** Python 3.14, FastAPI, curl_cffi (TLS impersonation), firecrawl-py (paid bot bypass), wappalyzer-next 2.0 (existing carrier DB), BeautifulSoup, tldextract, sqlite3 (stdlib).

**Spec:** [`docs/specs/2026-05-27-carrier-detective-v2-design.md`](../specs/2026-05-27-carrier-detective-v2-design.md) — every task references its corresponding spec section.

---

## File Structure

| File | Status | Responsibility |
| --- | --- | --- |
| `main.py` | Modified extensively | All backend logic: `detect()`, fetch cascade, discovery, matchers, FastAPI endpoints, sqlite persistence + cache fallback. Single file per spike constraint. |
| `index.html` | Modified | Frontend rendering: scan form, carrier cards, 5 error states, cache badge. |
| `.env` | Read-only | Contains `api_key=<firecrawl-api-key>` already provisioned. Code loads it manually (no python-dotenv dependency). |
| `scans.db` | Runtime artifact | Sqlite cache. Schema unchanged from v1: `scans(id, shop, scanned_at, result_json)`. New fields stored inside `result_json`, not as new columns. |
| `docs/specs/...` | Already written | Spec source of truth. |

The `detect()` function in `main.py` grows from one big function to roughly: `_fetch_real_content()` (cascade), `_is_real_content()` (gate), `_classify_error()` (label → code), `_discover_candidates()` (augmented), `_match_carriers_local()` and `_match_carriers_firecrawl()` (split by source), plus `_cache_lookup()` and `_cache_seed()` helpers. All inline in `main.py`, no new files.

---

## Slice 1 — TLS multi-impersonation + content gate

**Spec sections:** §3.1, §3.2, §3.3

### Task 1.1: Replace httpx imports with curl_cffi + add tldextract import

**Files:**

- Modify: `main.py` (top of file, imports block)

- [ ] **Step 1: Verify the curl_cffi exception class path before writing imports**

Run:

```bash
cd "c:/Users/hkodm/Documents/VSC-Projects/_TAG/Carrier Detective" && .venv/Scripts/python.exe -c "
import curl_cffi.requests as r
print('module:', r.__name__)
# Probe for exception classes — names may vary by version
for n in ['RequestException', 'RequestsError', 'Timeout', 'TimeoutException', 'ConnectionError', 'ConnectError']:
    print(f'  has {n}:', hasattr(r, n))
import curl_cffi
print('top-level errors module:', [x for x in dir(curl_cffi) if 'err' in x.lower() or 'exc' in x.lower()])
"
```

Use the actual class names that exist. If only `RequestsError` exists, import that. If broad inspection shows the exceptions live under `curl_cffi.requests.errors`, adjust the path.

- [ ] **Step 2: Update imports based on the probe**

Find the existing import block (currently at top of `main.py` after the START comment and CUT block):

```python
import httpx
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from wappalyzer import analyze
```

Replace with (using whatever exception classes Step 1 revealed; the example below assumes the common `requests`-compatible names):

```python
import tldextract
from curl_cffi import requests as cc_requests
# Catch broadly: curl_cffi exception class hierarchy varies by version. We
# inspect the error message in _fetch_real_content() to classify (DNS vs
# network vs timeout), so a single catch is sufficient.
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from wappalyzer import analyze
```

We keep `httpx` removed entirely — every HTTP fetch in v2 uses `curl_cffi`. `tldextract` was already installed transitively via wappalyzer; explicit import surfaces the intent.

- [ ] **Step 3: Verify imports load**

Run: `cd "c:/Users/hkodm/Documents/VSC-Projects/_TAG/Carrier Detective" && .venv/Scripts/python.exe -c "import main; print('ok')"`
Expected: `[init] tech_db filtered to 85 techs (82 carriers + 3 referenced)` then `ok`. No ImportError.

### Task 1.2: Add `LOCALE_BY_TLD` constant + `locale_for_url()` helper

**Files:**

- Modify: `main.py` (after `WAPPALYZER_SHIPPING_CATS`, before `DB_PATH`)

- [ ] **Step 1: Insert locale constants and helper**

After the line `WAPPALYZER_SHIPPING_CATS = {"Shipping carriers", "Fulfilment"}`, add:

```python
# Per-TLD Accept-Language. Native locale increases the chance the server
# serves localized content with more carrier text.
LOCALE_BY_TLD = {
    "nl":    "nl-NL,nl;q=0.9,en;q=0.8",
    "de":    "de-DE,de;q=0.9,en;q=0.8",
    "fr":    "fr-FR,fr;q=0.9,en;q=0.8",
    "it":    "it-IT,it;q=0.9,en;q=0.8",
    "es":    "es-ES,es;q=0.9,en;q=0.8",
    "be":    "nl-BE,fr-BE;q=0.9,nl;q=0.8,fr;q=0.7",
    "co.uk": "en-GB,en;q=0.9",
    "uk":    "en-GB,en;q=0.9",
}
LOCALE_DEFAULT = "en-US,en;q=0.9"


def locale_for_url(url: str) -> str:
    """Return Accept-Language header value based on URL TLD."""
    try:
        ext = tldextract.extract(url)
    except Exception:
        return LOCALE_DEFAULT
    return LOCALE_BY_TLD.get(ext.suffix, LOCALE_DEFAULT)
```

- [ ] **Step 2: Verify with quick probe**

Run:

```bash
cd "c:/Users/hkodm/Documents/VSC-Projects/_TAG/Carrier Detective" && .venv/Scripts/python.exe -c "
from main import locale_for_url
for u in ('https://www.bever.nl', 'https://www.amazon.co.uk', 'https://www.example.com', 'https://www.decathlon.fr'):
    print(u, '->', locale_for_url(u))
"
```

Expected output (after init line):

```text
https://www.bever.nl -> nl-NL,nl;q=0.9,en;q=0.8
https://www.amazon.co.uk -> en-GB,en;q=0.9
https://www.example.com -> en-US,en;q=0.9
https://www.decathlon.fr -> fr-FR,fr;q=0.9,en;q=0.8
```

### Task 1.3: Add `is_real_content()` content-quality gate

**Files:**

- Modify: `main.py` (after `locale_for_url`)

- [ ] **Step 1: Insert content gate**

```python
INTERSTITIAL_MARKERS = (
    "Just a moment",                       # Cloudflare
    "Checking your browser",               # older Cloudflare
    "cf-challenge",                        # Cloudflare element id
    "Attention Required! | Cloudflare",
    "enable JavaScript and cookies",       # Cloudflare blocked-page
    "Access denied",                       # generic
    "Pardon Our Interruption",             # DataDome
    "Please verify you are a human",       # PerimeterX / HUMAN
    "verify you are not a bot",            # various
)
MIN_REAL_CONTENT_BYTES = 10_000


def is_real_content(html: str) -> bool:
    """True iff the response body looks like actual page content,
    not a bot-challenge / interstitial / blocked page."""
    if not html or len(html) < MIN_REAL_CONTENT_BYTES:
        return False
    head = html[:5000]
    for marker in INTERSTITIAL_MARKERS:
        if marker in head:
            return False
    return True
```

- [ ] **Step 2: Quick verify**

Run:

```bash
cd "c:/Users/hkodm/Documents/VSC-Projects/_TAG/Carrier Detective" && .venv/Scripts/python.exe -c "
from main import is_real_content
print('empty:', is_real_content(''))
print('tiny:', is_real_content('x' * 100))
print('big-real:', is_real_content('<html><body>' + 'lorem ipsum ' * 2000 + '</body></html>'))
print('big-with-cf:', is_real_content('Just a moment...' + 'x' * 20000))
"
```

Expected:

```text
empty: False
tiny: False
big-real: True
big-with-cf: False
```

### Task 1.4: Add `_fetch_real_content()` cascade (local tiers only — Firecrawl in Slice 2)

**Files:**

- Modify: `main.py` (replace existing `_http_client()` and `_fetch_html()` functions; insert new cascade)

- [ ] **Step 1: Remove obsolete httpx helpers**

Delete the existing `_http_client()` function (the one that returns `httpx.Client(...)`) and the existing `_fetch_html()` function (used by `_wappalyzer_one`). They are replaced wholesale.

- [ ] **Step 2: Insert cascade with fail-label tracking**

```python
# Fail labels accumulated per attempt for downstream error classification.
# OK is implicit (return-path). The rest map to error codes via _classify_error().
LABEL_BLOCKED    = "BLOCKED"     # 4xx OR 2xx-but-failed-content-gate
LABEL_TIMEOUT    = "TIMEOUT"
LABEL_SERVER_ERR = "SERVER_ERR"  # 5xx
LABEL_NETWORK    = "NETWORK"     # TLS/connection error
LABEL_DNS        = "DNS"         # subset of NETWORK
LABEL_FC_RATE    = "FC_RATE"     # Firecrawl rate-limited
LABEL_FC_FAIL    = "FC_FAIL"     # Firecrawl other error

CURL_IMPERSONATIONS = ("chrome131", "safari17_0")
CURL_TIMEOUT = 10


def _fetch_real_content(url: str) -> tuple[str | None, str | None, list[str] | None, list[str]]:
    """Try local TLS-impersonated fetch tiers. Returns (content_html, source_tag, fc_links, fail_labels).

    On success: content_html is real HTML, source_tag is 'local' or (Slice 2) 'firecrawl'.
    On failure: content_html is None; fail_labels lists per-attempt failure reasons.
    """
    fail_labels: list[str] = []

    # Step A — curl_cffi local impersonations
    for impersonate in CURL_IMPERSONATIONS:
        try:
            # curl_cffi.requests is requests-API-compatible: allow_redirects is honored,
            # max_redirects is set at session level (not per-request) in some versions.
            # Verify max_redirects support during Task 1.1 probe and adjust if needed.
            r = cc_requests.get(
                url,
                impersonate=impersonate,
                timeout=CURL_TIMEOUT,
                headers={"accept-language": locale_for_url(url)},
                allow_redirects=True,
            )
        except Exception as e:
            # Single broad catch — curl_cffi exception class hierarchy varies by version.
            # Classify by the exception message instead of by class name.
            msg = str(e).lower()
            tname = type(e).__name__
            if "timeout" in msg or "timed out" in tname.lower():
                fail_labels.append(LABEL_TIMEOUT)
                print(f"[fetch] {url} [{impersonate}] -> TIMEOUT")
                continue
            if "getaddrinfo" in msg or "name or service not known" in msg or "could not resolve host" in msg:
                fail_labels.append(LABEL_DNS)
                print(f"[fetch] {url} [{impersonate}] -> DNS")
                # DNS failure won't be fixed by next impersonation; short-circuit.
                return None, None, None, fail_labels
            fail_labels.append(LABEL_NETWORK)
            print(f"[fetch] {url} [{impersonate}] -> NETWORK ({tname}: {msg[:80]})")
            continue

        status = r.status_code
        body = r.text or ""
        if 500 <= status < 600:
            fail_labels.append(LABEL_SERVER_ERR)
            print(f"[fetch] {url} [{impersonate}] -> {status} SERVER_ERR")
            continue
        if not (200 <= status < 300):
            fail_labels.append(LABEL_BLOCKED)
            print(f"[fetch] {url} [{impersonate}] -> {status} BLOCKED")
            continue
        if not is_real_content(body):
            fail_labels.append(LABEL_BLOCKED)
            print(f"[fetch] {url} [{impersonate}] -> 200 but {len(body)//1024}KB interstitial (BLOCKED)")
            continue

        print(f"[fetch] {url} [{impersonate}] -> 200 OK ({len(body)//1024}KB real)")
        return body, "local", None, fail_labels

    # Step B — Firecrawl fallback. Implemented in Slice 2.
    # For now, exhaust here so Slice 1 can ship without Firecrawl dependency.
    return None, None, None, fail_labels
```

- [ ] **Step 3: Verify Slice-1 cascade behaviour on three classes of shop**

Run:

```bash
cd "c:/Users/hkodm/Documents/VSC-Projects/_TAG/Carrier Detective" && .venv/Scripts/python.exe -c "
from main import _fetch_real_content
for url in ('https://www.bever.nl', 'https://www.decathlon.fr', 'https://does-not-exist-9j2k3l.invalid'):
    body, src, _, labels = _fetch_real_content(url)
    print(f'  {url:50} -> src={src!r}  body_size={len(body or \"\")//1024}KB  labels={labels}')
"
```

Expected (one of these per shop):

- `bever.nl` → `src='local'`, `body_size` > 100 KB, `labels=[]` (chrome131 succeeded)
- `decathlon.fr` → `src='local'`, `body_size` > 500 KB, `labels=['BLOCKED']` (chrome131 blocked, safari17_0 succeeded)
- `does-not-exist-9j2k3l.invalid` → `src=None`, `body_size=0`, `labels=['DNS']` (short-circuit on DNS)

### Task 1.5: Wire detect() to use the new cascade

**Files:**

- Modify: `main.py` (`detect()` function and `_wappalyzer_one()`)

- [ ] **Step 1: Update `_wappalyzer_one` signature to accept (url, content, source_tag)**

Replace the existing `_wappalyzer_one(url)` with:

```python
def _scan_candidate(url: str, content: str, source_tag: str) -> tuple[str, dict, float]:
    """Run carrier detection on already-fetched content.
    For source='local'  -> Wappalyzer (DOM + scriptSrc + etc) + our text-pattern layer.
    For source='firecrawl' -> text-pattern layer ONLY (Wappalyzer DOM doesn't match FC HTML).
    """
    t0 = time.time()
    page_techs: dict = {}

    if source_tag == "local":
        # Wappalyzer needs to fetch its own copy (it doesn't accept pre-fetched HTML),
        # so we re-invoke analyze() — its internal cache + our timeout patch keep it fast.
        try:
            results = analyze(url=url, scan_type="fast", timeout=CURL_TIMEOUT)
        except Exception as e:
            print(f"[wappalyzer] {url} -> ERROR {e}")
            results = {}
        for _k, techs in (results or {}).items():
            if isinstance(techs, dict):
                page_techs = techs
                break

    # Always run text matcher on the content we already have (regardless of source).
    page_techs.update(_text_match_carriers(content, page_techs))

    dt = time.time() - t0
    if page_techs:
        names = ", ".join(sorted(page_techs.keys()))
    else:
        names = "0 hits"
    print(f"[scan/{source_tag}] {url} -> {names} (took {dt:.1f}s)")
    return url, page_techs, dt
```

Delete the old `_wappalyzer_one` and `_fetch_html` functions.

- [ ] **Step 2: Update `_discover_sitemap` and `_discover_anchors` to take pre-fetched content**

The old versions took an `httpx.Client` and fetched themselves. They now take the homepage HTML directly (already fetched by `_fetch_real_content`). Sitemap discovery still needs to fetch a separate URL — give it the curl_cffi session.

Replace both functions:

```python
def _curl_get(url: str, impersonate: str = "chrome131", timeout: int = 8) -> tuple[int, str]:
    """Single curl_cffi GET, returns (status, body). Caller handles labeling.
    Used for sitemap.xml / robots.txt — auxiliary fetches not in the main cascade."""
    try:
        r = cc_requests.get(
            url,
            impersonate=impersonate,
            timeout=timeout,
            headers={"accept-language": locale_for_url(url)},
            allow_redirects=True,
        )
        return r.status_code, (r.text or "")
    except Exception as e:
        print(f"[curl_get] {url} -> error: {type(e).__name__}")
        return -1, ""


def _discover_sitemap(base: str) -> list[str]:
    """Fetch sitemap.xml (or sitemap-index entries), return stem-scored top-5 URLs.
    Single-attempt with chrome131 — sitemap.xml is rarely bot-protected."""
    sm_url = base.rstrip("/") + "/sitemap.xml"
    status, body = _curl_get(sm_url, impersonate="chrome131", timeout=8)
    if status != 200 or not body:
        print(f"[discover] sitemap {sm_url} -> {status} (skip)")
        return []
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        print(f"[discover] sitemap parse error: {e}")
        return []
    locs: list[str] = []
    for el in root.iter("{*}loc"):
        if el.text:
            locs.append(el.text.strip())
        if len(locs) >= 500:
            break
    # Drop sitemap-index children (those end .xml); recursing would blow budget.
    locs = [u for u in locs if not u.lower().endswith(".xml")]
    scored = [(u, _score_path(urlparse(u).path)) for u in locs]
    scored = [(u, s) for u, s in scored if s > 0]
    scored.sort(key=lambda x: x[1], reverse=True)
    top = [u for u, _ in scored[:5]]
    print(f"[discover] sitemap -> {len(locs)} candidates, kept top {len(top)}")
    return top


def _discover_anchors_from_html(base: str, homepage_html: str) -> list[str]:
    """Score anchors in the already-fetched homepage HTML. Returns top-5 same-origin URLs."""
    soup = BeautifulSoup(homepage_html, "html.parser")
    origin = urlparse(base)
    candidates: list[tuple[str, int]] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = (a.get_text() or "").strip()
        abs_url = urljoin(base, href)
        u = urlparse(abs_url)
        if u.scheme not in ("http", "https"):
            continue
        if u.netloc and u.netloc != origin.netloc:
            continue
        path_text = (u.path or "/").lower()
        anchor_text = text.lower()
        score = sum(1 for s in STEMS if s in path_text) + sum(1 for s in STEMS if s in anchor_text)
        if score > 0:
            candidates.append((abs_url, score))
    candidates.sort(key=lambda x: x[1], reverse=True)
    seen_paths: set[str] = set()
    kept: list[str] = []
    for u, _ in candidates:
        p = urlparse(u).path or "/"
        if p in seen_paths:
            continue
        seen_paths.add(p)
        kept.append(u)
        if len(kept) >= 5:
            break
    print(f"[discover] anchors -> {len(candidates)} candidates -> {len(kept)} kept")
    return kept
```

- [ ] **Step 3: Add `_classify_error()` helper**

Insert before `detect()`:

```python
def _classify_error(fail_labels: list[str]) -> tuple[str, str]:
    """Map accumulated fail labels to (error_code, error_detail). See spec §3.7."""
    if LABEL_DNS in fail_labels:
        return "DNS_FAIL", "DNS resolution failed for that hostname"
    if LABEL_SERVER_ERR in fail_labels:
        return "UNREACHABLE", "The shop's server returned 5xx — it appears to be having problems"
    if LABEL_FC_RATE in fail_labels:
        return "BOT_BLOCKED", "Firecrawl rate limit reached; try again in a few minutes"
    if fail_labels and all(l == LABEL_TIMEOUT for l in fail_labels):
        return "TIMEOUT", "The shop didn't respond before our timeout"
    if LABEL_BLOCKED in fail_labels:
        return "BOT_BLOCKED", "The shop blocks automated requests; we couldn't read its pages"
    return "UNREACHABLE", "Could not reach the shop (unspecified network failure)"
```

- [ ] **Step 4: Rewrite `detect()` to use the new cascade**

Replace the entire `detect()` body with:

```python
def detect(url: str) -> dict:
    t_start = time.time()
    if not isinstance(url, str) or not url.strip():
        return {"shop": url, "duration_s": 0.0, "pages_scanned": [], "carriers": [],
                "error": "INVALID_URL", "error_detail": "URL is empty"}
    shop = url.strip().rstrip("/")
    if not (shop.startswith("http://") or shop.startswith("https://")):
        return {"shop": shop, "duration_s": 0.0, "pages_scanned": [], "carriers": [],
                "error": "INVALID_URL", "error_detail": "URL must start with http:// or https://"}

    home_html, home_source, _fc_links, fail_labels = _fetch_real_content(shop)
    if home_html is None:
        code, detail = _classify_error(fail_labels)
        # Cache fallback handled in Slice 4.
        return {"shop": shop, "duration_s": round(time.time() - t_start, 2),
                "pages_scanned": [], "carriers": [],
                "error": code, "error_detail": detail}

    sm_urls = _discover_sitemap(shop)
    anchor_urls = _discover_anchors_from_html(shop, home_html)

    # Combine and cap. Slice 1: always cap at 8 (no Firecrawl-mode yet).
    candidates: list[str] = [shop + "/"]
    seen_paths: set[str] = {"/"}
    for u in sm_urls + anchor_urls:
        p = urlparse(u).path or "/"
        if p in seen_paths:
            continue
        seen_paths.add(p)
        candidates.append(u)
        if len(candidates) >= 8:
            break
    print(f"[discover] final -> {[urlparse(u).path or '/' for u in candidates]}")

    # Per-candidate scan. For Slice 1, every candidate is fetched by the local cascade.
    page_results: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=4) as ex:
        def _fetch_and_scan(u: str) -> tuple[str, dict]:
            body, src, _, _ = _fetch_real_content(u)
            if body is None:
                return u, {}
            return _scan_candidate(u, body, src)[:2]
        for u, techs in ex.map(_fetch_and_scan, candidates):
            page_results[u] = techs

    # Filter + aggregate (unchanged from v1).
    by_carrier: dict[str, list[dict]] = {}
    for u, techs in page_results.items():
        path = urlparse(u).path or "/"
        for tech_name, meta in (techs or {}).items():
            cats = set(meta.get("categories") or [])
            if not (cats & WAPPALYZER_SHIPPING_CATS):
                continue
            conf = int(meta.get("confidence") or 0)
            by_carrier.setdefault(tech_name, []).append(
                {"layer": "wappalyzer", "url": path, "confidence": conf}
            )

    carriers: list[dict] = []
    for name, signals in by_carrier.items():
        unique_urls = {s["url"] for s in signals}
        peak = max(s["confidence"] for s in signals)
        score = peak + (15 if len(unique_urls) >= 2 else 0)
        level = "high" if score >= 100 else ("medium" if score >= 60 else "low")
        carriers.append({"name": name, "confidence": level, "signals": signals})
    carriers.sort(key=lambda c: (-max(s["confidence"] for s in c["signals"]), c["name"]))

    return {
        "shop": shop,
        "duration_s": round(time.time() - t_start, 2),
        "pages_scanned": [urlparse(u).path or "/" for u in candidates],
        "carriers": carriers,
    }
```

### 🛑 Gate Slice 1

- [ ] **Verify (a): bever.nl baseline still works (no regression)**

Run:

```bash
cd "c:/Users/hkodm/Documents/VSC-Projects/_TAG/Carrier Detective" && .venv/Scripts/python.exe -c "
import json
from main import detect
print(json.dumps(detect('https://www.bever.nl'), indent=2))
"
```

Expected: completes <30s, returns ≥1 carrier (PostNL), confidence "high", at least one carrier has ≥2 signals. Same as v1's M1 gate fixture swap.

- [ ] **Verify (b): a shop that v1 fails on with chrome131-403 now succeeds via safari17_0**

Run:

```bash
cd "c:/Users/hkodm/Documents/VSC-Projects/_TAG/Carrier Detective" && .venv/Scripts/python.exe -c "
import json
from main import detect
r = detect('https://www.decathlon.fr')
print(json.dumps({'shop': r.get('shop'), 'error': r.get('error'), 'duration_s': r.get('duration_s'), 'n_carriers': len(r.get('carriers', []))}, indent=2))
"
```

Expected: no `error` field (or `error: None`), at least 1 carrier returned (Mondial Relay/Colissimo/Chronopost — same TLD-class as today's verification). If decathlon.fr's bot detection has shifted at implementation time and safari17_0 also fails, the gate is satisfied by *any* shop where the chrome→safari escalation works; pick a substitute and document it.

- [ ] **Verify (c): interstitial-disguised-as-200 is rejected**

Force the interstitial path by mocking. Run:

```bash
cd "c:/Users/hkodm/Documents/VSC-Projects/_TAG/Carrier Detective" && .venv/Scripts/python.exe -c "
from main import is_real_content
fake_interstitial = '<html><head></head><body><div>Just a moment...</div>' + '<p>x</p>' * 10000 + '</body></html>'
print('20KB-with-Just-a-moment:', is_real_content(fake_interstitial))
fake_tiny = '<html><body>real content</body></html>'
print('tiny-no-marker:', is_real_content(fake_tiny))
"
```

Expected:

```text
20KB-with-Just-a-moment: False
tiny-no-marker: False
```

---

## Slice 2 — Firecrawl scrape fallback

**Spec sections:** §3.1 Step B, §3.5

### Task 2.1: Add .env loader + Firecrawl SDK initialization

**Files:**

- Modify: `main.py` (after the existing imports + monkey-patches block, before `STEMS`)

- [ ] **Step 1: Load .env manually (no python-dotenv dep)**

Insert near top of `main.py`, after the wappalyzer monkey-patches:

```python
# Load .env values into os.environ without echoing them.
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# Firecrawl client (optional; absent → Slice 2 fallback is just skipped)
_FIRECRAWL_KEY = os.environ.get("api_key") or os.environ.get("FIRECRAWL_API_KEY")
_firecrawl_app = None
if _FIRECRAWL_KEY:
    try:
        from firecrawl import FirecrawlApp
        _firecrawl_app = FirecrawlApp(api_key=_FIRECRAWL_KEY)
        print("[init] Firecrawl client ready")
    except Exception as e:
        print(f"[init] Firecrawl unavailable: {e}")
else:
    print("[init] no FIRECRAWL_API_KEY/api_key in env — Firecrawl tier disabled")
```

- [ ] **Step 2: Verify**

Run: `.venv/Scripts/python.exe -c "import main; print('fc_ready =', main._firecrawl_app is not None)"`
Expected: `[init] Firecrawl client ready` then `fc_ready = True`. No traceback.

### Task 2.2: Implement Step B of the cascade

**Files:**

- Modify: `main.py` (`_fetch_real_content`, replace the `# Step B` placeholder)

- [ ] **Step 1: Replace the placeholder Step B with the real Firecrawl tier**

Inside `_fetch_real_content`, replace the comment block `# Step B — Firecrawl fallback. Implemented in Slice 2.` and the `return None, None, None, fail_labels` line that follows it with:

```python
    # Step B — Firecrawl fallback
    if _firecrawl_app is None:
        return None, None, None, fail_labels
    try:
        r = _firecrawl_app.scrape(url, formats=["html", "links"])
    except Exception as e:
        err_str = str(e).lower()
        if "rate" in err_str or "429" in err_str:
            fail_labels.append(LABEL_FC_RATE)
            print(f"[fetch] {url} [firecrawl] -> rate-limited")
        else:
            fail_labels.append(LABEL_FC_FAIL)
            print(f"[fetch] {url} [firecrawl] -> {type(e).__name__}")
        return None, None, None, fail_labels

    fc_html = getattr(r, "html", None) or ""
    fc_links = getattr(r, "links", None) or []
    if not is_real_content(fc_html):
        fail_labels.append(LABEL_BLOCKED)
        print(f"[fetch] {url} [firecrawl] -> {len(fc_html)//1024}KB but blocked/empty")
        return None, None, None, fail_labels
    print(f"[fetch] {url} [firecrawl] -> {len(fc_html)//1024}KB real, {len(fc_links)} links")
    return fc_html, "firecrawl", fc_links, fail_labels
```

### Task 2.3: Update `_scan_candidate()` to match by source

**Files:**

- Modify: `main.py` (`_scan_candidate`)

- [ ] **Step 1: Already split in Slice 1 Task 1.5 Step 1** — verify the existing code from that task includes the `if source_tag == "local"` branch and the always-on text matcher. If yes, no change here.

The Slice 1 code already only invokes Wappalyzer's `analyze()` for `source_tag == "local"`. For `firecrawl`, only `_text_match_carriers()` runs. Good.

- [ ] **Step 2: Per spec open question §10.3, verify Wappalyzer-DOM-on-FC-HTML doesn't help**

Optional but recommended: spend ~15 minutes verifying on ≥3 Firecrawl pages whether `analyze_from_response()` returns anything useful on FC HTML. If on some pages it does add DOM matches, consider adding it as a parallel pass for `firecrawl` source too. If it consistently returns `[]`, current text-only path is correct.

Run:

```bash
cd "c:/Users/hkodm/Documents/VSC-Projects/_TAG/Carrier Detective" && .venv/Scripts/python.exe -c "
from main import _firecrawl_app
from wappalyzer.core.analyzer import analyze_from_response
class FR:
    def __init__(self, html, url): self.text = html; self.url = url; self.headers = {}; self.cookies = type('C',(),{'get_dict': lambda self:{}})()
for u in ('https://www.douglas.nl', 'https://www.decathlon.fr/lp/i/modes-livraison', 'https://www.fnac.fr/help/'):
    try:
        r = _firecrawl_app.scrape(u, formats=['html'])
        html = getattr(r, 'html', '') or ''
        res = analyze_from_response(FR(html, u), 'fast')
        print(f'  {u}: html={len(html)//1024}KB  wapp_dom_hits={list(res.keys())[:5]}')
    except Exception as e:
        print(f'  {u}: ERR {e}')
"
```

Expected: either consistent empty hits (confirms text-only path) or sometimes-populated hits (justifies adding DOM pass; open follow-up ticket but don't block Slice 2).

### 🛑 Gate Slice 2

- [ ] **Verify: Firecrawl tier produces real content + carriers on a shop where local cascade fails**

Run:

```bash
cd "c:/Users/hkodm/Documents/VSC-Projects/_TAG/Carrier Detective" && .venv/Scripts/python.exe -c "
# Temporarily disable the local cascade by removing impersonations.
import main
main.CURL_IMPERSONATIONS = ()  # force Step A to skip
import json
print(json.dumps(main.detect('https://www.decathlon.fr'), indent=2))
"
```

Expected: returns ≥1 carrier (from `/lp/i/modes-livraison` or similar discovered page), `pages_scanned` non-empty, no `error` field. After verifying, re-enable local tier (next subprocess run starts fresh — no permanent change).

---

## Slice 3 — Discovery augmentation

**Spec sections:** §3.4

### Task 3.1: Add /robots.txt sitemap extraction

**Files:**

- Modify: `main.py` (new helper `_discover_robots_sitemaps`, called from `detect()`)

- [ ] **Step 1: Insert helper**

```python
_ROBOTS_SITEMAP_RE = re.compile(r"^Sitemap:\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE)


def _discover_robots_sitemaps(base: str) -> list[str]:
    """Parse /robots.txt for Sitemap: directives, return extracted URLs (deduped vs default sitemap.xml)."""
    robots_url = base.rstrip("/") + "/robots.txt"
    status, body = _curl_get(robots_url, impersonate="chrome131", timeout=5)
    if status != 200 or not body:
        return []
    found = _ROBOTS_SITEMAP_RE.findall(body)
    # Drop the default sitemap.xml (already scanned separately).
    default_sm = base.rstrip("/") + "/sitemap.xml"
    extra = [u.strip() for u in found if u.strip() and u.strip() != default_sm]
    if extra:
        print(f"[discover] robots.txt sitemaps: {extra}")
    return extra
```

Add `import re` if not already present at top of file (it should be — `_ROBOTS_SITEMAP_RE` uses it).

### Task 3.2: Add `_discover_sitemap_url()` for arbitrary sitemap URLs (used by robots-discovered sitemaps)

**Files:**

- Modify: `main.py`

- [ ] **Step 1: Refactor `_discover_sitemap` to call a URL-taking helper**

Replace the `_discover_sitemap` from Task 1.5 with:

```python
def _discover_sitemap_url(sm_url: str) -> list[str]:
    """Fetch a specific sitemap URL, return stem-scored top-5 <loc> entries."""
    status, body = _curl_get(sm_url, impersonate="chrome131", timeout=8)
    if status != 200 or not body:
        print(f"[discover] sitemap {sm_url} -> {status} (skip)")
        return []
    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        print(f"[discover] sitemap parse error: {e}")
        return []
    locs: list[str] = []
    for el in root.iter("{*}loc"):
        if el.text:
            locs.append(el.text.strip())
        if len(locs) >= 500:
            break
    locs = [u for u in locs if not u.lower().endswith(".xml")]
    scored = [(u, _score_path(urlparse(u).path)) for u in locs]
    scored = [(u, s) for u, s in scored if s > 0]
    scored.sort(key=lambda x: x[1], reverse=True)
    top = [u for u, _ in scored[:5]]
    print(f"[discover] sitemap {sm_url} -> {len(locs)} candidates, kept top {len(top)}")
    return top


def _discover_sitemap(base: str) -> list[str]:
    """Default sitemap.xml entry-point."""
    return _discover_sitemap_url(base.rstrip("/") + "/sitemap.xml")
```

### Task 3.3: Augment `detect()` discovery — Firecrawl links + cap differentiation

**Files:**

- Modify: `main.py` (`detect()` discovery block)

- [ ] **Step 1: Update discovery block in `detect()`**

Replace the Slice-1 discovery section with:

```python
    # Discovery: stem-scored anchors + sitemap.xml + robots.txt sitemap-indexed + (when FC) FC links
    sm_urls = _discover_sitemap(shop)
    robots_sms = _discover_robots_sitemaps(shop)
    for sm in robots_sms[:3]:  # cap robots-discovered sitemap recursion
        sm_urls.extend(_discover_sitemap_url(sm))
    anchor_urls = _discover_anchors_from_html(shop, home_html)

    fc_link_urls: list[str] = []
    if home_source == "firecrawl" and _fc_links:
        # Each FC link is a URL string. Score by stem hits.
        scored = [(u, _score_path(urlparse(u).path)) for u in _fc_links if isinstance(u, str)]
        scored = [(u, s) for u, s in scored if s > 0]
        scored.sort(key=lambda x: x[1], reverse=True)
        fc_link_urls = [u for u, _ in scored[:5]]
        print(f"[discover] firecrawl links -> {len(fc_link_urls)} kept")

    # Combine + dedupe by path. Cap based on source.
    cap = 6 if home_source == "firecrawl" else 8
    candidates: list[str] = [shop + "/"]
    seen_paths: set[str] = {"/"}
    for u in sm_urls + anchor_urls + fc_link_urls:
        p = urlparse(u).path or "/"
        if p in seen_paths:
            continue
        seen_paths.add(p)
        candidates.append(u)
        if len(candidates) >= cap:
            break
    print(f"[discover] final ({len(candidates)} of cap={cap}) -> {[urlparse(u).path or '/' for u in candidates]}")
```

Note: this requires `_fetch_real_content` to also return `fc_links`. It already does (the tuple shape was `(content_html, source_tag, fc_links, fail_labels)` from Task 1.4). Wire the rename so `detect()` captures it: the line `home_html, home_source, _fc_links, fail_labels = _fetch_real_content(shop)` should become `home_html, home_source, _fc_links, fail_labels = _fetch_real_content(shop)` (already correct from Slice 1).

### 🛑 Gate Slice 3

- [ ] **Verify: a shop whose homepage anchor scan yields <3 candidates now reaches ≥3 after augmentation**

Run on a shop in the weak-anchor class (today: douglas.nl):

```bash
cd "c:/Users/hkodm/Documents/VSC-Projects/_TAG/Carrier Detective" && .venv/Scripts/python.exe -c "
import json
from main import detect
r = detect('https://www.douglas.nl')
print('pages_scanned count:', len(r.get('pages_scanned', [])))
print('pages:', r.get('pages_scanned', []))
"
```

Expected: `pages_scanned count >= 3`. If douglas.nl is no longer in the weak-anchor class at implementation time, pick another shop with thin homepage nav (e.g., a minimalist Shopify shop) and verify the same property.

---

## Slice 4 — Cache fallback + error taxonomy

**Spec sections:** §3.6, §3.7

### Task 4.1: Add `_cache_lookup()` helper

**Files:**

- Modify: `main.py` (after `_db()` helper)

- [ ] **Step 1: Insert**

```python
def _cache_lookup(shop: str) -> dict | None:
    """Return most recent successful scan for `shop` (carriers non-empty), or None.
    Adds `cached: True` and `cached_at: <iso>` to the returned payload.
    """
    try:
        conn = _db()
        row = conn.execute(
            """
            SELECT result_json, scanned_at
              FROM scans
             WHERE shop = ?
               AND json_array_length(json_extract(result_json, '$.carriers')) > 0
             ORDER BY id DESC
             LIMIT 1
            """,
            (shop,),
        ).fetchone()
        conn.close()
    except Exception as e:
        print(f"[cache] lookup failed: {e}")
        return None
    if not row:
        return None
    try:
        payload = json.loads(row[0])
    except Exception:
        return None
    payload["cached"] = True
    payload["cached_at"] = row[1]
    return payload
```

### Task 4.2: Wire `_cache_lookup()` into `detect()`'s failure path

**Files:**

- Modify: `main.py` (`detect()`, around the `home_html is None` branch)

- [ ] **Step 1: Insert cache-check before error return**

Replace the existing failure block:

```python
    if home_html is None:
        code, detail = _classify_error(fail_labels)
        return {"shop": shop, "duration_s": round(time.time() - t_start, 2),
                "pages_scanned": [], "carriers": [],
                "error": code, "error_detail": detail}
```

with:

```python
    if home_html is None:
        # Before reporting failure, check sqlite for a prior successful scan.
        cached = _cache_lookup(shop)
        if cached is not None:
            cached["duration_s"] = round(time.time() - t_start, 2)
            print(f"[cache] hit for {shop} (scanned_at={cached.get('cached_at')})")
            return cached
        code, detail = _classify_error(fail_labels)
        return {"shop": shop, "duration_s": round(time.time() - t_start, 2),
                "pages_scanned": [], "carriers": [],
                "error": code, "error_detail": detail}
```

### Task 4.3: Make the `error` field always a code (not a free-form string)

**Files:**

- Modify: `main.py` (`detect()` and `scan()` route)

- [ ] **Step 1: Audit existing return-paths for string `error` values, convert to codes**

Search `main.py` for the existing pattern `"error": f"shop unreachable...` etc. and verify all error-paths now use the codes from `_classify_error()` or the INVALID_URL constant. The Slice 1 `detect()` rewrite should already cover this — quick scan confirms.

- [ ] **Step 2: Ensure `scan()` POST handler doesn't double-wrap errors**

The existing `scan()` (FastAPI POST `/scan`) catches `detect()` exceptions and wraps with `{"error": ...}`. Update to use a code:

```python
@app.post("/scan")
async def scan(req: Request):
    try:
        body = await req.json()
    except Exception:
        return JSONResponse({"error": "INVALID_URL", "error_detail": "Invalid JSON body"})
    url = (body or {}).get("url")
    if not url:
        return JSONResponse({"error": "INVALID_URL", "error_detail": "missing 'url' field"})
    try:
        result = detect(url)
    except Exception as e:
        return JSONResponse({"error": "UNREACHABLE", "error_detail": f"detect failed: {e}"})
    # Persist (existing logic — only persist successful or no-error results to keep cache clean)
    if not result.get("error"):
        try:
            from datetime import datetime, timezone
            conn = _db()
            conn.execute(
                "INSERT INTO scans (shop, scanned_at, result_json) VALUES (?, ?, ?)",
                (result.get("shop", url), datetime.now(timezone.utc).isoformat(), json.dumps(result)),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"[db] insert failed: {e}")
    return JSONResponse(result)
```

The key change vs v1: only persist scans without an `error` field (avoids polluting cache with failed lookups).

### 🛑 Gate Slice 4

- [ ] **Verify (a): invalid domain returns DNS_FAIL**

Run:

```bash
cd "c:/Users/hkodm/Documents/VSC-Projects/_TAG/Carrier Detective" && .venv/Scripts/python.exe -c "
import json
from main import detect
print(json.dumps(detect('https://does-not-exist-9j2k3l.invalid'), indent=2))
"
```

Expected: `error: "DNS_FAIL"`, `error_detail` is a friendly string.

- [ ] **Verify (b): cache fallback fires when a previously-successful shop is force-failed**

Run (two steps — seed cache, then force failure):

```bash
cd "c:/Users/hkodm/Documents/VSC-Projects/_TAG/Carrier Detective" && .venv/Scripts/python.exe -c "
import json
from main import detect
# Step 1: real scan to seed cache
r1 = detect('https://www.bever.nl')
print('seeded:', len(r1.get('carriers', [])), 'carriers')
# Step 2: force-fail by setting impersonations to () AND firecrawl_app to None
import main as m
m.CURL_IMPERSONATIONS = ()
m._firecrawl_app = None
r2 = detect('https://www.bever.nl')
print('post-fail cached =', r2.get('cached'), 'cached_at =', r2.get('cached_at'))
print('carriers (from cache):', [c['name'] for c in r2.get('carriers', [])])
"
```

Expected: `seeded: 1 carriers`, `post-fail cached = True`, `carriers (from cache): ['PostNL']`.

---

## Slice 5 — Frontend updates

**Spec sections:** §4

### Task 5.1: Update frontend error rendering to handle 5 error codes

**Files:**

- Modify: `index.html` (the `renderCarriers()` JS function)

- [ ] **Step 1: Replace the error-card block**

Find the existing JS:

```javascript
    if (data.error) {
      const card = document.createElement('div');
      card.className = 'card card-error';
      card.setAttribute('data-error', '');
      card.innerHTML = '<strong>Scan failed.</strong> ' + esc(data.error);
      root.appendChild(card);
      return;
    }
```

Replace with:

```javascript
    if (data.error) {
      const FRIENDLY = {
        DNS_FAIL:    "We couldn't find that domain. Check the spelling.",
        BOT_BLOCKED: "This shop blocks automated scans. We weren't able to read its pages.",
        TIMEOUT:     "The shop didn't respond in time. Try again later.",
        UNREACHABLE: "We couldn't reach the shop.",
        INVALID_URL: "URL must start with http:// or https://"
      };
      const card = document.createElement('div');
      card.className = 'card card-error';
      card.setAttribute('data-error', data.error);
      const friendly = FRIENDLY[data.error] || "Scan failed.";
      const detail = data.error_detail || '';
      card.innerHTML =
        '<strong>' + esc(friendly) + '</strong>' +
        (detail ? '<div class="error-detail">' + esc(detail) + '</div>' : '');
      root.appendChild(card);
      return;
    }
```

- [ ] **Step 2: Add CSS for `.error-detail` (monospace muted technical detail)**

In the `<style>` block, after `.card-error strong { ... }`, add:

```css
  .error-detail {
    margin-top: 6px;
    font-family: var(--mono);
    font-size: 12px;
    color: var(--muted);
  }
```

### Task 5.2: Add "cached from N days ago" badge

**Files:**

- Modify: `index.html` (the `renderMeta()` JS function and CSS)

- [ ] **Step 1: Update `renderMeta()`**

Replace the function body:

```javascript
  function renderMeta(data) {
    const m = document.getElementById('meta');
    if (!data || data.error) { m.innerHTML = ''; return; }
    const pages = (data.pages_scanned || []).map(p => '<code>' + esc(p) + '</code>').join(' · ');
    let cachedHtml = '';
    if (data.cached && data.cached_at) {
      const ts = new Date(data.cached_at);
      const daysAgo = Math.max(0, Math.floor((Date.now() - ts.getTime()) / 86400000));
      const label = daysAgo === 0 ? 'today' : (daysAgo === 1 ? '1 day ago' : daysAgo + ' days ago');
      cachedHtml = '<span class="cached-badge">cached ' + esc(label) + '</span><span class="sep">·</span>';
    }
    m.innerHTML =
      cachedHtml +
      'scanned <code>' + esc(data.shop || '') + '</code>' +
      '<span class="sep">·</span>' +
      esc(data.duration_s) + 's' +
      '<span class="sep">·</span>' +
      pages;
  }
```

- [ ] **Step 2: Add CSS for `.cached-badge`**

In the `<style>` block, near `.meta`:

```css
  .cached-badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 999px;
    background: var(--card);
    border: 1px solid var(--border);
    color: var(--text);
    font-family: var(--mono);
    font-size: 11px;
  }
```

### 🛑 Gate Slice 5

- [ ] **Verify (a): 5 distinct error states render visibly different**

Drive Playwright to capture each error code. Use these inputs:

- `https://does-not-exist-9j2k3l.invalid` → DNS_FAIL
- `https://www.fnac.fr` → (likely) BOT_BLOCKED (multiple-fail-label scenario)
- `not-a-url` → INVALID_URL
- For TIMEOUT and UNREACHABLE: simulate by patching impersonations to a non-existent profile or by pointing at a server returning 5xx (e.g., httpstat.us/500). Acceptable to capture these with controlled fakes.

Take a screenshot per state with absolute path; load via Read; describe each visible card.

Expected: each card shows distinct friendly copy from the FRIENDLY map; technical detail visible in muted monospace below; `data-error` attribute value matches the error code.

- [ ] **Verify (b): cached badge appears after force-failed cached scenario**

```bash
cd "c:/Users/hkodm/Documents/VSC-Projects/_TAG/Carrier Detective" && .venv/Scripts/python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000 &
sleep 3
# 1. real scan to seed cache (UI)
# 2. stop uvicorn, edit a debug flag (or just rely on cache hit via cli) ...
```

Practical approach: scan bever.nl through the UI, then via a one-liner script clear `CURL_IMPERSONATIONS` and `_firecrawl_app`, restart uvicorn, scan bever.nl again, screenshot. Expected: PostNL card renders + `cached today` badge visible in meta line.

- [ ] **Verify (c): v1 acceptance gates still pass**

Re-run v1's M3/M4 Playwright checks against the new frontend:

- Real flow (bever.nl) → 1+ carrier card rendered
- Failure flow (invalid domain) → `data-error="DNS_FAIL"` element present

---

## Final Acceptance — universality re-run

After all 5 slices complete, re-run v1's three-region acceptance fixture with the new cascade:

- [ ] **Run final-acceptance Playwright sequence**

Identical to v1's final-acceptance script in `context/TASK.md`, but with the **expectation that more shops now pass without swap**. Specifically:

- NL slot: jack-wolfskin.nl (already worked in v1) or douglas.nl (new — should now work via Slice 1 if local OR Slice 2 Firecrawl)
- FR slot: decathlon.fr (new — should now work via Slice 1 safari17_0)
- EN slot: suitsupply.com (already worked in v1)

Take screenshots of each result + the M5 history view. Verify all three return ≥1 carrier each, all three under 30s (or under 45s if Slice 2 fired; see spec §9).

- [ ] **Update the in-file CUT block in main.py**

Append a new section to the existing CUT comment block at the top of `main.py`:

```python
# CUT (v2 additions):
# 6) Multi-impersonation curl_cffi cascade (chrome131 → safari17_0) replaces
#    httpx entirely. Closes the bot-block gap on shops that fingerprint chrome
#    specifically (e.g. DataDome on decathlon.fr returned 403 to chrome131,
#    200+608KB to safari17_0).
# 7) Content-quality gate (is_real_content) rejects 200-but-tiny responses
#    that v1 silently mis-scanned as "0 carriers" (Cloudflare interstitials).
# 8) Firecrawl scrape as Tier 2 fallback when local cascade fails. Returns HTML
#    + links array; we run text-pattern matcher on the HTML and augment
#    discovery with the links. analyze_from_response() returns [] on FC HTML
#    (verified n=3) so DOM-pattern matching is skipped in FC mode.
# 9) Cache fallback: when all fetch tiers fail and a prior successful scan
#    exists in sqlite, render that instead with cached:true badge.
# 10) Structured error codes (DNS_FAIL, BOT_BLOCKED, TIMEOUT, UNREACHABLE,
#     INVALID_URL) replace v1's free-form error strings. Frontend renders
#     friendly per-code copy + monospace technical detail.
```

---

## Open follow-ups (track as v3 backlog, NOT in this plan)

- Cache staleness expiry / forced refresh (spec §10.4)
- Empirical Firecrawl fire-rate measurement on a 10-shop bot-blocked panel (spec §10.1)
- Sub-page content gate tuning for legitimately-small pages (spec §10.2)
- Bot-detection landscape monitoring (spec §10.6)
