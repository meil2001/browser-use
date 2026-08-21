# Optexity Assignment — Complete Solution

A learning/memory layer for Optexity automations: **Run 1 explores with an LLM, we record
what the browser actually did, and Run 2 replays it as deterministic Playwright steps with
zero model calls.**

This document is the whole thing, top to bottom — the design, every pipeline stage, the
rules that decide what gets cached, the results on four sites with measured numbers, every
bug found along the way, and an honest list of what is still broken.

---

## The problem, in one paragraph

Optexity workflows are JSON graphs of nodes. A node is either **deterministic** (a Playwright
locator plus an action — fast, free, repeatable) or **agentic** (a natural-language task handed
to browser-use plus an LLM — flexible, slow, expensive, and with no memory between runs). The
waste is that an agentic node re-explores the same page every single time, even though most of
what it does ("type the username", "click submit") is perfectly deterministic once you know
where the fields are. The assignment is to build the missing memory: watch an agentic run,
learn the deterministic steps from it, and replay them without the model.

---

## The core idea

> Run 1 is a student taking a test with a tutor. Run 2 is a cheat sheet built from what the
> student **did**, not from what the tutor **said**.

The LLM talks a great deal — reasoning, evaluations, self-assessments. All of it is discarded.
The only thing recorded is the moves the browser actually made, captured at the moment of
execution, together with enough information about each element to find it again tomorrow.

Two rules follow from this and are enforced everywhere:

1. **No invented locators.** Every selector in the output traces back to a logged event from a
   real browser action. If we don't have a locator, we say so — we never guess one.
2. **Never trust the agent's self-report.** browser-use routinely announces success it did not
   achieve. Verdicts come from the recorded trace and from measured page effects, never from
   the model's prose.

---

## Where the code lives

| Location | Lines | What it is |
|---|---|---|
| `browser-use/browser_use/action_cache.py` | 1,738 | The entire pipeline: hook, identity, filters, review, emit. **New file.** |
| `run_repair_loop.py` | 486 | The iterative loop driver: replay, repair, verify |
| `browser-use/browser_use/tools/service.py` | +19 | Two hook call sites (click, input) |
| `browser-use/browser_use/browser/session.py` | +8 | Fork compatibility (`include_full_page`) |
| `browser-use/browser_use/dom/views.py` | +2 | Fork compatibility (`remove_empty_nodes`) |
| `optexity/inference/child_process.py` | +22 | Local automation JSON override for iteration |
| `optexity/inference/models/chat_litellm.py` | +17/−3 | Token accounting |

The design deliberately concentrates almost everything in one new file inside the
**browser-use** fork rather than spreading edits across both repos. browser-use is where the
actions actually happen, so that is where they can be observed; the two `+8`/`+2` edits are
pure compatibility fixes for pre-existing mismatches in the fork, not part of the feature.

### Artifacts produced per run

```text
run_logs/<site>/
  action_cache.jsonl          Stage 1 — raw executed actions
  action_cache_aligned.jsonl  Stage 3 — on-task only
  action_cache_sliced.jsonl   Stage 4 — one record per field/element
  action_cache_located.jsonl  Stage 5 — with Playwright commands
  coverage.json               Stage 6.5 — what's missing, weak, ineffective
  run_review.json             Post-run LLM review: step verdicts + evidence
  dispatched_events.jsonl     Hook audit: what the event bus dispatched
  repair_attempts.json        History of repair prompts and outcomes

automations/<site>/
  test_automation_cached.json Stage 6 — the deliverable
```

Every stage writes its own file. That is deliberate: when a step goes missing you can point at
the exact stage that dropped it, which is the difference between "the cache is wrong" and
"Stage 3 dropped it because the typed value wasn't on the task list."

---

## How it works — the two halves

The mechanics live in two focused documents rather than in this one:

| Document | Covers |
|---|---|
| **[PIPELINE.md](PIPELINE.md)** | The step logic: the hook, element identity, the filters, effect measurement, emitting the automation, and the explicit good-vs-bad rules |
| **[LOOP.md](LOOP.md)** | The loop logic: the replay-first invariant, the post-run review, and the iterative repair loop |

