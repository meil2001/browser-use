# Optexity Assignment — Complete Solution

A learning/memory layer for Optexity automations: **Run 1 explores with an LLM, we record
what the browser actually did, and Run 2 replays it as deterministic Playwright steps with
zero model calls.**

This document is the whole thing, top to bottom — the design, every pipeline stage, the
rules that decide what gets cached, the results on four sites with measured numbers, every
bug found along the way, and an honest list of what is still broken.

---

## 1. The problem, in one paragraph

Optexity workflows are JSON graphs of nodes. A node is either **deterministic** (a Playwright
locator plus an action — fast, free, repeatable) or **agentic** (a natural-language task handed
to browser-use plus an LLM — flexible, slow, expensive, and with no memory between runs). The
waste is that an agentic node re-explores the same page every single time, even though most of
what it does ("type the username", "click submit") is perfectly deterministic once you know
where the fields are. The assignment is to build the missing memory: watch an agentic run,
learn the deterministic steps from it, and replay them without the model.

## 2. The core idea

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

## 3. Where the code lives

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

## 4. The invariant — how every retry is run

This is the single most important design decision, and it was learned the hard way.

1. **Replay every step we already hold a locator for through Playwright, with no LLM.** Not a
   teleport to a saved URL. A URL is not a position in a task — the *session* is. Replaying
   earns the cookies, the login and the correct page as a side effect, and costs about a second
   instead of a dozen model calls.
2. **The LLM takes over only at the broken step**, as one gap node's `prompt_instructions`.
   One node, one instruction, nothing else.
3. **Read the page's element inventory from that live session**, after the replay — so the
   vocabulary fed to the prompt sharpener comes from the page we are actually on. Scanning by
   URL beforehand reads whatever a logged-out visit gets redirected to.
4. **Progress means a locator appeared for the broken step.** Not "the trace grew" — a replayed
   prefix grows the trace every single time, which is exactly how a broken loop convinces
   itself it is making headway.

Violating (1) is what made an early saucedemo run cost 448,282 tokens across four passes and
still fail: it teleported to `inventory.html`, got bounced to the login screen, and then
described that login screen to the prompt sharpener while trying to solve a cart problem.
Honouring the invariant closed the same gap in one pass for 25,498 tokens.

One ordering detail is load-bearing rather than stylistic: **Playwright launches the browser
and browser-use attaches afterwards over CDP.** The replay must run on a clean Playwright
session, because with a browser-use session attached, Playwright's clicks become unreliable on
React sites.

---

## 5. The pipeline, stage by stage

### Stage 1 — Hook at *doing*, not at *thinking*

`append_executed_action()`, called from `tools/service.py` immediately after the click or type
succeeds.

"I should fill the city next" is worthless for replay. "I typed `SF` into this box" is
everything. The hook fires at execute time for one reason: that is the only moment when the
live DOM node object is still in scope **and** we know the action succeeded. One instant
earlier and we don't know it worked; one instant later and the node reference is gone.

Each record captures:

```json
{"t": "...", "action": "input", "index": 13, "text": "Pliers",
 "url": "https://...", "tag": "input", "attributes": {...},
 "xpath": "...", "label": "Search", "shadow_hosts": [],
 "identity": {"by": "id", "value": "search-query"},
 "after": {"title": "...", "interactive": 43, "text_len": 1385, "target": {...}}}
```

Structured JSONL, one object per line — not print statements. It can be diffed, filtered,
replayed and audited, and every later stage is a pure function over it.

**`label` is the quiet hero.** `field_label()` walks up to four ancestors looking for visible
text, falling back through `aria-label`, `title` and `placeholder`. Without it the compiler can
only ask "was this string on the task list?", which cannot tell the City box from the State box
when both accept `SF`. With it, the compiler can ask the far better question: "does this
field's label match the *role* the task asked for?"

**`after` is the effect probe** — see §7.

#### The hook audit

A hook on two call sites raises an obvious objection: what about everything else? So
`install_hook_audit()` attaches a wildcard observer to `browser_session.event_bus`, logs every
dispatched event to `dispatched_events.jsonl`, and `hook_coverage()` compares dispatched
against recorded. If browser-use performs a replayable action we failed to record, it appears
as a named blind spot rather than as a silent hole. This is how we know, with evidence, that
`NavigateToUrlEvent` is dispatched but never recorded.

