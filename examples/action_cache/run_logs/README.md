# Run logs — the evidence

This folder exists to make one claim checkable: **every locator in every cached automation came
from a real recorded browser action, not from a model's imagination and not from my hands.**

Each site folder holds the full pipeline, one file per stage, so if a step went missing you can
point at the exact stage that dropped it. That is the difference between "the cache is wrong"
and "Stage 3 dropped it because the typed value wasn't on the task's list."

> **Why the logs say `site2` and `site3`.** These folders were called `site1`…`site4` while I was
> working and were renamed afterwards to the site they belong to. The `.log` files still print the
> old paths, because they are captured stdout and editing them would mean editing the evidence.
> `site1` is `roboform`, `site2` is `books_toscrape`, `site3` is `saucedemo`, `site4` is `toolshop`.

---

## The site folders

| Folder | Site | Task |
|---|---|---|
| `roboform/` | roboform.com/filling-test-all-fields | fill 4 form fields |
| `saucedemo/` | saucedemo.com | log in, add a named product, open the cart |
| `toolshop/` | practicesoftwaretesting.com | search, open 1 result of 4, add it, open the cart |
| `books_toscrape/` | books.toscrape.com | Travel category, open a book, add to basket |
| `roboform_first_run/` | roboform.com | the earliest Roboform run, kept for comparison |
| `inference/` | — | Optexity inference server logs for Run 1 and Run 2 |
| `archive/` | — | superseded runs, kept to show what changed and why |
| `diagnostics/` | — | one-off scripts written to chase specific failures |

## What each file is

| File | Stage | Contents |
|---|---|---|
| `action_cache.jsonl` | 1 | Raw executed actions, one JSON object per line |
| `action_cache_aligned.jsonl` | 3 | Only actions the task actually asked for |
| `action_cache_sliced.jsonl` | 4 | One record per element: retries and corrections dropped |
| `action_cache_located.jsonl` | 5 | Same records, now carrying a Playwright command |
| `coverage.json` | 6.5 | What's missing, what's weak, what had no observed effect |
| `run_review.json` | — | The post-run review: per-step verdict plus cited evidence |
| `dispatched_events.jsonl` | — | Hook audit: everything the event bus dispatched |
| `hook_coverage.json` | — | Dispatched vs recorded, so blind spots are named not hidden |
| `repair_attempts.json` | — | Every repair prompt tried, and whether it produced a locator |
| `loop*.log` | — | Full stdout of a loop run: steps, tokens, verdicts |

## Reading one record

This is the real line that produced the Roboform city node, from
`roboform/action_cache.jsonl`:

```json
{
  "action": "input", "index": 17, "text": "SF",
  "url": "https://www.roboform.com/filling-test-all-fields",
  "after": {
    "title": "RoboForm Tutorials - Form Filler…", "interactive": 114, "text_len": 2544,
    "target": { "present": true, "value": "SF", "name": "13adr_city" }
  },
  "tag": "input",
  "attributes": { "type": "text", "size": "20", "name": "13adr_city" },
  "xpath": "html/body/div[2]/form/div/div[1]/div[10]/div[2]/input",
  "label": "City",
  "shadow_hosts": [],
  "identity": { "by": "name", "value": "13adr_city" }
}
```

Four things in there are doing real work:

- **`identity`** is what replaces `index: 17`. The index is "the seventeenth interactive thing
  on screen right now" and dies on the next render; `name="13adr_city"` survives. The index is
  recorded for debugging and never cached.
- **`label: "City"`** is why this node is correct. `SF` is a legal value for both City and
  State, so matching on the value alone cannot tell them apart — and an earlier run did cache
  State. Capturing the label at hook time lets the filter compare the field's label against the
  role named in the task instead of comparing strings.
- **`after.target.value: "SF"`** is proof the typing actually landed, read back from the field
  after the action. This is the difference between recording that an action *ran* and recording
  that it *worked*.
- **`after.title` / `interactive` / `text_len`** are a cheap page fingerprint, compared against
  the previous action's reading to judge whether anything changed at all.

Follow that record forward: it appears in `action_cache_aligned.jsonl` because `SF` is on the
task's list, survives into `action_cache_sliced.jsonl` as the winner for that field, gains
`locator("[name=\"13adr_city\"]").first` in `action_cache_located.jsonl`, and becomes node 3 of
`../automations/test_automation_cached.json`.

## Watching the filter work

Line counts per stage show what was thrown away and where:

```text
roboform    4 raw →  4 aligned →  4 sliced →  4 located    (a clean run, nothing to drop)
saucedemo  13 raw → 13 aligned →  6 sliced →  6 located    (7 duplicates collapsed at Stage 4)
```

Saucedemo's seven dropped records are the same buttons clicked repeatedly — mostly "Add to
cart", which browser-use clicked over and over while the cart stayed empty. Stage 4 keeps one
record per element, preferring the attempt that was actually observed to change something.

## A note on `archive/`

- `archive/saucedemo_before_effects/` — the same site before actions carried an `effect`
  verdict. Comparing its `coverage.json` against `saucedemo/coverage.json` shows what effect
  measurement bought.
- `archive/books_toscrape_first_finding/` — the run that first established the "Add to basket"
  button is genuinely absent.

These are kept because a claim like "measuring effects changed the outcome" should be checkable
against the run that came before it, not just asserted.

## Caveats worth knowing

- `saucedemo/probe_settle*.jsonl` are scratch files from measuring how long a framework takes
  to re-render after a click. Not part of any automation.
- `inference/` logs come from the Optexity server, which is long-lived, so their first and last
  timestamps span far more than a single run. They're useful for the ReAct step count, not for
  wall time. See [../METRICS.md](../METRICS.md) for what is and isn't measurable.
- Saucedemo's credentials appear in these logs in plain text. They are the site's own published
  demo values, deliberately chosen so the login flow could be demonstrated without handling a
  real secret.