The rest of this document is the context around them: results, the bugs that shaped the
design, what is still broken, and what a reviewer is likely to ask.

---

## Results — four sites, measured

Full numbers with log-line provenance in `examples/action_cache/METRICS.md`.

### Run 1 (agentic exploration)

| Site | Task shape | Steps | Tokens | Actions | Coverage after pass 0 |
|---|---|---|---|---|---|
| roboform | 4-field form fill | 6 | 68,473 | 4 | 4/4 — complete, no repair |
| books_toscrape | category → book → basket | 15 | 149,417 | 2 | 2/3 |
| saucedemo | login → add product → cart | 16 | 173,640 | 12 | 3/4 |
| toolshop | search → result → add → cart | 7 | 77,925 | 3 | 3/4 |

### Repair passes

| Site | Passes | Tokens | New locators | Outcome |
|---|---|---|---|---|
| roboform | 0 | 0 | — | complete after pass 0 |
| saucedemo | 1 | 25,498 | +1 | complete |
| toolshop | 1 | 26,223 | +1 | complete |
| books_toscrape | 2 | 89,159 + 86,344 | +0 | stopped: task impossible |

### Run 2 (deterministic replay)

| Site | Nodes | Agentic | Replayed | Failed | Tokens | Wall |
|---|---|---|---|---|---|---|
| roboform | 4 | 0 | 4/4 | 0 | **0** | 20.0 s |
| saucedemo | 6 | 0 | 6/6 | 0 | **0** | 11.7 s |
| toolshop | 4 | 0 | 4/4 | 0 | **0** | 38.6 s |
| books_toscrape | 3 (1 prompt-only) | 0 | 2/3 | 0 | **0** | 10.7 s |

Headline for the required site: **68,473 tokens and 6 LLM steps → 0 tokens and 0 LLM steps.**

Outcomes verified by reading the page, not by trusting an exit code: Roboform fills all four
fields including `13adr_city` for `SF`; saucedemo ends on `cart.html` with exactly one Sauce
Labs Backpack; toolshop ends on `/checkout` with exactly one Slip Joint Pliers.

### The zero is proved three ways

1. No node in any emitted automation contains `agentic_task`.
2. Every node carries `skip_prompt: true` — no LLM fallback exists even on locator failure.
3. The replay path is pure Playwright; no model client is constructed. The Roboform Run 2
   server log independently shows zero `📍 Step` occurrences.

### The architectural win, stated precisely

| saucedemo design | Passes | Total tokens | Result |
|---|---|---|---|
| teleport to last URL | 4 | 448,282 | still incomplete |
| replay prefix, LLM on the gap | 2 | 199,138 | complete |

### What each site was chosen to prove

- **roboform** — the required baseline, and the City-vs-State disambiguation.
- **books_toscrape** — the honest negative. The "Add to basket" button genuinely does not
  exist on that page; the loop spent two passes, added zero locators, and reported the task as
  impossible instead of inventing a selector. A system that cannot say "no" is not trustworthy
  when it says "yes".
- **saucedemo** — login, session state, and the case where the cached automation completed
  a task the agent itself never could.
- **toolshop** (practicesoftwaretesting.com) — the hardest shape: a **dynamic locator** (the
  product link exists only after searching), a **conditionally-appearing element** (the cart
  link exists only once the cart is non-empty), an **opaque ULID URL** so no navigate-shortcut
  can rescue a missed click, four near-identical "Pliers" cards requiring real disambiguation,
  and **Angular** rather than React for the effect probe.

Toolshop also produced the cleanest proof of the invariant. Because the cart link does not exist
while the cart is empty, a teleport to the product URL would have asked the LLM to click an
element that wasn't on the page. Replaying the three cached steps first put a real item in the
cart, the cart link appeared among 43 landmarks, and one pass closed it. On saucedemo
replay-first was *cheaper*; on toolshop it was *necessary*.

