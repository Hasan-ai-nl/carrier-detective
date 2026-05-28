# Build: Carrier Detective — 1-day GSD build

## Session ops (READ FIRST — overrides global CLAUDE.md + superpowers defaults)

This is a throwaway 8h spike. The following auto-triggering skills/agents are **SUPPRESSED for this session** — they would each violate the "ONE Python file + ONE HTML file / no abstractions / no tests / no review" constraints below:

- All `/gsd-*` skills (any one of them scaffolds planning artifacts that are not in the allowed file set)
- `superpowers:test-driven-development`, `tdd-guide` agent, `superpowers:brainstorming`, `superpowers:writing-plans`, `superpowers:requesting-code-review`, `superpowers:receiving-code-review`, `code-reviewer` / `code-review-and-quality` (spec forbids tests + review passes)
- `superpowers:executing-plans`, `superpowers:subagent-driven-development`, `superpowers:finishing-a-development-branch`, `superpowers:dispatching-parallel-agents` (executing-plans + subagent-driven-development both REQUIRE git-worktrees + finishing-a-development-branch as mandatory sub-skills — which violates the no-git rule below; the M1–M5 milestone structure in this file already IS the plan-execution loop; parallel-agents has no use case since milestones are sequential — M2 wraps M1's function, M4 calls M2's endpoint)
- `ship`, `superpowers:using-git-worktrees`, `agent-skills:git-workflow-and-versioning` (no git in this session)

**Allowed only on triggering event:**

- `agent-skills:debugging-and-error-recovery` OR `superpowers:systematic-debugging` (pick one — they overlap) — only after a gate fails *twice* (see Stop conditions)
- `agent-skills:incremental-implementation` — milestone-by-milestone discipline
- `superpowers:verification-before-completion` — **REQUIRED** before declaring *any* gate PASS. Its iron law ("NO COMPLETION CLAIMS WITHOUT FRESH VERIFICATION EVIDENCE") is the formal version of the Vision gate protocol below. At every 🛑 GATE, invoke this skill explicitly and run its 5-step Gate Function (identify → run → read → verify → claim). Skipping it = lying about gate state.

**Vision gate protocol (NON-NEGOTIABLE).** Taking a screenshot is *not* viewing it. After every `pg.screenshot(path="X.png")` you MUST immediately call the `Read` tool on the absolute path of `X.png` so the image loads into your vision context, then describe in plain text what is actually rendered (layout, colors, content, errors), then judge the gate. A gate decision that does not reference visible details from the loaded image is automatically a fail.

**Time tracking.** Make the first line of `main.py` a literal `# START: <ISO-8601 timestamp>` comment, written at session start. After each gate, compute elapsed = now − START and state it. At 7h elapsed, force-ship whatever passed. At 8h, hard stop.

**Global rule overrides + instruction priority.** This spec's "no tests / no review / no abstractions" *supersedes* the global CLAUDE.md TDD + 80%-coverage + mandatory-code-reviewer rules for this session only. Per `superpowers:using-superpowers`'s own Instruction Priority list (user instructions > superpowers skills > default system prompt), this TASK.md is a **priority-1 user instruction** and overrides any superpowers skill (priority 2) it conflicts with. The suppressions above are not "guidelines" — invoking a suppressed skill mid-session "just to be safe" is itself a discipline violation. The e2e gates ARE the QA.

---

## Goal
A web app where I paste a webshop URL, hit Scan, and within 30s see which delivery carriers it uses with confidence levels. ONE Python file (`main.py`) + ONE HTML file (`index.html`). Nothing else.

## Hard constraints
- **1 day, single session.** 8h hard cap.
- **Zero maintainability.** Throwaway code, will be rewritten. No abstractions, no separation of concerns, no config files, no pyproject, no README beyond a single `uvicorn main:app` line at the top of main.py. Inline SQL, inline carrier list, no type hints unless they help you think.
- **Agentic e2e gates between every milestone.** After each milestone, YOU (the agent) run the code, hit the endpoint with curl, drive the browser with Playwright, take screenshots, and *view* them. Unit tests are nonsense here — they confirm functions return values, not that the app works. Only proceed when the gate command produces the documented pass criteria with your own eyes.

## Stack & install
```
pip install fastapi 'uvicorn[standard]' wappalyzer trafilatura httpx playwright
python -m playwright install chromium
```

Note: if `import wappalyzer` fails, the PyPI ecosystem has two distinct packages with overlapping names. Try the alternative (`pip install python-Wappalyzer` → `from Wappalyzer import Wappalyzer, WebPage`) before redesigning M1. `sqlite3` is stdlib — no install.

