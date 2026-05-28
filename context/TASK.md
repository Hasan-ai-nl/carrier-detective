# Build: Carrier Detective — 1-day GSD build (v2 · website-agnostic)

## Session ops (READ FIRST — overrides global CLAUDE.md + superpowers defaults)

This is a throwaway 8h spike. **Universality is a hard constraint, not an aspiration.** The system must detect carriers without a hardcoded carrier-name list, without country-specific path lists, and without per-shop branches. Detection is driven by Wappalyzer's externally-maintained tech DB (~65 techs in cat 99 "Shipping carriers" + ~12 in cat 107 "Fulfilment" — verified at enthec/webappanalyzer; cat 93 "Reservations & delivery" is deliberately excluded as it is restaurant-reservation systems like OpenTable/Resy, not parcel carriers). Path discovery is dynamic (`/sitemap.xml` parse + homepage anchor-text scoring) using a bounded multilingual primitives set. Final acceptance spans 3 language regions — never tested only on NL.

**Skill suppressions for this session** (these would violate the "ONE py + ONE html, no tests, no review" envelope):

- All `/gsd-*` skills (scaffold planning artifacts outside the allowed file set)
- `superpowers:test-driven-development`, `tdd-guide` agent, `superpowers:brainstorming`, `superpowers:writing-plans`, `superpowers:requesting-code-review`, `superpowers:receiving-code-review`, `code-reviewer` / `code-review-and-quality` (spec forbids tests + review passes — gates ARE the QA)
- `superpowers:executing-plans`, `superpowers:subagent-driven-development`, `superpowers:finishing-a-development-branch`, `superpowers:dispatching-parallel-agents` (require git-worktrees / finishing-branch as mandatory sub-skills, conflicting with the no-git rule; the M1–M5 chain already IS the plan; milestones are strictly sequential so parallel-agents has no use case)
- `ship`, `superpowers:using-git-worktrees`, `agent-skills:git-workflow-and-versioning` (no git in this session)

**Allowed only on triggering event:**

- `agent-skills:debugging-and-error-recovery` OR `superpowers:systematic-debugging` (pick one — they overlap) — only after a gate fails *twice* (see Stop conditions)
- `agent-skills:incremental-implementation` — milestone-by-milestone discipline
- `superpowers:verification-before-completion` — **REQUIRED** before declaring *any* gate PASS. Iron law: "NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE". At every 🛑 GATE, invoke this skill explicitly and run its 5-step Gate Function (identify → run → read → verify → claim). Skipping it = lying about gate state.

**Vision gate protocol (NON-NEGOTIABLE).** Taking a screenshot is *not* viewing it. **All screenshots must be written to absolute paths** (use `pg.screenshot(path=os.path.abspath("X.png"))` or precompute `SHOT = os.path.abspath(".")` once). After every screenshot, immediately call the `Read` tool on the absolute PNG path so the image loads into your vision context, describe in plain text what is actually rendered (layout, colors, content, errors), then judge the gate. A gate decision that does not reference visible details from the loaded image is automatically a fail.

**Time tracking.** Make the first line of `main.py` a literal `# START: <ISO-8601 timestamp>` comment, written at session start. After each gate, compute elapsed = now − START and state it. At 7h elapsed, force-ship whatever passed. At 8h, hard stop.

**Global rule overrides + instruction priority.** This spec's "no tests / no review" supersedes the global CLAUDE.md TDD + 80%-coverage + mandatory-code-reviewer rules for this session only. Per `superpowers:using-superpowers`'s Instruction Priority list (user instructions > superpowers skills > default system prompt), this TASK.md is a **priority-1 user instruction**. Suppressions are not "guidelines" — invoking a suppressed skill mid-session "just to be safe" is itself a discipline violation.

---

## Goal

A web app where you paste a webshop URL, hit Scan, and within 30s see which delivery carriers it uses with confidence levels — **for any shop in any country, in any major European language**, with NO hardcoded carrier list in `main.py` and NO country-specific path list. ONE Python file (`main.py`) + ONE HTML file (`index.html`). Nothing else as *source*. Runtime artifacts (screenshots, sqlite db, `__pycache__`) are expected.

## Hard constraints

