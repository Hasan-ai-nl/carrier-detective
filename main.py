# START: 2026-05-27T13:42:40Z
# uvicorn main:app --reload
#
# CUT log:
# 1) wappalyzer-next v2.0.0 latency: scan_type='fast' ignores `timeout` param
#    (only used by Playwright backend). Patched get_response to enforce timeout.
# 2) wappalyzer-next v2.0.0 fingerprint coverage gap: analyze_from_response()
#    iterates 12 fingerprint fields (certIssuer, scriptSrc, dom, meta, xhr,
#    html, js, cookies, headers, url, dns, robots) but NOT `text`. 70% (57/82)
#    of carrier techs in cats 99/107 use `text` patterns (e.g. PostNL:
#    '\bPostNL\b'). The remaining 30% (AfterShip, Narvar, Route, ShipStation,
#    Malomo, Bleckmann, Cubyn, Descartes etc. — tracking/post-purchase tools)
#    rely on dom/scriptSrc and are still matched by Wappalyzer's normal flow.
#    Added an in-process text-pattern matcher driven by the same Wappalyzer
#    tech_db — universality preserved (no carrier names hardcoded, all data
#    from Wappalyzer DB filtered by category).
# 3) tech_db filtered in-place to cats 99+107 (~82 techs / 7193 total) to cut
#    per-URL CPU regex cost from ~44s to ~1s. Filter is by Wappalyzer category
#    only, not by carrier name. Plus implies/requires referenced techs
#    (Shopify, WordPress, Cart Functionality) kept to avoid KeyError in
#    create_result() for carriers like Track123, Corso, Planzer, Packlink PRO,
#    Deliverr — none of which appeared in the demo shops but would have
#    crashed on a real Shopify or WordPress shop.
# 4) M1 gate verification fixture: spec named jack-wolfskin.nl. That shop
#    mentions DHL/UPS only on /verzendinformatie/ (intrinsic shop property —
#    not a discovery bug; verified the page is in candidates). So criterion #4
#    (≥2 signals on one carrier) cannot fire on this shop. Verified on
#    bever.nl instead: PostNL × 4 pages, 15.4s. Multi-page aggregation proven.
#
# CUT log (v2 additions):
# 5) httpx removed entirely. Every HTTP fetch now goes through curl_cffi for
#    TLS-impersonation. v1's chrome-only fingerprint left shops that block
#    chrome-class clients (decathlon.fr) unreachable; v2 tries chrome131 then
#    safari17_0 before declaring blocked.
# 6) Content-quality gate (is_real_content): rejects bodies < 10 KB and bodies
#    matching one of 9 interstitial markers (Cloudflare/DataDome/PerimeterX
#    variants). v1 happily scanned a 2 KB Cloudflare interstitial as a "200
#    OK with 0 carriers" silent failure — now correctly labeled BLOCKED.
# 7) Firecrawl scrape() wired as Tier 2 fallback when local cascade fails.
#    Returns HTML + links array. Text-pattern matcher runs on the HTML;
#    Wappalyzer DOM analysis is SKIPPED on FC HTML (analyze_from_response
#    returns [] on FC-cleaned HTML, verified empirically — Firecrawl strips
#    DOM patterns Wappalyzer relies on). FC links augment discovery in FC
#    mode, capped at 5 paths after stem-scoring.
# 8) Cache fallback: when all fetch tiers fail and a prior successful scan
#    exists in sqlite (json_array_length(carriers) > 0), the prior result is
#    returned with `cached: true` + `cached_at: <iso>`. The error fields are
#    stripped — cache-served responses are presented as success states with a
#    badge, not as errors.
# 9) Structured error taxonomy (DNS_FAIL / BOT_BLOCKED / TIMEOUT / UNREACHABLE
#    / INVALID_URL) replaces v1's free-form error strings. error field is now
#    a stable code; error_detail carries the human-readable description.
#    Frontend renders per-code friendly copy + monospace technical detail.
#    DNS-level errors short-circuit the impersonation loop (no point trying
#    safari when the host can't be resolved).
# 10) Performance outlier — jack-wolfskin.nl homepage triggered a 167s
#     wappalyzer.analyze() call on a single 1372KB HTML, blowing the 30s/shop
#     budget (other 4 candidate pages took 6-18s each). Likely a pathological
#     regex-backtrack on one tech_db entry against that specific HTML. Scan
#     still completed and returned DHL+UPS correctly. v3 should add a
#     hard-cap per-URL wappalyzer timeout (currently relies on the lib's
#     internal HTTP timeout, not its in-process CPU budget).
# 11) Discovery augmentation paths (robots.txt sitemap extraction, arbitrary
#     sitemap-URL fetch, FC links scoring, source-aware cap of 6 vs 8) are
#     wired correctly but rarely contribute on today's empirical sample
#     because: (a) most accessible EU shops already return ≥3 stem-matching
#     homepage anchors, (b) most robots-discovered sitemaps are
#     sitemap-indexes (.xml children filtered per spec to bound 30s budget).
#     The class of shops this helps (weak-anchor + flat-sitemap or rich-FC-
#     links) is genuinely uncommon today. Architecture is in place for when
#     it does appear. v3 candidate: opt-in sitemap-index recursion behind a
#     per-shop budget.
import json
import os
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, urljoin

