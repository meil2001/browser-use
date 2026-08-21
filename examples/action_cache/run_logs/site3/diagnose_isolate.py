#!/usr/bin/env python
"""Which of the two differences makes a real click stop working?

Confounded so far: page-level `page.click(sel)` vs `page.locator(sel).first.click()`,
and whether settle() (networkidle + 1.2s) runs between steps. Four fresh sessions,
one variable each.
"""

import asyncio
import os
from pathlib import Path

BASE = Path("run_logs/site3")
os.environ["ACTION_CACHE_PATH"] = str(BASE / "replaytest_cache.jsonl")
os.environ["ACTION_DISPATCH_LOG"] = str(BASE / "replaytest_dispatch.jsonl")

SEL = '[name="add-to-cart-sauce-labs-backpack"]'
VIEWPORT = {"width": 1512, "height": 2400}


async def settle(page):
	try:
		await page.wait_for_load_state("networkidle", timeout=6000)
	except Exception:
		pass
	await page.wait_for_timeout(1200)


async def trial(use_locator: bool, use_settle: bool) -> str:
	from playwright.async_api import async_playwright

	from browser_use import BrowserProfile, BrowserSession

	session = BrowserSession(browser_profile=BrowserProfile(headless=True, window_size=VIEWPORT))
	pw = None
	try:
		await session.start()
		pw = await async_playwright().start()
		browser = await pw.chromium.connect_over_cdp(session.cdp_url)
		page = browser.contexts[0].pages[0]

		async def pause():
			if use_settle:
				await settle(page)
			else:
				await page.wait_for_load_state("networkidle")

		async def fill(sel, val):
			if use_locator:
				await page.locator(sel).first.fill(val)
			else:
				await page.fill(sel, val)

		async def click(sel):
			if use_locator:
				await page.locator(sel).first.click()
			else:
				await page.click(sel)

		await page.goto("https://www.saucedemo.com/", wait_until="load")
		await pause()
		await fill('[name="user-name"]', "standard_user")
		if use_settle:
			await settle(page)
		await fill('[name="password"]', "secret_sauce")
		if use_settle:
			await settle(page)
		await click('[name="login-button"]')
		await pause()
		await click(SEL)
		await page.wait_for_timeout(1500)
		badge = await page.locator(".shopping_cart_badge").count()
		return "WORKS" if badge else "no effect"
	finally:
		if pw is not None:
			try:
				await pw.stop()
			except Exception:
				pass
		try:
			await session.stop()
		except Exception:
			pass


async def main() -> None:
	for use_locator in (False, True):
		for use_settle in (False, True):
			how = f"{'locator().first' if use_locator else 'page.click     '}  settle={use_settle!s:5s}"
			result = await trial(use_locator, use_settle)
			print(f"{how} -> {result}")


if __name__ == "__main__":
	asyncio.run(main())