- **1 day, single session, 8h hard cap.**
- **Website-agnostic by architecture** — not "we tested on lots of shops". The two unavoidable bounded literals in `main.py` are:
  - (a) A `STEMS` tuple of ~30 multilingual keyword stems for anchor-text scoring (EN/NL/DE/FR/ES/IT). Linguistic primitives, not shop or carrier data. Adding a new language = add stems without code changes.
  - (b) The Wappalyzer category-name filter set `{"Shipping carriers", "Fulfilment"}` (cats 99/107 — published, stable Wappalyzer category identifiers). Cat 93 "Reservations & delivery" is deliberately excluded — its contents are restaurant-reservation systems (OpenTable, Resy, BookDinners, Quandoo, Yelp Reservations, …) which are not parcel carriers and would produce false positives.
  - Everything else (carrier list, per-shop paths, country-specific logic) is dynamic / externalized to Wappalyzer's auto-updating DB.
- **Zero maintainability for the code itself.** Throwaway, will be rewritten. No classes, no modules, no config files, no pyproject, no README beyond a single `uvicorn main:app` line at top of main.py. Inline SQL. Inline helper functions inside `main.py` are fine (and expected — one per layer).
- **Agentic e2e gates between every milestone.** YOU (the agent) run the code, hit the endpoint with curl, drive Playwright, take screenshots with absolute paths, view them via Read, judge against documented pass criteria. Unit tests are nonsense here — they confirm functions return values, not that the app works. Only proceed when the gate produces the documented pass criteria with your own eyes.

## Stack & install

```bash
pip install fastapi "uvicorn[standard]" wappalyzer httpx beautifulsoup4 playwright
python -m playwright install chromium
```

`trafilatura` was in v1 (privacy-text layer) but is intentionally NOT in v2 — Wappalyzer's fast scan does its own HTML text matching, so a separate text-extractor is dead weight.

**Python version note.** The repo `.venv` ships with Python 3.14 (`.venv\Scripts\pip3.14.exe`). Transitive C-extension deps (`lxml` via beautifulsoup4 alt-parsers, `cryptography` via httpx[http2], `pydantic-core` via fastapi) occasionally lag fresh Python releases — if any wheel is missing for 3.14 and source build fails (no MSVC build tools on this Windows host), recreate the venv against Python 3.12 before starting M1. This is install-time risk, not architecture risk.