### Stage 2 — Give every element a durable identity

`node_identity()`, `stable_identity()`.

browser-use addresses elements by index: `[13]<input>`. Index 13 is "the thirteenth interactive
thing on screen right now" — it dies on the next re-render. So each index is converted, at
capture time, into something that survives: `name`, then `id`, then `placeholder`, then an
xpath, plus any shadow-DOM host chain. **The index is never cached.**

### Stage 3 — Keep only what the task asked for

`align_cache()`, `is_on_task()`, `required_values_and_roles()`.

If the agent typed into a search bar while exploring, that is not part of the task. The filter
compares typed text against the values the task actually requires.

Two safety properties matter here more than the filter itself:

- **No silent substitution.** An earlier version fell back to Roboform's hardcoded values
  (`myname`, `xyz`, `abc`, `SF`) whenever its regex found nothing — which meant that on any
  other site, Stage 3 would silently delete every real action. Now extraction returns only what
  it genuinely found, including nothing.
- **Fail open, not closed.** `is_on_task()` treats an empty requirement list as "keep
  everything", not "reject everything". Worst case we over-keep, which `coverage.json` surfaces
  in its `extra` field as a visible problem. The alternative — silently deleting real data — is
  the failure mode that destroys trust in a cache.

`annotate_effects()` also runs here, on the **raw** consecutive trace, before any filtering.
It has to: filtering first would break adjacency and make the URL comparison compare two
actions that never actually followed one another.

### Stage 4 — One record per element (drop the retries)

`slice_cache()` = `last_write_per_identity()` → `best_write_per_input_text()`.

Run 1 types the same value repeatedly, corrects itself, and clicks the same button many times.
The cheat sheet needs one entry per element. Three rules, applied in order:

1. **Last write wins per identity** — the final successful write to a given element.
2. **Prefer the attempt that demonstrably did something** — `effect_rank()` promotes a record
   with a measured effect over one without, with `>=` preserving last-write-wins on equal
   evidence so traces with no effect data behave exactly as before.
3. **Role-match scoring** — when the same value was typed into two different boxes,
   `role_match_score()` compares the task's role ("city") against each field's captured label
   ("City" vs "State") and keeps the one that matches.

Rule 3 is what replaced luck with logic. The original rule was "if the same value went into two
boxes, keep the later one" — and `SF` happened to land in City second. Had the order been
reversed, we'd have cached State and never noticed.

### Stage 5 — Choose the most durable locator

`locate_cache()`, `playwright_command()`.

Preference order: `name` → `id` → `placeholder` → xpath. Anything resolved only by xpath is
flagged `weak` in the coverage report rather than silently accepted.

### Stage 6 — Emit the automation

`emit_cached_automation()`, `record_to_action_node()`, `build_param_names()`.

Produces `test_automation_cached.json`: `input_text` and `click_element` nodes, each carrying a
locator that came from a log line, each with `skip_prompt: true` so there is no LLM fallback
path even if a locator fails. Validated against the real
`optexity.schema.automation.Automation.model_validate()`, including the `key.isidentifier()`
constraint the schema enforces on parameter names.

**Parameterization.** Literal values are lifted into `input_parameters` and referenced as
`{city[0]}`, so the workflow is reusable with different data instead of hardcoding one person's
details. Names come from the task role when known, falling back to the value, with
collision-safe suffixing:

```json
"input_parameters": {"full_name": ["myname"], "address_line_1": ["xyz"],
                     "address_line_2": ["abc"], "city": ["SF"]}
```

### Stage 6.5 — Coverage: say what is missing

`coverage_report()`, `weak_identities()`, `ineffective_nodes()`, `write_coverage()`.

The stage that makes the whole thing honest. `coverage.json` reports:

- **value coverage** — required values cached vs missing
- **step coverage** — which task steps the trace supports (from the review, §6)
- **weak** — locators resolved only by xpath
- **ineffective** — cached actions never observed to change anything
- **extra** — cached actions that don't correspond to anything the task asked for

