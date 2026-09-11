# Run 1 vs Run 2 — measured

> **Version 2:** Phases 1–4 revision and a full four-site retest are documented in
> [VERSION2.md](VERSION2.md) and [METRICS_V2.md](METRICS_V2.md). This file remains the **V1**
> submission baseline.

Every number here comes from a log line or a timed run, and each row says where.
Nothing is estimated. Where something is not measured, it says so rather than guessing.

Reproduce any Run 1 row with:

```bash
python examples/action_cache/run_repair_loop.py \
  --automation examples/action_cache/automations/<site>/test_automation.json \
  --run-dir examples/action_cache/run_logs/<site> \
  --max-iterations 2 --first-run-max-steps 15
```

---

## Headline

| | Run 1 (agentic) | Run 2 (cached replay) |
|---|---|---|
| LLM tokens, Roboform | **68,473** | **0** |
| LLM ReAct steps, Roboform | **6** | **0** |
| Wall time, Roboform | ~69 s (whole loop) | **20.0 s** |

Run 2 uses zero tokens on all four sites. That is structural, not lucky — see
"How the zero is proved" below.

---

## Run 1 — agentic exploration (pass 0 of the loop)

| Site | Task shape | Steps | Tokens | Actions recorded | Coverage after pass 0 |
|---|---|---|---|---|---|
| **roboform** | 4-field form fill | 6 | 68,473 | 4 | 4/4 — complete, no repair needed |
| **books_toscrape** | category → book → basket | 15 | 149,417 | 2 | 2/3 — button genuinely absent |
| **saucedemo** | login → add product → cart | 16 | 173,640 | 12 | 3/4 — cart step missing |
| **toolshop** | search → result → add → cart | 7 | 77,925 | 3 | 3/4 — cart step missing |

Provenance: `run_logs/books_toscrape/loop.log:487`,
`run_logs/saucedemo/loop_effects.log:183`, `run_logs/toolshop/loop.log:96`.
Roboform's figures come from the loop's stdout; its artifacts
(`run_logs/roboform/coverage.json`, `run_review.json`, `action_cache.jsonl`) corroborate the
step and action counts independently, though not the token figure.

## Repair passes — replay-first, LLM only on the broken step

| Site | Passes | Tokens | New locators | Ended |
|---|---|---|---|---|
| roboform | 0 | 0 | — | complete after pass 0 |
| saucedemo | 1 | 25,498 | +1 | complete |
| toolshop | 1 | 26,223 | +1 | complete |
| books_toscrape | 2 | 89,159 + 86,344 | +0 | stopped: task impossible |

Provenance: `run_logs/saucedemo/loop_effects.log:243`, `run_logs/toolshop/loop.log:154`,
`run_logs/books_toscrape/loop.log:502,517`.

books_toscrape is the negative result worth keeping. Two passes added zero locators, the
loop's no-progress rule fired, and it reported the "Add to basket" button as genuinely absent
instead of inventing a selector.

---

## Run 2 — deterministic replay

Measured headless Chromium, one sample each, wall time **including** browser launch and the
initial page load.

| Site | Nodes | Agentic nodes | Prompt-only | Replayed | Failed | Tokens | Wall |
|---|---|---|---|---|---|---|---|
| roboform | 4 | 0 | 0 | 4/4 | 0 | 0 | 20.0 s |
| saucedemo | 6 | 0 | 0 | 6/6 | 0 | 0 | 11.7 s |
| toolshop | 4 | 0 | 0 | 4/4 | 0 | 0 | 38.6 s |
| books_toscrape | 3 | 0 | 1 | 2/3 | 0 | 0 | 10.7 s |

Outcomes verified by reading the page, not by trusting the exit code:

- **roboform** — 4 fields filled, including `13adr_city` for `SF`. The City-vs-State bug does
  not recur on a clean run.
- **saucedemo** — ends on `cart.html` with exactly one Sauce Labs Backpack.
- **toolshop** — ends on `/checkout` with exactly one Slip Joint Pliers, quantity 1, chosen
  correctly from four cards that all say "Pliers".
- **books_toscrape** — replays the 2 steps it has and stops; the third node is prompt-only by
  design.

### How the zero is proved

Three independent checks, not one claim:

1. No node in any cached automation contains `agentic_task`.
2. Every emitted node carries `skip_prompt: true`, so there is no LLM fallback path even
   if a locator fails.
3. The replay path is pure Playwright — no model client is constructed. The Roboform Run 2
   inference log independently shows `0` occurrences of `📍 Step`
   (`run_logs/inference/inference_run2_20260820_214935.log`).

---

## The architectural win, stated precisely

saucedemo, same task, same site, two designs:

| Design | Passes | Total tokens | Result |
|---|---|---|---|
| Teleport to last URL | 4 | **448,282** | still incomplete |
| Replay cached prefix, LLM on the gap | 2 | **199,138** | complete |

448,282 = 172,669 + 97,963 + 77,480 + 100,170 (`run_logs/saucedemo/loop.log`).
199,138 = 173,640 + 25,498 (`run_logs/saucedemo/loop_effects.log`).

The old design burned 275,613 tokens on three repair passes and never closed the gap,
because teleporting to `inventory.html` bounced to the login screen and the sharpener
then described that login screen while trying to solve a cart problem. Replaying the
cached prefix put the agent on the right page with the right session state, and one pass
of 25,498 tokens finished it.

Campaign totals: roboform 68,473 · books_toscrape 324,920 · saucedemo 199,138 ·
toolshop 104,148.

---

## Honest gaps

- **Run 1 wall time is only clean for roboform** (~69 s, measured). The loop does not
  timestamp per-pass, so the other three sites have token and step counts but no reliable
  per-pass duration. The older inference logs can't fill this in either: that Run 2 log
  spans 80 minutes because it's a long-lived server log, not a run.
- **Run 2 wall times are single samples** on one machine, not averages, and toolshop's 38.6 s
  reflects a slow Angular site rather than replay overhead.
- **Token counts are whole-pass totals** reported by browser-use, not split into prompt vs
  completion.
- toolshop's add-to-cart click is recorded as `no_navigation` — replay proves it works, but
  the effect probe could not verify it, because it was the first action on a freshly loaded
  page and so had no same-page baseline to compare against.