## Hardcoded constants (put at the top of main.py)
```python
CARRIERS = {
    "PostNL":          [r"\bPostNL\b", r"\bPost\.nl\b"],
    "DHL":             [r"\bDHL\b"],
    "DPD":             [r"\bDPD\b"],
    "GLS":             [r"\bGLS\b"],
    "UPS":             [r"\bUPS\b"],
    "FedEx":           [r"\bFed[Ee]x\b"],
    "Bpost":           [r"\bbpost\b"],
    "Trunkrs":         [r"\bTrunkrs\b"],
    "Homerr":          [r"\bHomerr\b"],
    "Budbee":          [r"\bBudbee\b"],
    "Red je Pakketje": [r"\bRed je Pakketje\b"],
    "Keen Delivery":   [r"\bKeen Delivery\b"],
    "Mondial Relay":   [r"\bMondial Relay\b"],
    "Colissimo":       [r"\bColissimo\b"],
    "Hermes":          [r"\bHermes\b"],
    "Cycloon":         [r"\bCycloon\b"],
    "ViaTim":          [r"\bViaTim\b"],
    "Sandd":           [r"\bSandd\b"],
}
PATHS = ["/", "/verzending", "/levering", "/bezorging",
         "/retour", "/returns", "/klantenservice",
         "/privacy", "/privacybeleid", "/privacyverklaring"]
```

## Output JSON shape (don't deviate)
```json
{
  "shop": "https://www.jack-wolfskin.nl",
  "duration_s": 12.4,
  "pages_scanned": ["/", "/verzending", "/privacy"],
  "carriers": [
    {"name": "PostNL", "confidence": "high",
     "signals": [
       {"layer": "wappalyzer", "url": "/verzending"},
       {"layer": "privacy_text", "url": "/privacy"}
     ]}
  ]
}
```
Confidence: `high` = privacy_text AND any shipping page; `medium` = shipping page only; `low` = homepage-only OR privacy-only.

---

## Milestones with mandatory verification gates

### M1 — `detect(url) -> dict`
One function in main.py that does: path probing (try HEAD first, **fall back to GET on 405/403** — many shops reject HEAD), Wappalyzer fast-mode scan on each kept page, fetch + trafilatura on privacy pages, regex match against CARRIERS, scoring. Use `httpx.Client(timeout=10, follow_redirects=True, headers={"user-agent": "Mozilla/5.0"})` — no UA + no timeout = hangs and 403s. Return the JSON shape above.

Every layer function MUST log one line to stdout per page it touched, e.g. `[wappalyzer] /verzending → 0 hits` / `[privacy_text] /privacy → PostNL, DHL`. Logs prove the code path executed even when it finds nothing.

**🛑 GATE M1.** Run:
```
python -c "import json; from main import detect; print(json.dumps(detect('https://www.jack-wolfskin.nl'), indent=2))"
```
**Pass (all four):** (1) completes < 60s; (2) stdout contains at least one `[wappalyzer]` line AND at least one `[privacy_text]` line (proves both code paths ran); (3) returns ≥ 1 carrier; (4) at least one returned carrier has ≥ 2 distinct `signals[].layer` values (proves the multi-layer scoring path is exercised). Wappalyzer producing 0 hits is acceptable as long as its log line is present — its tech DB may not include NL/EU couriers. If gate fails twice on the same root cause, drop the offending layer per Stop conditions. Do not start M2.

### M2 — API
Add `POST /scan` (body `{"url":"..."}`) wrapping `detect()`. Add `GET /` serving `index.html` from the same directory.

**🛑 GATE M2.** Terminal 1: `uvicorn main:app`. Terminal 2:
```
curl -sX POST localhost:8000/scan -H 'content-type: application/json' \
  -d '{"url":"https://www.jack-wolfskin.nl"}' | python -m json.tool
```
**Pass:** same JSON shape as M1, same carriers list. Do not start M3.

### M3 — Static frontend (mock data)
Build `index.html`. Inline a sample result object in a `<script>`. Render it. No fetch yet.

**🛑 GATE M3.** Drive Playwright and screenshot both modes:
```python
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width":1280,"height":900})
    pg.goto("http://localhost:8000")
    pg.screenshot(path="m3_light.png", full_page=True)
    pg.emulate_media(color_scheme="dark")
    pg.reload()
    pg.screenshot(path="m3_dark.png", full_page=True)
    b.close()
```
**View both screenshots with your vision.** **Pass:** visible URL input, visible Scan button, visible carrier card (name + confidence pill + signal chips), dark mode reads as intentionally designed (NOT auto-inverted — different bg/border/muted palette), single accent color used only for button + high-confidence pill + focus ring. If it looks like a Bootstrap admin template, throw away the CSS and redo. Do not start M4.