import tldextract
from curl_cffi import requests as cc_requests
# Single broad catch around cc_requests.get(): curl_cffi exception class
# hierarchy varies by version (4.x exports `RequestsError` + top-level
# `CurlError`). We classify by exception message + type-name instead of by
# class.
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from wappalyzer import analyze

# Fix wappalyzer-next bug: `timeout` param is ignored for scan_type='fast' (only
# the Playwright `full` backend honors it). Patch the internal HTTP fetcher so
# fast scans actually time out at WAPP_HTTP_TIMEOUT seconds.
WAPP_HTTP_TIMEOUT = 8
import wappalyzer.core.requester as _wr
import wappalyzer.core.analyzer as _wa
_orig_get_response = _wr.get_response
def _patched_get_response(url, cookie=None, **kwargs):
    kwargs.setdefault("timeout", WAPP_HTTP_TIMEOUT)
    return _orig_get_response(url, cookie, **kwargs)
_wr.get_response = _patched_get_response
_wa.get_response = _patched_get_response

# Fingerprint matching against the full 7193-entry tech_db takes ~44s per URL
# on this box (CPU-bound regex over ~3000 tech entries with html-matching).
# Carrier detection only needs cats 99 + 107 (~82 techs). Filter tech_db in
# place at startup. This is the same category filter the spec already mandates
# (WAPPALYZER_SHIPPING_CATS) applied earlier in the pipeline — no carrier
# names hardcoded, universality preserved. ~88x speedup.
#
# Expand the keep-set to also retain techs referenced by carriers via
# implies/requires (e.g. Track123 requires Shopify, Planzer requires WordPress,
# Corso requires Shopify, Deliverr implies Cart Functionality, Packlink PRO
# implies Shopify). Without these, create_result() would KeyError on
# tech_db['Shopify'] when one of these carriers matches on a Shopify shop.
from wappalyzer.core.config import tech_db as _tech_db, cat_db as _cat_db
_target_cat_ids = {int(cid) for cid, meta in _cat_db.items()
                   if meta.get("name") in {"Shipping carriers", "Fulfilment"}}
_carrier_set = {n for n, td in _tech_db.items()
                if {int(x) for x in (td.get("cats") or [])} & _target_cat_ids}
_keep = set(_carrier_set)
for _n in _carrier_set:
    _td = _tech_db[_n]
    for _k in ("implies", "requires"):
        _v = _td.get(_k)
        if not _v:
            continue
        if isinstance(_v, str):
            _v = [_v]
        for _t in _v:
            _bare = _t.split(r"\;")[0]
            if _bare in _tech_db:
                _keep.add(_bare)
for _k in list(_tech_db.keys()):
    if _k not in _keep:
        del _tech_db[_k]
print(f"[init] tech_db filtered to {len(_tech_db)} techs ({len(_carrier_set)} carriers + {len(_keep)-len(_carrier_set)} referenced)")

# Load .env values into os.environ without echoing them. Stdlib parser; the
# repo's .env uses `api_key=<value>` form (verified during v2 planning).
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path, "r", encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# Firecrawl client (optional; absent -> Step B of fetch cascade is skipped).
_FIRECRAWL_KEY = os.environ.get("api_key") or os.environ.get("FIRECRAWL_API_KEY")
_firecrawl_app = None
if _FIRECRAWL_KEY:
    try:
        from firecrawl import FirecrawlApp
        _firecrawl_app = FirecrawlApp(api_key=_FIRECRAWL_KEY)
        print("[init] Firecrawl client ready")
    except Exception as _e:
        print(f"[init] Firecrawl unavailable: {_e}")
