# Action cache — a memory layer for agentic automations

**Run 1 explores with an LLM. We record what the browser actually did. Run 2 replays it as
deterministic Playwright steps with zero model calls.**

On the Roboform task that is **68,473 tokens and 6 LLM steps, down to 0 and 0.**

An agentic Optexity node hands a natural-language task to browser-use and an LLM, which
re-derives the whole workflow from scratch on every single run. Most of what it does — type
the username, click submit — is perfectly deterministic once you know where the fields are.
This is the missing memory: watch one agentic run, learn the deterministic steps from it, and
replay them without the model.

Two rules hold everywhere in this code:

1. **No invented locators.** Every selector traces back to a logged browser action. When we
   don't have one, we say so rather than guessing.
2. **Never trust the agent's self-report.** browser-use routinely announces success it did not
   achieve. Verdicts come from the recorded trace and from measured page effects, never from
   the model's prose.

---

## Start here

| If you want… | Read |
|---|---|
| how it works, stage by stage, and why | **[DESIGN.md](DESIGN.md)** |
| the numbers, with a log line behind each one | **[METRICS.md](METRICS.md)** |
| the automations themselves, before and after | **[automations/README.md](automations/README.md)** |
| the raw evidence each locator came from | **[run_logs/README.md](run_logs/README.md)** |
| the one-off scripts used to diagnose failures | **[run_logs/diagnostics/README.md](run_logs/diagnostics/README.md)** |

## Results at a glance

| Site | Workflow | Run 1 | Run 2 | Outcome |
|---|---|---|---|---|
| Roboform | fill 4 form fields | 6 steps, 68,473 tok | 4 nodes, 0 tok, 20.0 s | complete in one pass |
| Saucedemo | login → add named product → cart | 16 steps, 173,640 tok | 6 nodes, 0 tok, 11.7 s | complete after 1 repair |
| Toolshop | search → pick 1 of 4 → add → cart | 7 steps, 77,925 tok | 4 nodes, 0 tok, 38.6 s | complete after 1 repair |
| Books to Scrape | category → book → basket | 15 steps, 149,417 tok | 2 of 3 nodes, 0 tok | **reported impossible** |

Books to Scrape is kept deliberately. The "Add to basket" button genuinely does not exist on
that page, so after two repair passes added no new locators the loop stopped and said so
instead of inventing a selector. A system that cannot say no is not trustworthy when it says
yes.

---

## Where the code is

| Path | What it is |
|---|---|
| `browser_use/action_cache.py` | The whole pipeline: hook, identity, filters, review, emit |
| `browser_use/tools/service.py` | The two hook call sites, on click and on input |
| `browser_use/browser/session.py`, `browser_use/dom/views.py` | Fork compatibility fixes |
| `examples/action_cache/run_repair_loop.py` | Loop driver: replay, repair, verify |

Companion changes live in the `optexity` fork: a local automation override so you can iterate
without a database round-trip, and per-run parameter propagation so parameterized cached
automations resolve their values. See `ACTION_CACHE.md` there.

## Layout of this folder

```text
DESIGN.md            how it works and why, stage by stage
METRICS.md           measured Run 1 vs Run 2, every row citing a log line
run_repair_loop.py   the iterative loop driver

automations/         every automation, agentic and cached, in one place
run_logs/            the evidence: traces, coverage reports, run reviews, loop logs
tools/               env var template and the inference server launcher
```

---

## How it works, in six lines

1. **Hook at execute time**, the instant the action succeeds and the live DOM node is still in
   scope. Record what was typed or clicked, the element's durable identity, its visible label,
   the URL, and a measurement of what changed on the page.
2. **Replace the index with an identity.** browser-use addresses elements as `[13]`, which dies
   on the next render. `name`, `id`, `placeholder` and shadow path survive. The index is never
   cached.
3. **Filter by stated rules**, not by eye: keep only values the task asked for, one write per
   element, the field whose label matches the task's role, and the attempt that demonstrably
   changed something.
4. **Emit a parameterized automation**, schema-validated, where every command traces to a log
   line, and write a coverage report naming anything missing.
5. **Review after the run.** One LLM call sees the task and the trace, never a locator, and
   judges each step covered, missing or unverifiable with a cited trace entry.
6. **Repair by replaying first.** Everything already known replays through Playwright with no
   LLM; the model only ever touches the broken step, prompted with the elements actually
   present on that page. Progress means a working locator appeared, not that the trace grew —
   so the loop stops honestly.

The full reasoning, including the bugs that shaped each rule, is in [DESIGN.md](DESIGN.md).

## Bonus items

Both are built. The cached automation is **generated from the logs** with Pydantic schema
validation rather than hand-assembled, and the caching runs in a **loop** that rebuilds,
reruns and recaches until nothing is missing or no further progress is possible.

## Running it

```bash
# Run 1 plus any repair passes, on any of the four sites
python examples/action_cache/run_repair_loop.py \
  --automation examples/action_cache/automations/saucedemo/test_automation.json \
  --run-dir   examples/action_cache/run_logs/saucedemo

# Run 2, deterministic, needs no LLM credentials
export LOCAL_AUTOMATION_JSON=examples/action_cache/automations/test_automation_cached.json
```

Environment variables are listed in `tools/setup_env.sh.example`. Run 2 needs none of the LLM
ones: every cached node carries `skip_prompt: true` and none is an `agentic_task`.