### M4 — Wire frontend to backend
Replace inline mock with `fetch('/scan', {method:'POST', headers:{'content-type':'application/json'}, body: JSON.stringify({url})})`. Loading state: pulsing dot + "Scanning…" in monospace. Errors render inline as a muted card with the message — never `alert()`, never console-only, never blank screen.

**🛑 GATE M4.** Real flow + failure flow:
```python
# real flow
pg.goto("http://localhost:8000")
pg.fill('input', 'https://www.jack-wolfskin.nl')
pg.click('button')
pg.wait_for_selector('[data-carrier]', timeout=60000)
pg.screenshot(path="m4_ok.png", full_page=True)

# failure flow
pg.goto("http://localhost:8000")
pg.fill('input', 'https://does-not-exist-9j2k3l.invalid')
pg.click('button')
pg.wait_for_selector('[data-error]', timeout=30000)
pg.screenshot(path="m4_err.png", full_page=True)
```
**View both screenshots.** **Pass:** `m4_ok.png` shows the carriers you saw via curl in M2, `m4_err.png` shows a human-readable inline error (not a stack trace, not a blank page). Do not start M5.

### M5 — History (only if M1–M4 all green AND time remaining)
Inline `sqlite3` in main.py. One table: `scans(id INTEGER PRIMARY KEY, shop TEXT, scanned_at TEXT, json TEXT)`. Append after each `/scan`. Add `GET /scans` returning last 20 as `[{id, shop, scanned_at, carrier_count}]`. Add `<details>Recent scans</details>` at the bottom of `index.html`.

**🛑 GATE M5.** Scan two different shops via Playwright. Reload. Expand recent-scans. Screenshot.  
**Pass:** both shops visible with timestamps and carrier counts.

---

## Frontend design — strict, no improvisation
- Layout: single column, max-width 720px, centered, 48px between sections, 20px card padding.
- Accent: `#d946ef`. Used ONLY for the Scan button bg, focus ring, and "high" confidence pill bg. Nothing else.
- Confidence pills (solid fill, not bordered): high = `#d946ef`, medium = `#f59e0b`, low = `#6b7280`. White text.
- Radius: 12px cards, 8px inputs/button, 999px pills.
- Fonts: `ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif` body; `ui-monospace, Menlo, monospace` for URLs, paths, log, wordmark. Weights: 400 body, 600 H1. Nothing else.
- Light: bg `#fafafa`, card `#fff`, border `#e5e5e5`, text `#0a0a0a`, muted `#737373`.
- Dark (`@media (prefers-color-scheme: dark)`): bg `#0a0a0a`, card `#141414`, border `#262626`, text `#e5e5e5`, muted `#737373`.
- Wordmark top-left: `carrier·detective` (monospace, muted, lowercase).
- H1: "Which couriers does a webshop actually use?" — 32px, weight 600, line-height 1.2.
- Transitions: 150ms ease-out, only on bg-color and opacity.
- No icon libs. Unicode `·` `→` `✓` only.

## Final acceptance
After M4 (M5 optional). Run Playwright through **three** real shops, screenshot each result, view all three. Primary list:
```
https://www.jack-wolfskin.nl
https://www.coolblue.nl
https://www.bol.com
```
coolblue and bol.com run aggressive anti-bot (Akamai / Cloudflare). If either returns a 403 / bot-challenge page on `httpx` GET — that is not a detection-logic bug, swap it with a shop from the fallback pool below and note the swap in stdout:
```
https://www.bever.nl
https://www.bax-shop.nl
https://www.hema.nl
https://www.suitsupply.com/nl-nl
https://www.decathlon.nl
```
**Pass:** three distinct shops return plausible carrier lists, each scan < 30s, all three screenshots look polished and visually consistent (load each PNG via `Read` and describe before judging — per the Vision gate protocol above). If any one looks ugly, fix the CSS before declaring done.

## Stop conditions
- A gate fails twice → cut scope (e.g. drop the `privacy_text` layer if it's the source of trouble), leave a `# CUT: ...` comment at top of file, move on.
- 8h elapsed → ship whatever passed its gate.
- Do not add anything not in this spec. Do not refactor. Do not add tests. Do not add Docker.
