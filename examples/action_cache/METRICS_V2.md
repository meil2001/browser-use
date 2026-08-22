# Run 1 vs Run 2 — Version 2 (measured Aug 22, 2026)

Every number comes from a log line or a timed replay. Nothing is estimated.

This document supersedes the **campaign totals and automation shapes** in [METRICS.md](METRICS.md)
for the four-site retest after Phases 1–4. V1 numbers remain valid as the original submission
baseline; V2 numbers reflect the same loop command on the same code after the revision pass.

**Context:** [VERSION2.md](VERSION2.md)  
**Code revision:** Phases 1–4 in `browser_use/action_cache.py` (74 unit tests passing)

---

## How these runs were taken

- **Date:** 22 Aug 2026  
- **Command (all sites):**

```bash
python run_repair_loop.py \
  --automation run_logs/<site>/test_automation.json \
  --run-dir run_logs/<site> \
  --max-iterations 2 \
  --first-run-max-steps 15
```

- **Environment:** `PYTHONPATH=browser-use`, `anthropic/claude-sonnet-4-6`, headless Chromium,
  viewport 1512×2400 (loop default).
- **Fresh start:** each site’s prior `action_cache.jsonl` and derived files were moved to
  `archive_before_retest_20260822/` before the run.
- **Logs:** `examples/action_cache/run_logs/roboform`, `books_toscrape`, `saucedemo`, `toolshop`
  (`loop.log` in each folder). Retest was first captured under `site1`…`site4` in a local
  workspace; evidence was copied here before commit.
- **Run 2 wall times:** `verify_replay()` from `run_repair_loop.py`, one sample per site,
  includes browser launch and initial navigation.

Site folders in this repo: **roboform** · **books_toscrape** · **saucedemo** · **toolshop**
(older local notes may still say site1…site4).

---

## Headline — V1 vs V2

| | V1 campaign tokens | V2 campaign tokens | V2 Run 2 tokens | V2 Run 2 wall |
|---|---|---|---|---|
| **Roboform (site1)** | 68,473 | 68,574 | 0 | 15.3 s |
| **Books (site2)** | 324,920 | 239,282 | 0 | 10.7 s |
| **Saucedemo (site3)** | 199,138 | 72,748 | 0 | 10.5 s |
| **Toolshop (site4)** | 104,148 | 116,601 | 0 | 38.5 s |
| **Total (4 sites)** | **696,679** | **518,205** | **0** | — |