Anything missing becomes a **gap node**: `skip_command: true`, `skip_prompt: false`. It carries
an instruction and no locator, so the LLM fills that one step, the run is recorded, and the
pipeline recompiles — this time with a real locator. That is the mechanism by which the cache
grows without anyone ever inventing a selector.

---

## 6. The post-run review — catching missing *steps*, not just missing values

An earlier design extracted a checklist from the task text alone. Testing on a second site
killed it: a task-text-only spec can notice a missing typed *value*, but it is blind to a
missing *click*. On books.toscrape the required values list was legitimately empty, so coverage
reported "nothing missing" for a run that had done two of three steps.

The fix is one LLM call that runs **after** the run and sees both the task and the trace.

`load_run_review()` → `llm_review_run()` → `_validated_review()`, writing `run_review.json`:

```text
1. [covered]  Type "Pliers" into the search box    evidence: trace[0] 'Search'
2. [covered]  Open the product "Slip Joint Pliers" evidence: trace[1] 'Slip Joint Pliers...'
3. [covered]  Add that product to the cart         evidence: trace[2] 'Add to cart'
4. [missing]  Open the cart page                   evidence: —
```

The model gets `trace_digest()` — a deliberately **locator-free** view. It never sees an xpath,
an identity or a command, so it cannot produce a locator even if it wanted to. It segments the
task into steps and judges each as `covered`, `missing` or `unverifiable`, and it must cite a
trace index as evidence.

This is where the "never trust the agent" rule pays off most visibly. On toolshop the agent
reached the cart by calling `navigate` — which isn't hooked — and then reported
`✅ Opened the cart page`. The reviewer read the trace, found no recorded action, and marked
the step `missing`. Without it we would have shipped a three-node automation that stops on the
product page while the agent's own summary insisted the task was complete.

### Guardrails on the model's output

Because an LLM is in the loop, every output is policed:

- `_scrub_repair_prompt()` + `_LOCATOR_LEAK_RE` — reject any repair prompt containing something
  that looks like a selector, so the model can never smuggle in a locator
- `_validated_review()` — verdicts must be one of `_STEP_VERDICTS`; evidence indices must exist
- `_empty_step_summary()` — a failed review yields an explicit "no check ran, this is not a
  pass", never a silent success
- `_review_fingerprint()` — results cached on `(task, digest)`, so recompiling never re-bills
- `_extract_json_object()` — tolerates markdown fences and surrounding prose

The review's only job is judging coverage and writing English prompts. It never picks a
locator, and it cannot overrule a measured effect.

---

## 7. Effect tracking — "executed" versus "achieved"

The deepest bug in the original design: the recorder captured that an action *ran*, not that it
*worked*. On saucedemo, browser-use clicked "Add to cart" repeatedly, the hook faithfully
recorded every click as a success, and the cart stayed empty. Ten recorded successes, zero
achieved. Everything downstream inherited that lie.

The fix is to measure, at execute time, what changed.

**`_EFFECT_PROBE`** — one JS snippet, run immediately after each action, reading three
page-wide numbers (title, interactive element count, body text length) plus the state of the
element just touched (present? text? value?).

**`_PROBE_SETTLE_SECONDS = 0.3`** — a framework re-render is fast but not instant; saucedemo
renames its add-to-cart button 18 ms after the click. A probe fired straight down an already-open
CDP channel can beat the re-render, read the stale DOM, and report a working click as dead. The
settle exists to lose that race deliberately.

