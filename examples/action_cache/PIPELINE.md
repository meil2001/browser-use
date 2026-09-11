# The pipeline — how one run becomes an automation

Stage by stage, from the moment browser-use clicks something to a schema-valid Optexity
automation whose every locator traces back to a logged action.

This is the *step* logic. For how several runs converge on a complete automation, see
[LOOP.md](LOOP.md). For the numbers, [METRICS.md](METRICS.md).

---

## The pipeline, stage by stage

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

**`after` is the effect probe** — see *Effect tracking* below.

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
- **step coverage** — which task steps the trace supports (from the post-run review, see [LOOP.md](LOOP.md))
- **weak** — locators resolved only by xpath
- **ineffective** — cached actions never observed to change anything
- **extra** — cached actions that don't correspond to anything the task asked for

Anything missing becomes a **gap node**: `skip_command: true`, `skip_prompt: false`. It carries
an instruction and no locator, so the LLM fills that one step, the run is recorded, and the
pipeline recompiles — this time with a real locator. That is the mechanism by which the cache
grows without anyone ever inventing a selector.

---

## Effect tracking — "executed" versus "achieved"

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

## Good vs bad steps — the explicit rules

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
