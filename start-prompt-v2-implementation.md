# Start prompt — Carrier Detective v2 implementation

> Open a fresh session and paste:
> **`Read start-prompt-v2-implementation.md and execute the session it describes.`**

---

## Session goal

Implement Carrier Detective v2 — a robustness upgrade to the v1 spike. Five slices, ~10 hours total, single-file constraint preserved. The architecture, gates, and task breakdown are already specified and reviewed; this session is pure execution.

## Operating constants (READ FIRST — these override default skill behaviors)

1. **No git in this repo.** The project directory `c:\Users\hkodm\Documents\VSC-Projects\_TAG\Carrier Detective` is not a git repo. Skip every "commit" step in any executing skill. Use file-level checkpoints instead.
2. **No unit tests.** v1 inherited the spike model where "gates ARE the QA" (verification by curl + Playwright + log inspection). v2 preserves this. The skill `superpowers:test-driven-development` is **suppressed** for this session. Don't author pytest files; don't add a test directory. Each slice ends at a 🛑 Gate; the gate's verification commands are the QA.
3. **Spike-spirit constraints preserved.** Single `main.py` + single `index.html` as the only source files. Runtime artifacts (`scans.db`, screenshots, `__pycache__`) are expected. No Docker, no new modules, no test suite.
4. **`agent-skills:incremental-implementation`** and **`superpowers:verification-before-completion`** are the active skills. The latter is REQUIRED before declaring any 🛑 Gate PASS — invoke its 5-step Gate Function (identify → run → read → verify → claim) at every gate.
5. **Vision gate protocol.** Any Playwright screenshot must be written to an absolute path and then opened via Read so the image enters vision context. Describe what's actually rendered before judging.
6. **8-hour soft cap, fresh-session handoff at hour 7.** This is execution work, not exploration. If a slice is taking 3× its estimated time, stop and capture state in a new handoff file rather than push through.
7. **The `error` field semantic changed in v2** — it's now a stable code (e.g. `BOT_BLOCKED`), not a human string. Existing frontend code expecting a human string in `data.error` is updated as part of Slice 5. Don't ship Slice 5 partial.

## State of dependencies (verified during the planning session)

