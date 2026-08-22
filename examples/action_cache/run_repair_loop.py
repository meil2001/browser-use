#!/usr/bin/env python
"""Iterate a task to a complete cache: run -> review -> replay + sharper prompt -> ...

One command instead of "Run 1, look at it, hand-write a gap prompt, run again".

The invariant (see STEPS.md): a repair never teleports and never re-reasons about
solved ground. Everything we already hold a locator for is replayed through
Playwright with **no LLM at all**, and the LLM is handed exactly one thing — the
step that is actually broken. Replaying earns the session, the login and the
correct page as a side effect, which a saved URL cannot.

    python run_repair_loop.py --automation test_automation.json --run-dir run_logs

It stops on evidence, not on optimism: a step counts as done only when a recorded
action proves it, and progress means *a new locator appeared*, never "the log got
longer" — a replayed prefix lengthens the log every single time.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import socket
from pathlib import Path
from typing import Any

# A short viewport is a generic cause of "the agent never saw the button", so give
# every pass a tall one. Costs nothing headless.
VIEWPORT = {"width": 1512, "height": 2400}

# Clicking a JS app before it hydrates lands on the element and silently does
# nothing: no error, no effect. That is what made saucedemo look broken — nine
# clicks on a cart link whose handler was not attached yet. So after every replayed
# action, wait for the network to go quiet and then let handlers settle.
SETTLE_MS = 1200
SETTLE_TIMEOUT_MS = 6000
ACTION_TIMEOUT_MS = 8000

_PARAM_RE = re.compile(r"\{(\w+)\[(\d+)\]\}")


def free_port() -> int:
	"""A port for Chromium's debugging endpoint, so browser-use can join later."""
	with socket.socket() as s:
		s.bind(("127.0.0.1", 0))
		return int(s.getsockname()[1])


def configure_paths(run_dir: Path, automation: Path) -> None:
	"""Point every stage at this run's directory before anything reads them."""
	run_dir.mkdir(parents=True, exist_ok=True)
	os.environ["AUTOMATION_JSON"] = str(automation)
	os.environ["ACTION_CACHE_PATH"] = str(run_dir / "action_cache.jsonl")
	os.environ["ACTION_DISPATCH_LOG"] = str(run_dir / "dispatched_events.jsonl")
	os.environ["RUN_REVIEW_PATH"] = str(run_dir / "run_review.json")
	os.environ["REPAIR_ATTEMPTS_PATH"] = str(run_dir / "repair_attempts.json")


def declared_start_url(automation: Path) -> str:
	"""The starting URL the automation file declares, if any.

	Without this, pass 0 depends on the agent inferring the site from the task
	wording — which worked on books.toscrape only because the model happened to
	recognise it. The automation file already states where to start; use it.
	"""
	try:
		return str(json.loads(automation.read_text(encoding="utf-8")).get("url") or "")
	except Exception:
		return ""


def resolve_params(text: str, params: dict[str, list[str]]) -> str:
	"""Turn "{username[0]}" back into the literal the parameters hold."""

	def sub(m: re.Match[str]) -> str:
		values = params.get(m.group(1)) or [""]
		index = int(m.group(2))
		return values[index] if index < len(values) else ""

	return _PARAM_RE.sub(sub, text)


async def settle(page: Any) -> None:
	"""Let the page finish reacting before the next action."""
	try:
		await page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
	except Exception:
		pass  # a busy page is normal; the fixed wait below still applies
	await page.wait_for_timeout(SETTLE_MS)


async def replay_prefix(page: Any, automation: dict[str, Any]) -> dict[str, Any]:
	"""Execute every cached command through Playwright. No LLM, no recording.

	`eval(f"page.{command}")` is exactly how optexity resolves these strings
	(`Browser.get_locator_from_command`), so a replay here runs the same thing a
	real Run 2 will run — a rehearsal, not an approximation.

	Nothing here is recorded: the hook listens on browser-use's event bus, and
	Playwright drives the page directly. That is deliberate. It keeps the trace
	free of duplicated prefix actions, which in turn lets "a new record appeared"
	mean "genuinely new behaviour".
	"""
	params = automation.get("parameters", {}).get("input_parameters", {})
	done, failures = 0, []

	await page.goto(automation["url"], wait_until="load")
	await settle(page)

	for i, node in enumerate(automation.get("nodes", [])):
		action_name, action = next(iter(node["interaction_action"].items()))
		command = action.get("command")
		if not command or action.get("skip_command"):
			continue
		try:
			locator = eval(f"page.{command}")  # noqa: S307 - our own recorded command
			if action_name == "input_text":
				value = resolve_params(action.get("input_text") or "", params)
				await locator.fill(value, timeout=ACTION_TIMEOUT_MS)
			else:
				await locator.click(timeout=ACTION_TIMEOUT_MS)
			await settle(page)
			done += 1
		except Exception as e:
			failures.append(f"node {i} ({command[:44]}): {type(e).__name__}")

	return {"replayed": done, "failures": failures, "url": page.url}