else:
    print("[init] no FIRECRAWL_API_KEY/api_key in env - Firecrawl tier disabled")

# Linguistic primitives for anchor-text scoring during page discovery.
# Covers EN/NL/DE/FR/ES/IT. Adding a new language = append stems. No code change.
# Substring matching (Python `in` operator) - accepts overlap noise like
# "deliver"+"lever" both matching "delivery"; relative scoring is unaffected.
STEMS = (
    "ship", "deliver", "carrier", "courier", "track", "return", "refund",
    "verzend", "lever", "bezorg", "retour", "klantenservice",
    "versand", "lieferung", "ruecksend", "rücksend",
    "livraison", "expedition", "expédition", "renvoi",
    "envio", "envío", "entrega", "devolucion", "devolución",
    "spedizione", "consegna", "reso",
    "privacy", "privacybeleid", "datenschutz",
    "confidentialite", "confidentialité", "privacidad", "policy",
    "terms", "conditions", "voorwaarden", "agb", "cgv",
)
# Cat 93 "Reservations & delivery" is INTENTIONALLY excluded - restaurant
# reservation systems (OpenTable, Resy, BookDinners) live there, not carriers.
WAPPALYZER_SHIPPING_CATS = {"Shipping carriers", "Fulfilment"}

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


# Content-quality gate. A response passes iff body is >= 10 KB AND the first
# 5 KB contains none of the known interstitial markers. Threshold rationale:
# verified interstitials in this session were 2-5 KB; smallest real shop
# homepage was hema.nl at 526 KB; smallest real sub-page seen was 19 KB. The
# marker list is intentionally short (high-precision) to avoid false positives
# on help-articles that quote an error message in their body text.
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


# Fail labels accumulated per fetch attempt for downstream error
# classification by _classify_error(). OK is implicit (return-path).
LABEL_BLOCKED    = "BLOCKED"     # 4xx OR 2xx-but-failed-content-gate
LABEL_TIMEOUT    = "TIMEOUT"
LABEL_SERVER_ERR = "SERVER_ERR"  # 5xx
LABEL_NETWORK    = "NETWORK"     # TLS/connection error
LABEL_DNS        = "DNS"         # subset of NETWORK
LABEL_FC_RATE    = "FC_RATE"     # Firecrawl rate-limited
LABEL_FC_FAIL    = "FC_FAIL"     # Firecrawl other error

CURL_IMPERSONATIONS = ("chrome131", "safari17_0")
CURL_TIMEOUT = 10

DB_PATH = os.path.abspath("scans.db")


def _score_path(path: str) -> int:
    p = path.lower()
    return sum(1 for s in STEMS if s in p)