- **Python 3.14** in `.venv\Scripts\python.exe` — works
- **curl_cffi** installed and verified — `safari17_0` impersonation unblocks decathlon.fr; `chrome131` is fast path
- **firecrawl-py** installed and verified — `.env` contains `api_key=<value>` (already loaded by `main.py` patches you'll write)
- **wappalyzer 2.0** — already in use, monkey-patched in v1 for the timeout bug and the `text`-field gap; both patches must be preserved in v2
- **tldextract** — transitive dep via wappalyzer; available
- **Playwright + Chromium** — already installed and used by v1 acceptance gates

## Required reading (in order)

1. **`context/TASK.md`** — v1 spec. Defines the spike-spirit constraints, skill suppressions, instruction-priority hierarchy. The v2 plan inherits these (with the no-tests rule explicitly re-confirmed).
2. **`docs/specs/2026-05-27-carrier-detective-v2-design.md`** — v2 architecture spec. 10 sections, 4 critical-review passes applied. Read sections 3 (architecture), 7 (backwards-compat), 8 (implementation order), 9 (limitations) carefully; sections 5 (rejected components) and 10 (open questions) are context.
3. **`docs/plans/2026-05-27-carrier-detective-v2-implementation.md`** — the task-by-task plan. 5 slices, ~25 tasks. Each task lists files, code, verification steps. Treat this as authoritative; if you find an inconsistency between plan and spec, the spec wins and the plan should be updated.
4. **`main.py`** — current v1 state. Has the START timestamp, the CUT block documenting wappalyzer-next workarounds, the working `detect()` function. v2 modifies this file extensively but the structure should remain recognizable.
5. **`index.html`** — current v1 state. Vanilla JS frontend with `data-carrier` and `data-error` test hooks. v2 adds error-code variants and the cached badge.

## Pre-flight (run these before starting Slice 1)

```bash
cd "c:/Users/hkodm/Documents/VSC-Projects/_TAG/Carrier Detective"

# Confirm files in place
test -f context/TASK.md && echo "v1 spec ok"
test -f docs/specs/2026-05-27-carrier-detective-v2-design.md && echo "v2 spec ok"
test -f docs/plans/2026-05-27-carrier-detective-v2-implementation.md && echo "v2 plan ok"
test -f main.py && echo "main.py ok"
test -f index.html && echo "index.html ok"
test -f .env && echo ".env ok"

# Confirm venv + deps
.venv/Scripts/python.exe -c "
import curl_cffi.requests as cc
import firecrawl
import tldextract
import wappalyzer
print('all deps importable')
"

# Confirm v1 baseline still works (regression baseline before v2 changes)
.venv/Scripts/python.exe -c "
import json
from main import detect
r = detect('https://www.bever.nl')
assert not r.get('error'), 'v1 baseline broken'
assert len(r.get('carriers', [])) >= 1, 'v1 returns 0 carriers'
print('v1 baseline ok:', [c['name'] for c in r['carriers']])
"
```

If any pre-flight check fails, **stop and diagnose before starting Slice 1**.

## Workflow

For each of the 5 slices in the plan:

1. Invoke `agent-skills:incremental-implementation` (already invoked in v1; declare you're using it).
2. Work through each task in order. Each task has `- [ ]` checkboxes. Mark them as you go (file-level state, not git).
3. **Before declaring any 🛑 Gate PASS:** invoke `superpowers:verification-before-completion` and run its 5-step Gate Function. Skipping = lying about gate state.
4. At every gate, run the exact verification commands the plan specifies. Capture output. If the gate fails twice on the same root cause, leave a `# CUT:` comment in `main.py` documenting what was cut and why (same protocol v1 used).
5. After all 5 slices pass, run the Final Acceptance section (3 shops × 3 regions, plus updated CUT block).

## Critical reminders carried forward from v1 + audit

- **Wappalyzer-next bugs:** the `text`-field gap and the `timeout=` ignored-in-fast-mode bug are real. Don't remove the monkey-patches in `main.py`. The v2 plan instructs preserving them.
- **tech_db filter expansion:** v2 keeps the audit fix that expanded the keep-set from 82 to 85 techs (Shopify, WordPress, Cart Functionality added because some carriers `requires` them). Removing this re-introduces a KeyError on Shopify shops using Track123/Corso/Packlink PRO.
- **Per-TLD locale headers:** use `tldextract.extract(url).suffix` for multi-segment TLDs like `.co.uk`. Don't hand-roll `.endswith()` lookups — that's a documented bug in an earlier spec draft.
- **Confidence model:** unchanged from v1. Score = max(per-page-confidences) + 15 multi-page bonus. Buckets: high ≥ 100, medium ≥ 60, low < 60. Don't be tempted to rewrite this — the spec audit found the "Bring" false-positive concern was a probe-script artifact, not a production issue.
- **Hard-blocked failure modes:** fnac.fr-class shops (timeout on both local AND Firecrawl) are intrinsically unfixable in this architecture. Return the appropriate error code and move on. Don't add retry logic or Playwright fallback (both empirically rejected in spec §5).

## Acceptance criteria (session done when all true)

- [ ] All 5 slices' 🛑 Gates have passed with verification-before-completion evidence
- [ ] Final Acceptance: 3 shops × 3 language regions all return ≥1 carrier (or document the swap)
- [ ] The CUT block in `main.py` has entries 6-10 appended documenting v2 changes
- [ ] `index.html` renders 5 visually-distinct error states
- [ ] No regression on v1 fixtures: bever.nl returns PostNL high-confidence multi-page

## Out-of-scope explicitly

- Unit tests, pytest, test directory
- Git operations (not a repo)
- Cache expiry policy (v3)
- Empirical multi-shop bot-detection panel measurement (v3)
- LLM-based carrier extraction (rejected in spec §5; recall reasons: hallucinated evidence quotes, 6 FP/4 FN across 5 control shops)
- Playwright stealth fallback (rejected in spec §5; SPAs verified to server-side-render enough HTML)

## Cross-references

- ROADMAP equivalent: this is the v2 release of the project. v1 was the throwaway spike (now shipped and running). v3 backlog is captured in spec §10.
- Memory anchors for v1 context: observation IDs 4858-4862 (spec refinements), S880 (post-acceptance audit), S879 (initial architecture).
