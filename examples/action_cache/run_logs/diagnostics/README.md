# Diagnostics

One-off scripts written to answer a specific question when a run failed in a way I couldn't
explain. They are not part of the pipeline and nothing imports them. They're kept because the
reasoning behind two of the design's most important rules lives here, and "I investigated
before I changed the design" is worth being able to prove.

> **Note on paths.** These scripts hardcode the old folder names (`run_logs/site2`,
> `run_logs/site3`) from before the logs were renamed to `books_toscrape/` and `saucedemo/`.
> They are preserved as they were actually run rather than retro-edited, so adjust the paths if
> you want to re-run one.

---

## The saucedemo investigation

Four scripts, in the order I wrote them. The problem: browser-use clicked "Add to cart" nine
times, the hook faithfully recorded nine successful clicks, and the cart stayed empty.

**`diagnose_replay.py`** — replay the cached locators with Playwright and print the URL after
every step. Two questions at once: does the cart click navigate when driven as a real Playwright
click, and can Playwright drive a browser that browser-use owns? Answer to the second was yes,
which is what made the replay-first architecture possible.

**`diagnose_cart.py`** — inspect the header markup and try each candidate element. The point was
to separate two explanations that look identical from the outside: are we clicking the wrong
element, or clicking the right one and it simply doesn't navigate? It found the cart anchor has
no `href` at all, so any navigation must come from a React handler.

**`diagnose_react.py`** — given that both failing clicks depend on React handlers, is the app
even hydrated? If the bundle never hydrated, every click failure on the site has a single cause
and none of them are the cache design's fault. With proper waiting the add-to-cart click did
work, which pointed at hydration timing rather than a broken app.

**`diagnose_isolate.py`** — two variables were still confounded: `page.click(sel)` versus
`page.locator(sel).first.click()`, and whether a `settle()` (networkidle plus 1.2s) ran between
steps. Four fresh sessions, one variable changed at a time.

**What this chain produced.** It is the reason actions now record what they *achieved* rather
than only that they *executed*, and the reason the replay runs on a Playwright-owned browser
that browser-use attaches to afterwards rather than the other way round. It also corrected an
overclaim of mine: I had concluded browser-use's clicks never reach React on this site, and a
later clean run showed 11 dispatched clicks with no blind spots and a cart badge that did
appear. The supportable claim is that they're *intermittent*, consistent with hydration timing.

## books.toscrape.com

**`run_agent.py`** — Run 1 for the second site as a plain `browser_use.Agent` script rather than
through the Optexity inference server. Worth knowing that this works: the hook lives in
`browser_use/tools/service.py` and fires regardless of who calls the action functions, so the
cache populates identically either way.

**`compile.py`** — compile that site's run with every path redirected through environment
variables, `AUTOMATION_JSON` included. It exists because the alternative was swapping the
Roboform `test_automation.json` in and out of the working directory between runs, which is
exactly the kind of manual step that silently produces a cache for the wrong task.
