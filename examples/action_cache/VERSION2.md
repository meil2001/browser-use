# Action cache — Version 2

Version 1 shipped the full pipeline: hook at execute time, compile to
`test_automation_cached.json`, repair loop, coverage report, and four live sites with
measured Run 1 vs Run 2 numbers ([METRICS.md](METRICS.md)).

Version 2 is a **revision pass** on that same architecture — not a rewrite. Every change is
scoped to `browser_use/action_cache.py`, tests, docs, and recompiled automations. The repair
loop, hook contract, and “no invented locators” rule are unchanged.

**Measured results for V2:** [METRICS_V2.md](METRICS_V2.md)  
**V1 baseline:** [METRICS.md](METRICS.md)

---

## Why V2 exists

An adversarial review of V1 found real gaps:

| Issue | Risk |
|---|---|
| Password fields logged plaintext while docs claimed redaction | Documentation honesty / demo grep |
| Stage 3 kept every click unconditionally | “Good vs bad step” was input-only |
| Gap nodes always appended at the end | Wrong replay order when a **middle** step failed |
| Locator ladder jumped to xpath when `data-test` / clean labels existed | Brittle automations on site 4 |

V2 fixes these without changing where the hook fires, how the reviewer reads the raw log, or
how Run 2 executes Playwright commands.

---

## Phase 1 — Password redaction and honest docs

**Problem:** Saucedemo logs contained `secret_sauce` in `text` and `after.target.value` while
`DESIGN.md` implied passwords were never stored.

**What changed:**

- `is_sensitive_field()` — detects password fields (`type=password`, label/name/id).
- `redact_sensitive_record()` — writes `text: null` and redacts probe values before the row
  is stored or sent to the reviewer digest.
- `is_on_task()` — password rows still pass Stage 3 (locator kept; value comes from
  `input_parameters`).
- `emit_cached_automation()` — binds `{password[0]}` via label/role when log text is redacted.
- `DESIGN.md` limitation #8 corrected; committed saucedemo `action_cache.jsonl` scrubbed.

**What did not change:** filtering rules for normal inputs, dedupe, repair loop, locator ladder.

**Tests:** `TestSensitiveFields` in `tests/ci/test_action_cache.py`.

---

## Phase 2 — Locator ladder (`data-test` / role before xpath)

**Problem:** `stable_identity` went `name → id → placeholder → xpath`, skipping `data-test`,
`data-testid`, `data-cy`, `data-qa`, and clean link labels already present in hook
`attributes`.

**What changed:**

- `stable_identity` — test hooks (`data-testid`, `data-test`, …) before xpath.
- `resolve_identity()` — at **compile time**, re-reads `attributes` from log rows so old
  traces upgrade without re-running sites.
- `playwright_command()` — emits `get_by_test_id`, `[data-test=…]`, `get_by_role`, `get_by_label`
  when identity allows.
- Site 4 product click: `xpath …/a[5]` → `[data-test="product-…"]` (same click, stronger handle).

**What did not change:** hook capture, Stage 3 value filter, effect probe, loop logic.

**Evidence:** all four `automations/*/test_automation_cached.json` recompiled with `xpath=0`
on locator nodes (gap nodes still have no command).

---

## Phase 3 — Gap node insertion order

**Problem:** `emit_cached_automation` appended gap nodes **after** all cached locators. If step
2 of a four-step task failed, step 4’s locator could run before the gap — wrong order at replay.

**What changed:**

- When step review is available (`summary.available`), nodes are built by walking
  `review.steps` in task order:
  - **covered / unverifiable** with evidence → matching located row
  - **missing click** → `step_gap_node` at that position
  - **missing input** → value gap at that position (same contract as before)
- Unclaimed located rows still append in slice order (extras the reviewer did not map).
- If step review is unavailable → legacy path (all located, then gaps at end).

**What did not change:** gap node contract (`skip_command: true`, no invented locator), reviewer
(still reads full raw `action_cache.jsonl`).

**Tests:** `TestGapNodeOrder` — gap sits between two covered click steps.

---

## Phase 4 — Stage 3 click filtering

**Problem:** `is_on_task()` returned `True` for every click. Only typed inputs were filtered
against the task value list.

**What changed:**

- When step review is available, **clicks** are kept only if their raw trace index appears as
  `evidence` on a step with verdict `covered` or `unverifiable`.
- **Fail-open** when review did not run, cited zero trace rows, or `align_cache` reads a
  different file than `cache_path()` (no misaligned index filtering).
- Reviewer still reads the **full** raw log; filtering applies only to `action_cache_aligned.jsonl`.

**Supporting fix:** `best_write_per_input_text()` always keeps sensitive input rows so redacted
passwords are not dropped in Stage 4 value-dedupe.

**Tests:** `TestStage3OnTaskFilter` — credited click kept, exploratory click dropped, empty
review keeps all clicks.

---

## V2 verification (Aug 22, 2026)

All four sites were re-run from a clean `action_cache.jsonl` with the same loop parameters as
V1. No code changes during the retest window.

| Site | Folder | V1 campaign tokens | V2 campaign tokens | Final automation |
|---|---|---|---|---|
| Roboform | `run_logs/roboform` | 68,473 | 68,574 | 4 nodes, 0 gaps — **byte-identical** cached JSON |
| Books to Scrape | `run_logs/books_toscrape` | 324,920 | 239,282 | 3 nodes (2 + gap) — same intentional stop |
| Saucedemo | `run_logs/saucedemo` | 199,138 | 72,748 | 5 nodes, 0 gaps — complete in pass 0 |
| Toolshop | `run_logs/toolshop` | 104,148 | 116,601 | 4 nodes, 0 gaps — complete after 1 repair |

Full tables, log line references, and Run 2 wall times: **[METRICS_V2.md](METRICS_V2.md)**.

Archives of pre-retest artifacts:
`examples/action_cache/run_logs/<site>/archive_before_retest_20260822/` (V1-era `loop.log` in
each). Older local notes may label these folders `site1`…`site4`; the repo uses the site names
above.

---

## What V2 deliberately did not change

- Hook still records only `click` and `input` (audit documents the rest).
- Repair loop invariant: replay prefix with **no LLM**, one broken step at a time.
- Books site still ends with a prompt-only gap — task step is genuinely uncachable.
- Run 2 still uses zero LLM tokens on every replayable node (`skip_prompt: true`).

---

## Files touched in V2

| Area | Files |
|---|---|
| Core | `browser_use/action_cache.py` |
| Tests | `tests/ci/test_action_cache.py` (74 tests) |
| Docs | `DESIGN.md`, `VERSION2.md`, `METRICS_V2.md` |
| Automations | `examples/action_cache/automations/*/test_automation_cached.json` |
| Evidence | Recompiled `run_logs/*` pipeline JSONL + coverage (where synced) |

---

## Reproduce V2 metrics

From the `browser-use` repo root (same flags as V1):

```bash
python examples/action_cache/run_repair_loop.py \
  --automation <path-to-test_automation.json> \
  --run-dir <path-to-run-dir> \
  --max-iterations 2 \
  --first-run-max-steps 15
```

Then time Run 2 replay (from repo root):

```bash
cd examples/action_cache
PYTHONPATH=../../ python -c "
import asyncio, json
from pathlib import Path
from run_repair_loop import verify_replay
async def main():
    a = json.loads(Path('run_logs/roboform/test_automation_cached.json').read_text())
    r = await verify_replay(a)
    print(r)
asyncio.run(main())
"
```
