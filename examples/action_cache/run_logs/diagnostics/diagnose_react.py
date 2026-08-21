#!/usr/bin/env python
"""Is the React app actually alive in this browser?

Both the add-to-cart click and the cart click did nothing, and the cart anchor has no
href — so both depend on React handlers. If the bundle failed or never hydrated, every
"click failure" on this site has one cause and none of them are our design's fault.
"""

import asyncio
import os
from pathlib import Path

BASE = Path("run_logs/site3")
os.environ["ACTION_CACHE_PATH"] = str(BASE / "diag_action_cache.jsonl")
os.environ["ACTION_DISPATCH_LOG"] = str(BASE / "diag_dispatched.jsonl")


async def main() -> None:
	from playwright.async_api import async_playwright

	from browser_use import BrowserProfile, BrowserSession

	session = BrowserSession(
		browser_profile=BrowserProfile(headless=True, window_size={"width": 1512, "height": 2400})
	)
	await session.start()
	pw = await async_playwright().start()
	browser = await pw.chromium.connect_over_cdp(session.cdp_url)
	page = browser.contexts[0].pages[0]

	console: list[str] = []
	failed: list[str] = []
	page.on("console", lambda m: console.append(f"[{m.type}] {m.text[:160]}"))
	page.on("pageerror", lambda e: console.append(f"[pageerror] {str(e)[:160]}"))
	page.on("requestfailed", lambda r: failed.append(f"{r.failure} {r.url[:110]}"))

	await page.goto("https://www.saucedemo.com/", wait_until="load")
	await page.wait_for_load_state("networkidle")
	await page.fill('[name="user-name"]', "standard_user")
	await page.fill('[name="password"]', "secret_sauce")
	await page.click('[name="login-button"]')
	await page.wait_for_load_state("networkidle")
	print(f"after login: {page.url}")

	react = await page.evaluate("""() => {
		const btn = document.querySelector('[name="add-to-cart-sauce-labs-backpack"]');
		if (!btn) return {found: false};
		const keys = Object.keys(btn);
		return {
			found: true,
			text: btn.innerText.trim(),
			reactKeys: keys.filter(k => k.startsWith('__react')),
			allKeys: keys.slice(0, 6),
		};
	}""")
	print(f"add-to-cart button: {react}")

	before = await page.locator('[name="add-to-cart-sauce-labs-backpack"]').inner_text()
	await page.click('[name="add-to-cart-sauce-labs-backpack"]')
	await page.wait_for_timeout(2000)
	after_count = await page.locator('[name="remove-sauce-labs-backpack"]').count()
	badge = await page.locator(".shopping_cart_badge").count()
	print(f"button before={before!r}  turned into Remove: {bool(after_count)}  badge: {bool(badge)}")

	storage = await page.evaluate("() => JSON.stringify(window.localStorage)")
	print(f"localStorage: {storage[:200]}")

	print(f"\nfailed requests ({len(failed)}):")
	for f in failed[:10]:
		print(f"  {f}")
	print(f"\nconsole ({len(console)}):")
	for c in console[:15]:
		print(f"  {c}")

	await pw.stop()
	await session.stop()


if __name__ == "__main__":
	asyncio.run(main())