**`action_effect()`** compares each record against the previous one (the previous record's
`after` doubles as this action's `before`, so one probe per action suffices) and returns:

| Verdict | Meaning |
|---|---|
| `effective` | the page provably changed, or the field holds the typed value |
| `no_observed_effect` | the action ran and nothing changed at all |
| `no_navigation` | a click that didn't navigate; in-page effects unmeasured |
| `unknown` | no usable data — **never** treated as failure |

Three subtleties, each learned from a real false verdict:

- **Only navigable actions get credit for navigating.** Typing cannot move the page, so a URL
  change straddling a typing action came from something else.
- **Never compare two readings that straddle a navigation.** A reading taken just after a page
  load catches it mid-render; charging that drift to the next click declared a dead button
  successful.
- **Structure beats prose.** Title and interactive-element count survive re-render noise; raw
  text length does not, so a text-only difference yields `unknown`, not proof.

Wired into four places: dedupe prefers effective records; the reviewer sees the verdict and is
told to prefer citing an `effective` entry; coverage flags ineffective nodes; and the loop's
progress rule counts only effective new locators.

Crucially, ineffective records are **flagged, never deleted** — because on saucedemo that same
"dead" click produced the locator that works perfectly under Playwright. An ineffective action
tells you about the executor that ran it, not necessarily about the locator it yielded.

---

## 8. The iterative repair loop

`run_repair_loop.py`. Run 1 → review → sharper prompt → repair → re-review, until nothing is
missing or no progress is made.

```text
pass 0   agentic exploration from the declared start URL
         ↓ compile, review
         is anything missing?
pass n   replay_prefix()          all cached steps, pure Playwright, zero LLM
         page_landmarks()         read the live page's actual element labels
         improve_repair_prompt()  sharpen using landmarks + prior failures
         run_agent()              browser-use attaches over CDP, ONE broken step
         ↓ compile, review
         did an effective new locator appear?  no → stop
verify   verify_replay()          run the whole emitted automation, pure Playwright
```

`improve_repair_prompt()` is fed the page's real vocabulary from `page_landmarks()` and is told
to reference only elements in that list. This exists because the sharpener used to invent page
details ("the green or orange button") that were nowhere on the page. It also receives the
prior attempts including `recorded_a_new_action` and `agent_claimed_success` — and is told
explicitly that `recorded_a_new_action: false` outranks any prose the agent wrote.

The loop stops on **no progress**, not on a fixed iteration count, and "progress" is
deliberately strict (an *effective* locator for the *broken* step). That is what lets it
conclude a task is impossible instead of burning passes forever.

---

## 9. Good vs bad steps — the explicit rules

Not "I looked at the log and picked". Every drop is a rule with a name:

| Kept | Dropped | Rule |
|---|---|---|
| typed value on the task's list | typing into a search bar while exploring | Stage 3, `is_on_task()` |
| final write to a field | the seven earlier retries | Stage 4, `last_write_per_identity()` |
| the field whose label matches the task role | the same value in the wrong box | Stage 4, `role_match_score()` |
| the attempt that changed something | the identical attempt that didn't | Stage 4, `effect_rank()` |
| `name`/`id`/`placeholder` locators | the element index | Stage 5, identity order |
| — | scrolls, hovers, no-ops | never recorded: the hook only fires on click/input |

Anything that survives all of these and *still* can't be turned into a locator becomes a gap
node rather than a guess.

---

## 10. Results — four sites, measured

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

## 11. Bugs found and fixed

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

## 12. Known limitations, in severity order

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

## 13. Bonus items

Both optional items are built and working:

- **Auto-generated cached automation from logs** — `emit_cached_automation()` produces the JSON
  directly from the log, schema-validated with `Automation.model_validate()`. No hand-writing,
  which is what makes the no-invented-locators guarantee checkable rather than a promise.
- **The iterative loop** — `run_repair_loop.py`, run → cache → rebuild → rerun → recache, with
  a strict progress rule so it terminates honestly.

Beyond the brief: the hook coverage audit, effect tracking, evidence-cited step review,
page-grounded prompt sharpening, parameterized output, and a documented negative result.

---

## 14. Questions a reviewer will ask

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
Section 12, starting with the position-dependent xpath that could silently add the wrong
product — the one known defect that can produce a wrong result while reporting success.

---

## 15. File map

| File | Contents |
|---|---|
| `browser_use/action_cache.py` | The pipeline: hook, identity, filters, review, emit |
| `browser_use/tools/service.py` | The two hook call sites (click, input) |
| `browser_use/browser/session.py`, `browser_use/dom/views.py` | Fork compatibility fixes |
| `examples/action_cache/README.md` | Introduction and index |
| `examples/action_cache/DESIGN.md` | This file |
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

## 16. In one page

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