async def repair_pass(
	automation: dict[str, Any],
	task: str,
	step: dict[str, Any],
	attempts: list[dict[str, Any]],
	max_steps: int,
) -> dict[str, Any]:
	"""Replay what we know, then let the LLM attempt only the broken step.

	The order matters: the element inventory is read *after* the replay, from the
	live session. Reading it beforehand by URL is what fed saucedemo's login page
	into a prompt about a shopping cart.
	"""
	from playwright.async_api import async_playwright

	from browser_use import Agent, BrowserProfile, BrowserSession
	from browser_use.action_cache import improve_repair_prompt, page_landmarks, target_present
	from optexity.inference.models.chat_litellm import build_agent_llm

	# Chromium is launched by Playwright and only *later* joined by browser-use. The
	# order is not cosmetic: with a browser-use session already attached, real mouse
	# clicks stop reaching React handlers on saucedemo — verified against a pure
	# Playwright control, which passes the same sequence every time. So the replay
	# runs clean, exactly as optexity's Run 2 will, and the agent joins afterwards.
	port = free_port()
	pw = None
	browser = None
	session = None
	out: dict[str, Any] = {"replay": None, "prompt": None, "source": None, "repeated": False}
	try:
		pw = await async_playwright().start()
		browser = await pw.chromium.launch(
			headless=True, args=[f"--remote-debugging-port={port}"]
		)
		page = await browser.new_page(viewport=VIEWPORT)

		out["replay"] = await replay_prefix(page, automation)
		print(
			f"  replayed {out['replay']['replayed']} cached step(s) with no LLM "
			f"-> {out['replay']['url']}"
		)
		for failure in out["replay"]["failures"]:
			print(f"    replay failure: {failure}")

		# Now join the replayed browser, so the agent inherits the session, the login
		# and the page the replay actually reached.
		session = BrowserSession(
			cdp_url=f"http://127.0.0.1:{port}",
			browser_profile=BrowserProfile(headless=True, window_size=VIEWPORT),
		)
		await session.start()
		state = await session.get_browser_state_summary()
		landmarks = page_landmarks(state.dom_state.selector_map)
		target = step.get("expected_target") or step["description"]
		out["landmarks"] = len(landmarks)
		out["target_seen"] = target_present(target, landmarks)
		print(f"  page offers {len(landmarks)} labelled element(s); {target!r} present: {out['target_seen']}")
		for m in [m for m in landmarks if target.lower() in m["label"].lower()][:5]:
			note = f" — appears {m['count']}x, so the prompt must disambiguate" if int(m["count"]) > 1 else ""
			print(f"    <{m['tag']}> {m['label']!r}{note}")

		sharpened = improve_repair_prompt(
			task, step, attempts, landmarks=landmarks, baseline=step["repair_prompt"]
		)
		# The template fallback is a downgrade from the review's own wording, which at
		# least saw the task. On a first attempt, only take a rewrite the model made.
		if not attempts and sharpened["source"] != "llm":
			sharpened = {
				"prompt": step["repair_prompt"],
				"source": step["repair_prompt_source"],
				"repeated": False,
			}
		out.update(sharpened)
		if sharpened["repeated"]:
			return out

		print(f"  prompt ({sharpened['source']}): {sharpened['prompt']}")
		agent = Agent(
			task=sharpened["prompt"],
			llm=build_agent_llm(),
			browser_session=session,
			max_actions_per_step=1,
		)
		history = await agent.run(max_steps=max_steps)
		usage = history.usage
		out.update(
			{
				"final_result": history.final_result(),
				"is_successful": history.is_successful(),
				"agent_steps": len(history.history),
				"tokens": usage.total_tokens if usage is not None else None,
			}
		)
		return out
	finally:
		for closer in (
			session.stop if session is not None else None,
			browser.close if browser is not None else None,
			pw.stop if pw is not None else None,
		):
			if closer is not None:
				with contextlib.suppress(Exception):
					await closer()