**Verified package facts (don't waste cycles re-discovering):**

- `wappalyzer` on PyPI = 2.0.0 (s0md3v/wappalyzer-next). This is the **maintained** package. The older `python-Wappalyzer` (chorsley) is **archived** since 2023 — do not fall back to it.
- Library API: `from wappalyzer import analyze; results = analyze(url=u, scan_type='fast', timeout=10)`.
- `scan_type` accepts `'fast'` (1 HTTP request, no JS), `'balanced'` (more HTTP), `'full'` (Playwright + extension, ~30s/page). **For this spec, `scan_type='fast'` is mandatory** — `full` would launch Chromium per page and blow the 30s/shop budget.
- Return shape: `{url: {tech_name: {"version": str, "confidence": int_0_100, "categories": [str, ...], "groups": [...]}}}`. Categories are returned as **human-readable names** (strings like `"Shipping carriers"`), not numeric IDs — this is why the filter set uses names.
- Wappalyzer DB relevant-tech count (verified at enthec/webappanalyzer): ~65 in cat 99 "Shipping carriers" + ~12 in cat 107 "Fulfilment" ≈ ~80 distinct techs covering NL/DE/FR/ES/IT/UK/US/AU (PostNL, DHL, DPD, GLS, UPS, FedEx, Bpost, Trunkrs, Homerr, Budbee, Red je Pakketje, Keen Delivery, Mondial Relay, Colissimo, Hermes, Royal Mail, USPS, Australia Post, Asendia, Chronopost, Correos, Yodel, Whistl, Parcelforce, AfterShip, ShipStation, Narvar, Route, Malomo, etc.). Cat 93 is excluded (restaurant reservations).
- `requiresCategory: [6]` (Ecommerce) gates most carrier fingerprints — they only fire on actual shops, preventing false positives on news sites that mention carrier names.

`sqlite3` is stdlib — no install.

## Core architecture — `detect(url) -> dict`

One function. Inline helpers in main.py. Algorithm:

1. **Normalize input.** Strip trailing `/`, validate scheme (must start with `http://` or `https://`).
2. **Dynamic page discovery** (no hardcoded path list):
   - Try `GET /sitemap.xml` (timeout=8). If 200, parse `<loc>` elements with `xml.etree.ElementTree` (stdlib). **Sitemap XML uses the `http://www.sitemaps.org/schemas/sitemap/0.9` namespace** — use `root.iter('{*}loc')` (wildcard namespace, ET 3.8+) so the lookup works regardless of namespace declaration. If the response is a sitemap-index (`<sitemapindex>` root pointing to child sitemap files), the same `{*}loc` query still works but yields URLs ending in `.xml` — discard those rather than recursing (keeps the 30s budget bounded). Cap entries scanned at first 500 (some shops emit huge sitemaps). Score each URL by counting `STEMS` matches in its path. Keep top 5.
   - Fetch `GET /` (timeout=10). Parse with `BeautifulSoup(html, 'html.parser')`. For each `<a href>`, compute `score = sum(stem in href.lower() for stem in STEMS) + sum(stem in anchor_text.lower() for stem in STEMS)`. Keep top 5 same-origin URLs.
   - Always include `/`. Deduplicate. Cap total at 8.
   - Log: `[discover] sitemap → N candidates`, `[discover] anchors → M candidates → K kept`, `[discover] final → [path1, path2, ...]`.
3. **Per-page Wappalyzer scan (parallel).** Run all candidate URLs concurrently via `concurrent.futures.ThreadPoolExecutor(max_workers=4)` calling `analyze(url=u, scan_type='fast', timeout=10)` per URL. Sequential scans risk blowing the 30s budget (8 × ~3s = 24s healthy, 8 × 10s = 80s worst-case). Parallel keeps worst-case at ~10s for the slowest URL. Wrap each call in `try/except` — log failures but continue. Log per page: `[wappalyzer] <path> → <comma-separated tech names or "0 hits"> (took Xs)`. Known limitation: `wappalyzer.analyze()` does not expose a UA override for `scan_type='fast'`; hardened shops may 403 the Wappalyzer-default UA even though discovery succeeds. This is data ("0 hits"), not a bug — treat as the universality-fixture-swap case.
4. **Filter + aggregate.** Keep only techs whose `categories` list intersects `WAPPALYZER_SHIPPING_CATS = {"Shipping carriers", "Fulfilment"}` (Wappalyzer cats 99 + 107). **Do NOT include cat 93 "Reservations & delivery"** — that category contains restaurant-reservation systems (OpenTable, Resy, Quandoo, BookDinners, Yelp Reservations), not parcel carriers. Including it produces silent false positives on any shop that uses one of those services. Group by carrier name across all pages. For each carrier: collect one signal per page where it was detected: `{"layer": "wappalyzer", "url": <path>, "confidence": <0-100 from Wappalyzer>}`.
5. **Confidence calculation (data-driven, no per-carrier heuristics):**
   - `score = max(per_page_confidences) + (15 if seen_on_2_or_more_pages else 0)`
   - `high` if `score >= 100`, `medium` if `60 <= score < 100`, `low` if `score < 60`.
6. **Return.** No carriers detected ⇒ honest empty `"carriers": []`. **Do NOT add a hardcoded regex fallback layer — that violates the universality constraint and is explicitly forbidden by the Stop conditions.**

### Bounded literals in main.py

```python
# Linguistic primitives for anchor-text scoring during page discovery.
# Covers EN/NL/DE/FR/ES/IT. Adding a new language = append stems. No code change.
# Substring matching (Python `in` operator) — accepts overlap noise like
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
# Cat 93 "Reservations & delivery" is INTENTIONALLY excluded — restaurant
# reservation systems (OpenTable, Resy, BookDinners) live there, not carriers.
WAPPALYZER_SHIPPING_CATS = {"Shipping carriers", "Fulfilment"}
```

This is the ONLY hardcoded data in `main.py`. It is a linguistic-primitive set + a published-category-name set — neither is per-shop, per-carrier, or per-country.

## Output JSON shape (don't deviate)

```json
{
  "shop": "https://www.jack-wolfskin.nl",
  "duration_s": 12.4,
  "pages_scanned": ["/", "/verzending", "/privacy"],
  "carriers": [
    {"name": "PostNL", "confidence": "high",
     "signals": [
       {"layer": "wappalyzer", "url": "/verzending", "confidence": 100},
       {"layer": "wappalyzer", "url": "/privacy",    "confidence": 100}
     ]}
  ]
}
```

---

## Milestones with mandatory verification gates

### M1 — `detect(url) -> dict`

Implement steps 1–6 above in `main.py`. Use `httpx.Client(timeout=10, follow_redirects=True, headers={"user-agent": "Mozilla/5.0"})` for sitemap + homepage fetches via **direct GET** (HEAD returns no body — useless for sitemap XML or HTML parsing; v1's HEAD-then-GET pattern was for path-existence probing and does not apply here). Treat non-2xx on `/sitemap.xml` as "no sitemap, fall back to anchor-only discovery". Non-2xx on `/` itself is fatal — `detect()` should return `{"error": "shop unreachable: <status>", ...}`.

Every inline helper MUST log one line to stdout per URL it touches (see logging template in the architecture section above). Logs prove the code path ran even when it finds nothing.

**🛑 GATE M1.** Run:

```bash
python -c "import json; from main import detect; print(json.dumps(detect('https://www.jack-wolfskin.nl'), indent=2))"
```

**Pass (all four):**

1. Completes < 30s.
2. Stdout contains at least one `[discover]` line AND at least one `[wappalyzer]` line per scanned URL.
3. Returns ≥ 1 carrier, each with a confidence in `{low, medium, high}`.
4. At least one returned carrier has ≥ 2 entries in its `signals[]` array (proves multi-page aggregation runs).

If gate fails twice on the same root cause → invoke `superpowers:systematic-debugging` and fix at root. **Do NOT add a hardcoded regex layer as a fallback — that is a universality violation, not a scope cut.**

Do not start M2.

### M2 — API

Add `POST /scan` (body `{"url":"..."}`) wrapping `detect()`. Catch exceptions, return `{"error": "<message>"}` with HTTP 200 (frontend renders inline). Add `GET /` serving `index.html` from the same directory via `FileResponse`.

**🛑 GATE M2.** Launch uvicorn with `run_in_background=true`. Verify it bound to `:8000` (`curl -s -o /dev/null -w "%{http_code}" localhost:8000` → `200`). Then:

```bash
curl -sX POST localhost:8000/scan -H 'content-type: application/json' \
  -d '{"url":"https://www.jack-wolfskin.nl"}' | python -m json.tool
```

**Pass:** Same JSON shape as M1, same carriers, same confidence levels.

Do not start M3.

### M3 — Static frontend (mock data)

Build `index.html`. Inline a sample result object (matching the JSON shape above, including multi-page signals per carrier) in a `<script>`. Render it. No fetch yet.

**Test hooks (REQUIRED — M4 gate selectors depend on these):**

- Each rendered carrier card MUST carry `data-carrier="<name>"` attribute on its outer element.
- Each inline error message MUST carry `data-error` attribute on its outer element.

**🛑 GATE M3.** Drive Playwright. **Absolute screenshot paths required:**

```python
import os
from playwright.sync_api import sync_playwright
SHOT = os.path.abspath(".")
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width":1280,"height":900})
    pg.goto("http://localhost:8000")
    pg.screenshot(path=f"{SHOT}/m3_light.png", full_page=True)
    pg.emulate_media(color_scheme="dark")
    pg.reload()
    pg.screenshot(path=f"{SHOT}/m3_dark.png", full_page=True)
    b.close()
print(f"{SHOT}/m3_light.png", f"{SHOT}/m3_dark.png")
```

**View both screenshots via Read on their absolute paths.** **Pass:** visible URL input, visible Scan button, visible carrier card (name + confidence pill + per-signal chips showing layer + URL + confidence%), `data-carrier` attribute present in DOM (verify via `pg.query_selector_all('[data-carrier]')`), dark mode reads as intentionally designed (NOT auto-inverted — different bg/border/muted palette), single accent color used only for Scan button bg + high-confidence pill bg + focus ring. If it looks like a Bootstrap admin template, throw away the CSS and redo.

Do not start M4.

### M4 — Wire frontend to backend

Replace inline mock with `fetch('/scan', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({url})})`. Loading state: pulsing dot + "Scanning…" in monospace. Errors render inline as a muted card with the message — never `alert()`, never console-only, never blank screen. If the JSON response contains an `error` key, render the error card; otherwise render carriers.

**🛑 GATE M4.** Real flow + failure flow:

```python
SHOT = os.path.abspath(".")
# real flow
pg.goto("http://localhost:8000")
pg.fill('input', 'https://www.jack-wolfskin.nl')
pg.click('button')
pg.wait_for_selector('[data-carrier]', timeout=60000)
pg.screenshot(path=f"{SHOT}/m4_ok.png", full_page=True)

# failure flow
pg.goto("http://localhost:8000")
pg.fill('input', 'https://does-not-exist-9j2k3l.invalid')
pg.click('button')
pg.wait_for_selector('[data-error]', timeout=30000)
pg.screenshot(path=f"{SHOT}/m4_err.png", full_page=True)
```

**View both screenshots.** **Pass:** `m4_ok.png` shows the carriers you saw via curl in M2, `m4_err.png` shows a human-readable inline error (not a stack trace, not a blank page).

Do not start M5.

### M5 — History (only if M1–M4 all green AND time remaining)

Inline `sqlite3` in main.py. One table: `scans(id INTEGER PRIMARY KEY, shop TEXT, scanned_at TEXT, result_json TEXT)` (use `result_json` — `json` is a SQLite reserved word). Append after each `/scan`. Add `GET /scans` returning last 20 as `[{id, shop, scanned_at, carrier_count}]`. Add `<details>Recent scans</details>` at the bottom of `index.html`.

**🛑 GATE M5.** Scan three shops from **different language regions** via Playwright (re-uses the final-acceptance fixture). Reload. Expand recent-scans. Screenshot with absolute path. View. **Pass:** all three shops visible with timestamps and carrier counts.

---

## Frontend design — strict, no improvisation

- Layout: single column, max-width 720px, centered, 48px between sections, 20px card padding.
- Accent: `#d946ef`. Used ONLY for Scan button bg, focus ring, "high" confidence pill bg.
- Confidence pills (solid fill, not bordered): high = `#d946ef`, medium = `#f59e0b`, low = `#6b7280`. White text.
- Radius: 12px cards, 8px inputs/button, 999px pills.
- Fonts: `ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif` body; `ui-monospace, Menlo, monospace` for URLs, paths, log lines, wordmark, signal chips. Weights: 400 body, 600 H1.
- Light: bg `#fafafa`, card `#fff`, border `#e5e5e5`, text `#0a0a0a`, muted `#737373`.
- Dark (`@media (prefers-color-scheme: dark)`): bg `#0a0a0a`, card `#141414`, border `#262626`, text `#e5e5e5`, muted `#737373`.
- Wordmark top-left: `carrier·detective` (monospace, muted, lowercase).
- H1: "Which couriers does a webshop actually use?" — 32px, weight 600, line-height 1.2.
- Transitions: 150ms ease-out, only on bg-color and opacity.
- No icon libs. Unicode `·` `→` `✓` only.
- Required test hooks: carrier card → `data-carrier="<name>"`; inline error → `data-error`.

## Final acceptance — universality enforcement

After M4 (M5 optional). Run Playwright through **THREE shops from three different language regions** to prove the architecture is genuinely website-agnostic. **Mandatory diversity:** at least one shop each from {NL}, {FR or DE}, {EN (UK/US/AU)}. Primary fixture:

```text
https://www.jack-wolfskin.nl       # NL (mostly accessible — outdoor retail)
https://www.decathlon.fr           # FR (sports retail)
https://www.suitsupply.com         # EN-default multi-lang menswear
```

If any primary shop returns 403 / bot-challenge / Cloudflare interstitial on `httpx` GET — that is not a detection-logic bug. Swap with a shop **from the same language region** (preserves the universality assertion):

```text
NL fallback:    https://www.bever.nl, https://www.hema.nl, https://www.bax-shop.nl
FR/DE fallback: https://www.decathlon.de, https://www.fnac.fr, https://www.aboutyou.de
EN fallback:    https://www.suitsupply.com/en-gb, https://www.uniqlo.com/uk
```

Document any swap in stdout: `[swap] decathlon.fr → fnac.fr (reason: 403)`.

**Pass:**

1. Three distinct shops from three language regions return JSON results.
2. Each scan completes < 30s end-to-end.
3. At least 2 of the 3 shops return ≥ 1 carrier (0 carriers across all 3 = algorithm is broken; 0 on 1 shop = data, not a bug, provided `[wappalyzer]` logs confirm the layer ran on all candidate pages).
4. All three frontend screenshots look polished and visually consistent — load each PNG via `Read` (absolute path) and describe before judging.

If any screenshot is ugly, fix the CSS before declaring done.

## Stop conditions

- A gate fails twice on the same root cause → leave a `# CUT: <root cause + decision>` comment at top of `main.py` and move on. **Acceptable cuts:** relax confidence thresholds; drop the multi-page bonus; drop sitemap discovery (keep only anchor scan); reduce path cap from 8 to 5. **Forbidden cuts (universality violations, not scope cuts):** adding a hardcoded CARRIERS regex dict; adding a hardcoded NL/EU PATHS list; per-shop branches; per-domain heuristics.
- 8h elapsed → ship whatever passed its gate.
- Do not add anything not in this spec. Do not refactor. Do not add tests. Do not add Docker.