---

## Bugs found and fixed

The list a demo should volunteer rather than have extracted from it.

| Bug | Fix |
|---|---|
| `SF` typed into State, not City | Capture `field_label()` at hook time; `role_match_score()` compares role to label |
| Silent fallback to hardcoded Roboform values on any other site | Extraction returns only what it found; `is_on_task()` fails open |
| Coverage blind to missing *clicks*, only saw missing values | Post-run review sees task **and** trace |
| Agent reports success it never achieved | Verdicts read the trace; `recorded_a_new_action: false` outranks prose |
| `automation.url` taken from the first recorded action | Prefer the declared start URL |
| Sharpener invented page details that didn't exist | `page_landmarks()` grounds prompts in real element labels |
| Early-stop rule fired on non-interactive targets | Hard stop removed — landmarks only lists *interactive* elements; now a hint |
| Teleport-to-URL caused repeated logins and poisoned prompts | The replay-first invariant |
| Typing credited with navigating | Only navigable actions get navigation credit |
| Dead click marked effective (stale cross-navigation baseline) | `comparable` check; structural signals over text length |
| Probe read the DOM before React re-rendered (18 ms) | 300 ms settle in `capture_effect()` |
| Progress measured as "the trace grew" | Progress = an *effective* locator for the broken step |
| `include_full_page` / `remove_empty_nodes` fork mismatches | Compatibility args in `session.py` / `views.py` |

One earlier claim was **corrected by later evidence**: I had written that browser-use 0.11.4's
clicks never reach React on saucedemo. A clean run recorded 11 dispatched clicks with no hook
blind spots and the cart badge did appear partway through, so at least one click landed. The
supportable claim is that they are **intermittent**, consistent with hydration timing — clicks
fired immediately after login do nothing, later ones land.

---

## Known limitations, in severity order

1. **Position-dependent xpath.** Site 4 cached `.../div[1]/a[5]` and `ul/li[5]/a`. They work
   today, but if catalog order or relevance ranking changes, `a[5]` silently clicks a *different
   product* and replay still reports success. Flagged `weak` in coverage, not hardened. This is
   the only limitation that can produce a **wrong result** rather than a visible failure, which
   makes it the most serious.
2. **Two silent field-mismatch bugs, one root cause.** `roles` is a flat `value → role` dict, so
   one value can only ever carry one role. Consequences: (a) a right value typed into the wrong
   field with no competing correct write still reports `missing: []`; (b) a value legitimately
   needed twice (confirm-email) loses one of the two fields. The fix is `(field-identity, role)`
   pairs instead of a flat map.
3. **First action on a new page can't be verified.** The rule forbidding comparison across a
   navigation — which correctly killed a false positive — means the first action on a freshly
   loaded page has no same-page baseline, so a non-navigating click there degrades to
   `no_navigation`. This is why toolshop's add-to-cart is unverified despite demonstrably working.
   The fix is a baseline reading captured on page arrival.
4. **Redundant nodes are flagged, not merged.** Dedupe keys on element identity, so three
   genuinely different identities that mean "open the cart" all survive. Merging needs a notion
   of "same intent" that doesn't exist yet.
5. **Only `click` and `input` are recorded or emittable.** `navigate`, `select_option` and
   `send_keys` are dispatched but not captured, and the emitter speaks only `click_element` and
   `input_text`. The hook audit reports this rather than hiding it.