async def verify_replay(automation: dict[str, Any]) -> dict[str, Any]:
	"""Run the emitted cache the way Run 2 will: pure Playwright, no LLM, no agent.

	Worth more than any verdict a model can offer about the log, because it is the
	actual deliverable being executed. On saucedemo this is what revealed that the
	cache completes a task the agentic run could not.
	"""
	from playwright.async_api import async_playwright

	pw = await async_playwright().start()
	browser = None
	try:
		browser = await pw.chromium.launch(headless=True)
		page = await browser.new_page(viewport=VIEWPORT)
		return await replay_prefix(page, automation)
	finally:
		if browser is not None:
			with contextlib.suppress(Exception):
				await browser.close()
		with contextlib.suppress(Exception):
			await pw.stop()


async def run_agent(task: str, initial_url: str | None, max_steps: int) -> dict[str, Any]:
	"""One fully agentic pass, used for the initial exploration only."""
	from browser_use import Agent, BrowserProfile
	from optexity.inference.models.chat_litellm import build_agent_llm

	initial_actions = [{"navigate": {"url": initial_url, "new_tab": False}}] if initial_url else None
	agent = Agent(
		task=task,
		llm=build_agent_llm(),
		browser_profile=BrowserProfile(headless=True, window_size=VIEWPORT),
		max_actions_per_step=1,
		initial_actions=initial_actions,
	)
	history = await agent.run(max_steps=max_steps)
	usage = history.usage
	return {
		"final_result": history.final_result(),
		"is_successful": history.is_successful(),
		"agent_steps": len(history.history),
		"tokens": usage.total_tokens if usage is not None else None,
	}


def print_review(review: dict[str, Any], heading: str) -> None:
	summary = review["summary"]
	print(f"\n--- {heading} ---")
	if not summary["available"]:
		print("  step check DID NOT RUN (no LLM) — this is not a pass")
		return
	for i, step in enumerate(review["steps"], 1):
		where = (
			f"trace[{step['evidence']}] {step['evidence_label']!r}"
			if step["evidence"] is not None
			else "—"
		)
		print(f"  {i}. [{step['verdict']:>12}] {step['description']}  evidence: {where}")
	print(
		f"  {summary['covered']}/{summary['steps_total']} covered, "
		f"{summary['missing']} missing, {summary['unverifiable']} unverifiable, "
		f"{summary['actions_recorded']} actions recorded"
	)
	for conflict in summary["conflicts"]:
		print(f"  CONFLICT: {conflict}")


