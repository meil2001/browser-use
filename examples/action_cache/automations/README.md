# Automations

Every automation in the project, in one place. Each site has two files:

- `test_automation.json` — the **agentic** starting point: one natural-language task handed to
  browser-use, the way Optexity runs it today.
- `test_automation_cached.json` — the **deterministic** result the cache pipeline generated
  from watching that run.

Roboform's pair sits at the top level because it is the starting point named in the assignment
brief. The other three sites are in subfolders.

```text
test_automation.json                    Roboform, agentic
test_automation_cached.json             Roboform, deterministic
saucedemo/                              login → add named product → cart
books_toscrape/                         category → book → basket
toolshop/                               search → pick 1 of 4 → add → cart
```

**None of these cached files was written by hand.** Each was emitted by
`emit_cached_automation()` from the recorded trace and validated against
`optexity.schema.automation.Automation`. Every `command` string traces to a line in the
matching `run_logs/<site>/action_cache.jsonl`.

---

## What each cached automation contains

### `test_automation_cached.json` — Roboform

4 nodes, all `input_text`, all located by `name`. Parameterized as `full_name`,
`address_line_1`, `address_line_2`, `city`.

```text
[name="04fullname"]   {full_name[0]}
[name="10address1"]   {address_line_1[0]}
[name="11address2"]   {address_line_2[0]}
[name="13adr_city"]   {city[0]}
```

The last one is the interesting node. `SF` was typed into **State** on an earlier run, because
both fields accept the same string and value-matching alone cannot tell them apart. The fix was
to capture each field's visible label at hook time and score it against the role named in the
task, which is why this cache holds `13adr_city` and not `13adr_state`.

### `saucedemo/test_automation_cached.json`

6 nodes. Two typed values, four clicks, five of the six located by `name` or `id`.

```text
[name="user-name"]                        {username[0]}
[name="password"]                        {password[0]}
[name="login-button"]                    click
[name="add-to-cart-sauce-labs-backpack"] click
#shopping_cart_container                 click
xpath=…/div[1]/div[1]/div[3]/…           click
```

Credentials are the site's own published demo values, so they are parameterized rather than
hidden. This automation completes a task the agent itself never managed: browser-use clicked
"Add to cart" repeatedly without the cart ever filling, yet the locator that click yielded
works perfectly under Playwright. An ineffective action tells you about the executor that ran
it, not necessarily about the locator it produced.

### `toolshop/test_automation_cached.json`

4 nodes, one typed value parameterized as `search_box`.

```text
#search-query                             {search_box[0]}
xpath=…/app-overview/…/a[5]               click   ← the search result
#btn-add-to-cart                          click
xpath=…/app-header/nav/…/li[5]/a          click   ← the cart link
```

The hardest of the four. The product link exists only *after* the search runs, and the cart
link appears only once the cart is non-empty — so a repair pass that jumped straight to the
product URL would have been asked to click an element that wasn't on the page. Replaying the
cached steps first is what made it solvable.

Two of these locators are position-dependent xpath (`a[5]`, `li[5]`). They work today, but if
the catalog order or relevance ranking changes, `a[5]` silently clicks a **different product**
and the replay still reports success. `run_logs/toolshop/coverage.json` flags both as `weak`.
This is the most serious known limitation in the project and it is not fixed.

### `books_toscrape/test_automation_cached.json`

3 nodes, of which **one is prompt-only** (`skip_command: true`, no locator).

```text
xpath=…/aside/div[2]/ul/li/ul…            click   ← Travel category
xpath=…/section/div[2]/ol…                click   ← the book
(no command)                              prompt-only gap node
```

This is the negative result, kept on purpose. The "Add to basket" button genuinely does not
exist on that product page. Two repair passes added zero locators, the loop's no-progress rule
fired, and it emitted a gap node describing what is missing instead of fabricating a selector.
The honest artifact is the point.

---

## Running one

```bash
# Run 1: agentic, plus any repair passes the loop decides it needs
python examples/action_cache/run_repair_loop.py \
  --automation examples/action_cache/automations/saucedemo/test_automation.json \
  --run-dir   examples/action_cache/run_logs/saucedemo

# Run 2: deterministic, no LLM credentials needed
export LOCAL_AUTOMATION_JSON=examples/action_cache/automations/saucedemo/test_automation_cached.json
```

Every node in every cached file carries `skip_prompt: true`, and none is an `agentic_task`, so
Run 2 has no path to an LLM even if a locator fails to resolve. That is what makes the
zero-token claim structural rather than incidental. Measured numbers are in
[../METRICS.md](../METRICS.md).