def _curl_get(url: str, impersonate: str = "chrome131", timeout: int = 8) -> tuple[int, str]:
    """Single curl_cffi GET; returns (status, body). Caller handles labeling.
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


def _fetch_real_content(url: str) -> tuple[str | None, str | None, list[str] | None, list[str]]:
    """Try fetch tiers in cascade. Returns (content_html, source_tag, fc_links, fail_labels).

    On success: content_html is real HTML, source_tag is 'local' or 'firecrawl'.
    On failure: content_html is None; fail_labels lists per-attempt failure reasons.

    Step A: curl_cffi local TLS-impersonations (chrome131 then safari17_0).
    Step B: Firecrawl fallback (wired in Slice 2).
    """
    fail_labels: list[str] = []

    # Step A — curl_cffi local impersonations
    for impersonate in CURL_IMPERSONATIONS:
        try:
            r = cc_requests.get(
                url,
                impersonate=impersonate,
                timeout=CURL_TIMEOUT,
                headers={"accept-language": locale_for_url(url)},
                allow_redirects=True,
            )
        except Exception as e:
            msg = str(e).lower()
            tname = type(e).__name__.lower()
            if "timeout" in msg or "timed out" in tname or "timeout" in tname:
                fail_labels.append(LABEL_TIMEOUT)
                print(f"[fetch] {url} [{impersonate}] -> TIMEOUT")
                continue
            if (
                "getaddrinfo" in msg
                or "name or service not known" in msg
                or "could not resolve host" in msg
                or "couldn't resolve host" in msg
                or "name resolution" in msg
            ):
                fail_labels.append(LABEL_DNS)
                print(f"[fetch] {url} [{impersonate}] -> DNS")
                # DNS won't be fixed by next impersonation; short-circuit.
                return None, None, None, fail_labels
            fail_labels.append(LABEL_NETWORK)
            print(f"[fetch] {url} [{impersonate}] -> NETWORK ({type(e).__name__}: {str(e)[:80]})")
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

    # Step B — Firecrawl fallback.
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
            print(f"[fetch] {url} [firecrawl] -> {type(e).__name__}: {str(e)[:120]}")
        return None, None, None, fail_labels

    fc_html = getattr(r, "html", None) or ""
    fc_links = getattr(r, "links", None) or []
    if not is_real_content(fc_html):
        fail_labels.append(LABEL_BLOCKED)
        print(f"[fetch] {url} [firecrawl] -> {len(fc_html)//1024}KB but blocked/empty")
        return None, None, None, fail_labels
    print(f"[fetch] {url} [firecrawl] -> {len(fc_html)//1024}KB real, {len(fc_links)} links")
    return fc_html, "firecrawl", fc_links, fail_labels


def _classify_error(fail_labels: list[str]) -> tuple[str, str]:
    """Map accumulated fail labels to (error_code, error_detail). See spec §3.7.

    Rule order (first match wins):
      1. DNS → DNS_FAIL (cascade is short-circuited in _fetch_real_content)
      2. SERVER_ERR → UNREACHABLE (shop's server is broken)
      3. FC_RATE → BOT_BLOCKED with rate-limit detail
      4. Both local impersonations timed out (n_timeout >= 2 and >= n_blocked) →
         TIMEOUT, even if Firecrawl downstream appended a BLOCKED label for its
         own response. Local-cascade outcome is what the user actually saw.
      5. Any BLOCKED → BOT_BLOCKED
      6. Any TIMEOUT (single occurrence, e.g. one impersonation timed out and
         the other gave an unrelated error) → TIMEOUT
      7. Default → UNREACHABLE
    """
    if LABEL_DNS in fail_labels:
        return "DNS_FAIL", "DNS resolution failed for that hostname"
    if LABEL_SERVER_ERR in fail_labels:
        return "UNREACHABLE", "The shop's server returned 5xx - it appears to be having problems"
    if LABEL_FC_RATE in fail_labels:
        return "BOT_BLOCKED", "Firecrawl rate limit reached; try again in a few minutes"
    n_timeout = fail_labels.count(LABEL_TIMEOUT)
    n_blocked = fail_labels.count(LABEL_BLOCKED)
    if n_timeout >= 2 and n_timeout >= n_blocked:
        return "TIMEOUT", "The shop didn't respond before our timeout"
    if n_blocked > 0:
        return "BOT_BLOCKED", "The shop blocks automated requests; we couldn't read its pages"
    if n_timeout > 0:
        return "TIMEOUT", "The shop didn't respond before our timeout"
    return "UNREACHABLE", "Could not reach the shop (unspecified network failure)"


_ROBOTS_SITEMAP_RE = re.compile(r"^Sitemap:\s*(\S+)\s*$", re.IGNORECASE | re.MULTILINE)


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
    # Wildcard namespace for sitemaps.org schema 0.9.
    for el in root.iter("{*}loc"):
        if el.text:
            locs.append(el.text.strip())
        if len(locs) >= 500:
            break
    # Drop .xml children (sitemap-index entries). Recursion bounded by caller.
    locs = [u for u in locs if not u.lower().endswith(".xml")]
    scored = [(u, _score_path(urlparse(u).path)) for u in locs]
    scored = [(u, s) for u, s in scored if s > 0]
    scored.sort(key=lambda x: x[1], reverse=True)
    top = [u for u, _ in scored[:5]]
    print(f"[discover] sitemap {sm_url} -> {len(locs)} candidates, kept top {len(top)}")
    return top


def _discover_sitemap(base: str) -> list[str]:
    """Default /sitemap.xml entry-point."""
    return _discover_sitemap_url(base.rstrip("/") + "/sitemap.xml")


def _discover_robots_sitemaps(base: str) -> list[str]:
    """Parse /robots.txt for Sitemap: directives; return non-default sitemap URLs."""
    robots_url = base.rstrip("/") + "/robots.txt"
    status, body = _curl_get(robots_url, impersonate="chrome131", timeout=5)
    if status != 200 or not body:
        return []
    found = _ROBOTS_SITEMAP_RE.findall(body)
    default_sm = base.rstrip("/") + "/sitemap.xml"
    extra = [u.strip() for u in found if u.strip() and u.strip() != default_sm]
    if extra:
        print(f"[discover] robots.txt sitemaps: {extra}")
    return extra


def _discover_anchors_from_html(base: str, homepage_html: str) -> list[str]:
    """Score anchors in already-fetched homepage HTML. Returns top-5 same-origin URLs."""
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
    # Deduplicate by path while preserving order.
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


def _text_match_carriers(html: str, existing: dict) -> dict:
    """Run carrier `text` field regexes against page HTML. wappalyzer-next's
    analyze_from_response skips the `text` field — this layer fixes that bug.
    Uses wappalyzer's own matcher + cat lookup so universality holds (no
    carrier-name hardcoding; all driven by tech_db).
    """
    from wappalyzer.core.matcher import match as _wmatch
    from wappalyzer.core.utils import get_cats_and_groups as _cats_lookup
    found: dict = {}
    for tech_name, td in _tech_db.items():
        if tech_name in existing:
            continue
        text_patterns = td.get("text")
        if not text_patterns:
            continue
        matched, version, confidence = _wmatch(text_patterns, html)
        if matched:
            try:
                cats, groups = _cats_lookup(tech_name)
            except Exception:
                cats, groups = [], []
            found[tech_name] = {
                "version": version or "",
                "confidence": int(confidence or 0),
                "categories": cats,
                "groups": groups,
            }
    return found


def _scan_candidate(url: str, content: str, source_tag: str) -> tuple[str, dict, float]:
    """Run carrier detection on already-fetched content.
    For source='local'  -> Wappalyzer (DOM+scriptSrc+etc.) + our text-pattern layer.
    For source='firecrawl' -> text-pattern layer ONLY (Wappalyzer DOM doesn't match FC HTML).
    """
    t0 = time.time()
    page_techs: dict = {}

    if source_tag == "local":
        # Wappalyzer doesn't accept pre-fetched HTML, so we re-invoke analyze().
        # Its internal cache + our timeout monkey-patch (see CUT note 1) keep it fast.
        try:
            results = analyze(url=url, scan_type="fast", timeout=CURL_TIMEOUT)
        except Exception as e:
            print(f"[scan/local] {url} -> wappalyzer ERROR {type(e).__name__}: {e}")
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

    # Discovery: stem-scored anchors + sitemap.xml + robots.txt sitemaps + (when FC) FC links.
    sm_urls = _discover_sitemap(shop)
    robots_sms = _discover_robots_sitemaps(shop)
    for sm in robots_sms[:3]:  # cap robots-discovered sitemap recursion
        sm_urls.extend(_discover_sitemap_url(sm))
    anchor_urls = _discover_anchors_from_html(shop, home_html)

    fc_link_urls: list[str] = []
    if home_source == "firecrawl" and _fc_links:
        scored = [(u, _score_path(urlparse(u).path)) for u in _fc_links if isinstance(u, str)]
        scored = [(u, s) for u, s in scored if s > 0]
        scored.sort(key=lambda x: x[1], reverse=True)
        fc_link_urls = [u for u, _ in scored[:5]]
        print(f"[discover] firecrawl links -> {len(fc_link_urls)} kept")

    # Combine + dedupe by path. Cap based on source-mode (FC pays per page).
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

    # Spec §3.4 step 5: if we're in Firecrawl mode AND discovery is thin
    # (homepage anchors empty, FC links don't score on stems), invoke FC map
    # with a shipping-related search query. Returns ranked URLs; we filter
    # by stem-match. Costs ~5 credits, only fires in the thin-discovery
    # case so cost stays bounded.
    if (
        home_source == "firecrawl"
        and len(candidates) < 3
        and _firecrawl_app is not None
    ):
        try:
            m = _firecrawl_app.map(shop, search="shipping delivery carrier returns")
            map_links = getattr(m, "links", []) or []
            map_urls: list[str] = []
            for ml in map_links:
                u = getattr(ml, "url", None) or (ml if isinstance(ml, str) else None)
                if not u:
                    continue
                if _score_path(urlparse(u).path) > 0:
                    map_urls.append(u)
            print(f"[discover] firecrawl map -> {len(map_links)} total, {len(map_urls)} stem-matching")
            for u in map_urls:
                p = urlparse(u).path or "/"
                if p in seen_paths:
                    continue
                seen_paths.add(p)
                candidates.append(u)
                if len(candidates) >= cap:
                    break
        except Exception as e:
            print(f"[discover] firecrawl map failed: {type(e).__name__}: {str(e)[:100]}")

    print(f"[discover] final ({len(candidates)} of cap={cap}) -> {[urlparse(u).path or '/' for u in candidates]}")

    # Per-candidate scan. Each candidate goes through the cascade independently;
    # the homepage is re-fetched here (small redundancy vs. simpler control flow).
    page_results: dict[str, dict] = {}

    def _fetch_and_scan(u: str) -> tuple[str, dict]:
        body, src, _, _ = _fetch_real_content(u)
        if body is None or src is None:
            return u, {}
        _, techs, _dt = _scan_candidate(u, body, src)
        return u, techs

    with ThreadPoolExecutor(max_workers=4) as ex:
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

    # Confidence calc (unchanged from v1).
    carriers: list[dict] = []
    for name, signals in by_carrier.items():
        unique_urls = {s["url"] for s in signals}
        peak = max(s["confidence"] for s in signals)
        score = peak + (15 if len(unique_urls) >= 2 else 0)
        if score >= 100:
            level = "high"
        elif score >= 60:
            level = "medium"
        else:
            level = "low"
        carriers.append({"name": name, "confidence": level, "signals": signals})

    # Stable order: highest peak first, then name.
    carriers.sort(key=lambda c: (-max(s["confidence"] for s in c["signals"]), c["name"]))

    return {
        "shop": shop,
        "duration_s": round(time.time() - t_start, 2),
        "pages_scanned": [urlparse(u).path or "/" for u in candidates],
        "carriers": carriers,
    }


# ---- API ----
app = FastAPI()


def _db() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.execute(
        "CREATE TABLE IF NOT EXISTS scans (id INTEGER PRIMARY KEY, shop TEXT, scanned_at TEXT, result_json TEXT)"
    )
    return c


def _cache_lookup(shop: str) -> dict | None:
    """Return most recent successful scan for `shop` (carriers non-empty), or None.
    Adds `cached: True` and `cached_at: <iso>` to the returned payload.
    Uses JSON1 functions (json_extract, json_array_length) - guaranteed in stdlib
    sqlite3 since SQLite 3.38 (Python 3.11+)."""
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
    # Drop any prior `error` fields - cached result is a success
    payload.pop("error", None)
    payload.pop("error_detail", None)
    return payload


@app.get("/")
def root():
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"))


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
        return JSONResponse({"error": "UNREACHABLE", "error_detail": f"detect failed: {type(e).__name__}: {e}"})
    # Persist only successful, non-cache-replay scans. Skipping cache replays
    # avoids pollution: a re-saved cache hit would inherit a fresh scanned_at,
    # making the "cached N days ago" badge perpetually report "cached today"
    # for a shop that's been bot-blocked since the original successful scan.
    if not result.get("error") and not result.get("cached"):
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


@app.get("/scans")
def scans_list():
    try:
        conn = _db()
        rows = conn.execute(
            "SELECT id, shop, scanned_at, result_json FROM scans ORDER BY id DESC LIMIT 20"
        ).fetchall()
        conn.close()
    except Exception as e:
        return JSONResponse({"error": f"db read failed: {e}"})
    out = []
    for rid, shop, ts, raw in rows:
        try:
            r = json.loads(raw)
            carriers = [c["name"] for c in (r.get("carriers") or []) if "name" in c]
        except Exception:
            carriers = []
        out.append({"id": rid, "shop": shop, "scanned_at": ts, "carriers": carriers})
    return JSONResponse(out)


@app.delete("/scans")
def scans_clear():
    try:
        conn = _db()
        conn.execute("DELETE FROM scans")
        conn.commit()
        conn.close()
    except Exception as e:
        return JSONResponse({"error": f"db clear failed: {e}"})
    return JSONResponse({"ok": True})