Run 2 still uses **zero LLM tokens** on every replayable node — structural, not lucky (see
[METRICS.md § How the zero is proved](METRICS.md#how-the-zero-is-proved)).

V2 campaign total is lower mostly because saucedemo completed in **one** agentic pass (no repair)
and books used fewer repair steps; roboform and toolshop are within ~0.1% and +12% respectively.

---

## Run 1 — pass 0 (agentic exploration)

| Site | V1 steps | V1 tokens | V1 actions | V1 pass-0 coverage | V2 steps | V2 tokens | V2 actions | V2 pass-0 coverage |
|---|---|---|---|---|---|---|---|---|
| roboform | 6 | 68,473 | 4 | 4/4 complete | 6 | 68,574 | 4 | 4/4 complete |
| books | 15 | 149,417 | 2 | 2/3 missing basket | 10 | 86,427 | 2 | 2/3 missing basket |
| saucedemo | 16 | 173,640 | 12 | 3/5, cart **missing** | 7 | 72,748 | 5 | 3/5, cart **unverifiable** |
| toolshop | 7 | 77,925 | 3 | 3/4, cart **missing** | 7 | 77,786 | 3 | 3/4, cart **missing** |

**Provenance V1:** [METRICS.md](METRICS.md), `run_logs/books_toscrape/archive_before_retest_20260822/loop.log`,
`run_logs/saucedemo/loop_effects.log`, `run_logs/toolshop/archive_before_retest_20260822/loop.log`.

**Provenance V2:** `run_logs/roboform/loop.log` … `run_logs/toolshop/loop.log` stdout lines
`agent steps=… tokens=… actions recorded=…`.

---

## Repair passes — V1 vs V2

| Site | V1 repair | V1 repair tokens | V1 new locators | V2 repair | V2 repair tokens | V2 new locators | V2 end state |
|---|---|---|---|---|---|---|---|
| roboform | 0 | 0 | — | 0 | 0 | — | complete pass 0 |
| books | 2 | 89,159 + 86,344 | 0 | 2 | 45,500 + 47,355 | 0 | stopped: no new locator |
| saucedemo | 1 | 25,498 | +1 cart link | **0** | 0 | — | complete pass 0 |
| toolshop | 1 | 26,223 | +1 cart nav | 1 | 38,827 | +1 `data-test=nav-cart` | complete |

**Notes:**

- **Books (site2):** Both versions stop honestly — zero new locators after two repair attempts.
  V2 spent less because pass 0 and repair passes used fewer agent steps (10 + 6 + 6 vs 15 + 9 + 9).
- **Saucedemo (site3):** V1 agent failed pass 0 (12 cache rows, many dead cart clicks) and
  needed repair. V2 agent finished pass 0 in 7 steps; reviewer marked cart **unverifiable** on
  container click, not **missing** — no repair iteration.
- **Toolshop (site4):** Both need one repair for “open cart”. V2 repair cost more tokens
  (38,827 vs 26,223) because the agent clicked cart twice; Stage 4 dropped the duplicate row.

---

## Final automations — V1 vs V2

| Site | V1 nodes | V2 nodes | V1 gaps | V2 gaps | V1 xpath on locators | V2 xpath on locators | V2 `weak` in coverage |
|---|---|---|---|---|---|---|---|
| roboform | 4 | 4 | 0 | 0 | 0 | 0 | 0 |
| books | 3 | 3 | 1 | 1 | 2 (hook) → 2 in old emit | 0 (`get_by_role`) | 0 |
| saucedemo | 6 | **5** | 0 | 0 | 1 (cart xpath from repair) | 0 | 0 |
| toolshop | 4 | 4 | 0 | 0 | 2 (product + cart) | 0 (`data-test`) | 0 |

### Locator upgrades (V2 compile output)

| Site | Step | V1 command (typical) | V2 command |
|---|---|---|---|
| toolshop | open product | `xpath=…/a[5]` | `locator("[data-test=product-…]").first` |
| toolshop | open cart | `xpath=…/li[5]/a` | `locator("[data-test=nav-cart]").first` |
| books | Travel | `xpath=…/aside/…/a` | `get_by_role("link", name="Travel").first` |
| books | Himalayas | `xpath=…/ol/li[1]/…` | `get_by_role("link", name="It's Only the Himalayas").first` |
| saucedemo | cart | xpath cart link (6th node) | **removed** — 5-node path uses `#shopping_cart_container` only |
| roboform | all fields | `name=` locators | unchanged (already optimal) |

### Gap node placement (Phase 3)

During repair, when cart was still **missing**, logs show:

```
stage6.5 inserted gap node at step 'Open the cart page' …   (V2, site4 iteration 1)
stage6.5 inserted gap node at step 'Click the "Add to basket" …'   (V2, site2)
```

V1 logs said `appended gap node` for the same situations. Final books automation order:
Travel → Himalayas → gap (index 2).

---

## Run 2 — deterministic replay (V2 measured)

| Site | Nodes | Replayable | Prompt-only | Replayed | Failed | LLM tokens | Wall time |
|---|---|---|---|---|---|---|---|
| roboform | 4 | 4 | 0 | 4/4 | 0 | 0 | 15.3 s |
| books | 3 | 2 | 1 | 2/2 | 0 | 0 | 10.7 s |
| saucedemo | 5 | 5 | 0 | 5/5 | 0 | 0 | 10.5 s |
| toolshop | 4 | 4 | 0 | 4/4 | 0 | 0 | 38.5 s |

**V1 Run 2 wall (same machine, earlier session):** roboform 20.0 s · saucedemo 11.7 s ·
toolshop 38.6 s · books 10.7 s ([METRICS.md](METRICS.md)).

### Outcomes verified (V2 loop `replay check` + page read)

| Site | End URL | Task outcome |
|---|---|---|
| roboform | `…/filling-test-all-fields` | `myname`, `xyz`, `abc`, **SF in City field** (`13adr_city`) |
| books | book detail page | Travel + Himalayas replayed; gap node not executed in deterministic replay |
| saucedemo | `cart.html` | one Sauce Labs Backpack in cart |
| toolshop | `/checkout` | one Slip Joint Pliers, qty 1 |

---

## Phase impact on these numbers

| Phase | Visible in V2 retest |
|---|---|
| **1** Password redaction | site3: `action_cache.jsonl` has `text: null` on password row; automation uses `{password[0]}` |
| **2** Locator ladder | site2/site4: `get_by_role` / `data-test` in emitted JSON; `weak: []` on all four coverage files |
| **3** Gap order | site2/site4: `inserted gap node at step` during repair; books gap at index 2 |
| **4** Click filter | site4: `stage3 dropped off-task click trace[4]` (duplicate nav-cart); site3: 5 raw→5 kept vs V1 13→12 before slice |

---

## Campaign totals

| Version | roboform | books | saucedemo | toolshop | **Sum** |
|---|---|---|---|---|---|
| **V1** | 68,473 | 324,920 | 199,138 | 104,148 | **696,679** |
| **V2** | 68,574 | 239,282 | 72,748 | 116,601 | **518,205** |

Token figures are whole-pass totals reported by browser-use (`tokens=` in `loop.log`), not
prompt/completion splits.

---

## Honest caveats (same as V1, plus V2 notes)

- **Run 2 wall times** are single samples, not averages.
- **V2 saucedemo token drop** is largely agent behavior (clean pass 0), not a pipeline magic
  bullet — a thrashing run would still cost repair tokens.
- **Saucedemo cart** still caches `#shopping_cart_container` when the agent clicks the wrapper
  `div` instead of `data-test=shopping-cart-link`; replay still reaches `cart.html`.
- **Books** remains the deliberate negative result: two repair passes, zero new locators, one
  prompt-only node.
- **Run 1 wall time** per pass is still not logged by the loop (only whole-loop duration in
  inference logs for roboform ~69 s in V1).

---

## Archive paths

Pre-retest artifacts (V1-era runs on the same workspace):

```
run_logs/roboform/archive_before_retest_20260822/   (no prior loop.log — first capture here)
run_logs/books_toscrape/archive_before_retest_20260822/
run_logs/saucedemo/archive_before_retest_20260822/
run_logs/toolshop/archive_before_retest_20260822/
```

Current V2 logs and emitted automations: `run_logs/<site>/loop.log` plus matching pipeline
jsonl in each site folder under `examples/action_cache/run_logs/`.