6. **iframe / shadow DOM elements** — fails loudly (the locator doesn't resolve). Named scope.
7. **Auto-generated ids that change per reload** — undetectable at compile time by any design.
8. **Passwords** — logged as `text: None` and dropped as off-task; routed around, not solved.

---

## Bonus items

Both optional items are built and working:

- **Auto-generated cached automation from logs** — `emit_cached_automation()` produces the JSON
  directly from the log, schema-validated with `Automation.model_validate()`. No hand-writing,
  which is what makes the no-invented-locators guarantee checkable rather than a promise.
- **The iterative loop** — `run_repair_loop.py`, run → cache → rebuild → rerun → recache, with
  a strict progress rule so it terminates honestly.

Beyond the brief: the hook coverage audit, effect tracking, evidence-cited step review,
page-grounded prompt sharpening, parameterized output, and a documented negative result.

---

## Questions a reviewer will ask

**"Show me the log line that produced this locator."**
`action_cache.jsonl` line → `identity: {"by": "id", "value": "search-query"}` →
`action_cache_located.jsonl` command → node 0 of `test_automation_cached.json`. Every stage on
disk, one file per stage.

**"What if browser-use clicked the wrong field first?"**
Stage 4 keeps the write whose field label matches the task role, not the last one. That is
`role_match_score()`, and it exists because the naive last-write rule got City right by luck.

**"Prove Run 2 used fewer tokens."**
68,473 → 0 on Roboform. Proved three ways: no `agentic_task` node exists, every node is
`skip_prompt: true`, and the replay path constructs no model client.

**"Why hook browser-use rather than optexity?"**
Because that is where actions actually execute and where the live DOM node is still in scope.
Hooking optexity would see the node graph, not the element that was really touched.

**"Why did you drop step 4?"**
Every drop names its rule — off-task value, superseded write, wrong role for the label, or no
observed effect — and `coverage.json` lists what was dropped and why.

**"What's still wrong with it?"**
*Known limitations*, starting with the position-dependent xpath that could silently add the wrong
product — the one known defect that can produce a wrong result while reporting success.

---

## File map

| File | Contents |
|---|---|
| `browser_use/action_cache.py` | The pipeline: hook, identity, filters, review, emit |
| `browser_use/tools/service.py` | The two hook call sites (click, input) |
| `browser_use/browser/session.py`, `browser_use/dom/views.py` | Fork compatibility fixes |
| `examples/action_cache/README.md` | Introduction and index |
| `examples/action_cache/DESIGN.md` | This file: results, bugs, limitations, reviewer questions |
| `examples/action_cache/PIPELINE.md` | Step logic: hook, identity, filters, effects, emit |
| `examples/action_cache/LOOP.md` | Loop logic: invariant, post-run review, repair loop |
| `examples/action_cache/METRICS.md` | Measured Run 1 vs Run 2, every row citing a log line |
| `examples/action_cache/run_repair_loop.py` | Loop driver: replay, repair, verify |
| `examples/action_cache/automations/` | Every automation, agentic and cached, plus its own README |
| `examples/action_cache/run_logs/` | The evidence behind every locator, plus its own README |
| `examples/action_cache/run_logs/diagnostics/` | One-off scripts that shaped two design rules |
| `examples/action_cache/tools/` | Env var template and the inference server launcher |

Companion changes in the `optexity` fork: a local automation-JSON override for iteration
without a server round-trip, which also propagates input and secure parameters so
parameterized cached automations resolve their values, and a `ChatLiteLLM.ainvoke` that accepts
and drops the `session_id` browser-use 0.11+ passes and litellm rejects. See `ACTION_CACHE.md`
there.

---

## In one page

Hook browser-use at execute time, where the live DOM node is still in scope, and record what
the browser *did* along with each element's durable identity, its visible label, and a
measurement of what actually changed. Filter that trace with named rules — on-task values,
one write per element, the field whose label matches the task's role, the attempt that
demonstrably worked. Rank locators by durability and never cache an index. Emit a parameterized,
schema-valid Optexity automation in which every selector traces to a log line. Ask one LLM,
after the fact and with no sight of any locator, whether the trace covers the task, and require
it to cite evidence. Where it doesn't, replay everything already known through Playwright with
no model at all, let the LLM touch only the broken step with a prompt grounded in that live
page's real vocabulary, and recompile. Stop when a genuine locator stops appearing — even if
that means reporting the task impossible.

Result: 68,473 tokens and 6 model steps become 0 and 0, on four sites, with a coverage report
that names everything it couldn't do.
