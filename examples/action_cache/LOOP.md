# The loop — how runs converge on a complete automation

One agentic run rarely covers the whole task. This is how the system notices what is missing,
writes a sharper instruction for exactly that gap, and retries without paying to rediscover
what it already knows.

This is the *loop* logic. For how a single run is turned into an automation, see
[PIPELINE.md](PIPELINE.md). For the numbers, [METRICS.md](METRICS.md).

---

## The invariant — how every retry is run

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

## The post-run review — catching missing *steps*, not just missing values

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

## The iterative repair loop

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
