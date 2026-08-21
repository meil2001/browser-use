#!/usr/bin/env python
"""Replay site 3's cached locators with Playwright and watch the URL after each step.

Two questions at once:
  1. Does the cart click actually navigate when driven as a real Playwright click?
     Nine agent clicks never left inventory.html, and we never found out why.
  2. Can Playwright drive the browser that browser-use owns, so a replay and an
     agentic step can share one session?

No LLM anywhere in here.
"""

import asyncio
import json
import os
import re
from pathlib import Path

BASE = Path("run_logs/site3")
os.environ["ACTION_CACHE_PATH"] = str(BASE / "diag_action_cache.jsonl")
os.environ["ACTION_DISPATCH_LOG"] = str(BASE / "diag_dispatched.jsonl")

VIEWPORT = {"width": 1512, "height": 2400}


def resolve(text: str, params: dict[str, list[str]]) -> str:
	"""Turn "{username[0]}" back into the literal value the parameters hold."""

	def sub(m: re.Match[str]) -> str:
		name, idx = m.group(1), int(m.group(2))
		return params.get(name, [""])[idx]

	return re.sub(r"\{(\w+)\[(\d+)\]\}", sub, text)


async def main() -> None:
	from playwright.async_api import async_playwright

	from browser_use import BrowserProfile, BrowserSession

	automation = json.loads((BASE / "test_automation_cached.json").read_text())
	params = automation["parameters"]["input_parameters"]

	session = BrowserSession(browser_profile=BrowserProfile(headless=True, window_size=VIEWPORT))
	await session.start()
	cdp_url = session.cdp_url
	print(f"browser-use cdp url: {cdp_url}")

	pw = await async_playwright().start()
	browser = await pw.chromium.connect_over_cdp(cdp_url)
	context = browser.contexts[0]
	page = context.pages[0] if context.pages else await context.new_page()
	print("playwright attached to the same browser\n")

	await page.goto(automation["url"], wait_until="domcontentloaded")
	print(f"start: {page.url}\n")

	for i, node in enumerate(automation["nodes"]):
		action_name, action = next(iter(node["interaction_action"].items()))
		command = action.get("command")
		if not command or action.get("skip_command"):
			print(f"{i}: [gap node, no command] {action['prompt_instructions'][:70]}...")
			continue

		before = page.url
		locator = eval(f"page.{command}")  # exactly what optexity does
		try:
			if action_name == "input_text":
				await locator.fill(resolve(action["input_text"], params), timeout=5000)
				did = f"filled {resolve(action['input_text'], params)!r}"
			else:
				await locator.click(timeout=5000)
				did = "clicked"
		except Exception as e:
			print(f"{i}: FAILED {command} -> {type(e).__name__}: {str(e)[:90]}")
			continue

		await page.wait_for_timeout(1200)
		moved = "URL CHANGED" if page.url != before else "url unchanged"
		print(f"{i}: {did:34s} {command[:44]:46s} {moved} -> {page.url}")

	print(f"\nfinal url: {page.url}")
	print("cart reached:", "cart.html" in page.url)

	await pw.stop()
	await session.stop()


if __name__ == "__main__":
	asyncio.run(main())