async def main() -> None:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--automation", default="test_automation.json")
	parser.add_argument("--run-dir", default="run_logs")
	parser.add_argument("--max-iterations", type=int, default=5)
	parser.add_argument("--first-run-max-steps", type=int, default=15)
	parser.add_argument("--repair-max-steps", type=int, default=6)
	args = parser.parse_args()

	logging.basicConfig(level=logging.INFO)
	logging.getLogger("LiteLLM").setLevel(logging.WARNING)
	run_dir = Path(args.run_dir)
	configure_paths(run_dir, Path(args.automation))

	from browser_use.action_cache import (
		align_cache,
		annotate_effects,
		cache_path,
		emit_cached_automation,
		load_agentic_task,
		load_repair_attempts,
		load_run_review,
		locate_cache,
		missing_steps,
		read_jsonl,
		record_repair_attempt,
		slice_cache,
	)

	task = load_agentic_task()
	if not task:
		raise SystemExit(f"no agentic_task found in {args.automation}")
	print(f"task: {task}")

	def trace_size() -> int:
		return len(read_jsonl(cache_path()))

	def identities() -> set[str]:
		"""Elements we hold a locator for *and* saw do something.

		A locator alone is not progress. On saucedemo the agent recorded ten clicks
		on "Add to cart" that left the cart empty; counting those as progress is what
		kept the old loop spending money. Actions we could not measure still count —
		unmeasured is not the same as useless.
		"""
		found = set()
		for row in annotate_effects(read_jsonl(cache_path())):
			ident = row.get("identity") or {}
			if not ident.get("value"):
				continue
			if (row.get("effect") or {}).get("verdict") in {"no_observed_effect", "no_navigation"}:
				continue
			found.add(f"{row.get('action')}:{ident.get('by')}={ident.get('value')}")
		return found

	def compile_cache() -> dict[str, Any] | None:
		"""align -> slice -> locate -> emit. Returns the automation, or None if empty."""
		aligned = run_dir / "action_cache_aligned.jsonl"
		sliced = run_dir / "action_cache_sliced.jsonl"
		located = run_dir / "action_cache_located.jsonl"
		align_cache(dst=aligned)
		slice_cache(src=aligned, dst=sliced)
		if not locate_cache(src=sliced, dst=located):
			return None
		return emit_cached_automation(
			src=located,
			dst=run_dir / "test_automation_cached.json",
			coverage_dst=run_dir / "coverage.json",
		)

	# Pass 0 is the original exploration, and only runs if nothing was recorded
	# yet — so re-running this script continues a run instead of restarting it.
	if trace_size() == 0:
		start = declared_start_url(Path(args.automation))
		print("\n=== pass 0: agentic exploration ===")
		print(f"  start url: {start or '(none declared — the agent must find the site itself)'}")
		result = await run_agent(task, start or None, args.first_run_max_steps)
		print(
			f"  agent steps={result['agent_steps']} self-reported success={result['is_successful']} "
			f"tokens={result['tokens']} actions recorded={trace_size()}"
		)
	else:
		print(f"\n=== pass 0 skipped: {trace_size()} actions already recorded ===")

	no_progress = 0
	for iteration in range(1, args.max_iterations + 1):
		review = load_run_review(force=True)
		print_review(review, f"review before iteration {iteration}")

		holes = missing_steps(review)
		if not holes:
			print("\nno missing steps left — stopping")
			break

		automation = compile_cache()
		if automation is None:
			print("\nnothing locatable recorded yet — nothing to replay, stopping")
			break

		step = holes[0]
		attempts = load_repair_attempts().get(step["description"], [])
		before = identities()

		print(f"\n=== iteration {iteration}: repairing {step['description']!r} ===")
		result = await repair_pass(automation, task, step, attempts, args.repair_max_steps)

		if result["repeated"]:
			print("  stopping: the sharpened prompt matches one already tried — no new idea to test")
			break

		gained = identities() - before
		print(
			f"  agent steps={result.get('agent_steps')} "
			f"self-reported success={result.get('is_successful')} "
			f"tokens={result.get('tokens')} new locators={len(gained)}"
		)
		for g in gained:
			print(f"    + {g}")

		record_repair_attempt(
			step["description"],
			{
				"iteration": iteration,
				"prompt": result["prompt"],
				"prompt_source": result["source"],
				"replayed_steps": (result.get("replay") or {}).get("replayed"),
				"replay_failures": (result.get("replay") or {}).get("failures"),
				"resumed_at": (result.get("replay") or {}).get("url"),
				"agent_report": result.get("final_result"),
				"agent_claimed_success": result.get("is_successful"),
				"new_records": len(gained),
				"tokens": result.get("tokens"),
				"page_elements_seen": result.get("landmarks"),
				"target_seen_in_page_elements": result.get("target_seen"),
			},
		)

		# A pass that produced no new locator taught us nothing about the page.
		# Two in a row means further attempts are just spending money.
		if not gained:
			no_progress += 1
			if no_progress >= 2:
				print("\nstopping: two attempts in a row produced no new locator")
				break
		else:
			no_progress = 0
	else:
		print(f"\nstopping: hit the {args.max_iterations}-iteration cap")

	final = load_run_review(force=True)
	print_review(final, "final review")

	automation = compile_cache()
	if automation is None:
		print("\nnothing locatable was recorded — no automation emitted")
		return
	gaps = sum(
		1
		for node in automation["nodes"]
		for action in node["interaction_action"].values()
		if action.get("skip_command")
	)
	print(
		f"\nemitted {len(automation['nodes'])} nodes "
		f"({len(automation['nodes']) - gaps} replayable, {gaps} still prompt-only) "
		f"-> {run_dir / 'test_automation_cached.json'}"
	)

	check = await verify_replay(automation)
	print(
		f"\nreplay check: {check['replayed']}/{len(automation['nodes']) - gaps} "
		f"cached step(s) executed, {len(check['failures'])} failed -> {check['url']}"
	)
	for failure in check["failures"]:
		print(f"  {failure}")


if __name__ == "__main__":
	asyncio.run(main())
