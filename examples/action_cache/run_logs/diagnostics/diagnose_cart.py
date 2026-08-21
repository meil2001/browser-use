#!/usr/bin/env python
"""Why doesn't the cart click navigate? Inspect the header, then try each candidate.

Nine agent clicks and two Playwright clicks all stayed on inventory.html. Either we
are clicking the wrong element, or clicking the right one does not navigate. This
tells us which, without guessing.
"""

import asyncio
import os
from pathlib import Path

BASE = Path("run_logs/site3")
os.environ["ACTION_CACHE_PATH"] = str(BASE / "diag_action_cache.jsonl")
os.environ["ACTION_DISPATCH_LOG"] = str(BASE / "diag_dispatched.jsonl")

VIEWPORT = {"width": 1512, "height": 2400}


async def main() -> None:
	from playwright.async_api import async_playwright

	from browser_use import BrowserProfile, BrowserSession

	session = BrowserSession(browser_profile=BrowserProfile(headless=True, window_size=VIEWPORT))
	await session.start()
	pw = await async_playwright().start()
	browser = await pw.chromium.connect_over_cdp(session.cdp_url)
	page = browser.contexts[0].pages[0]

	await page.goto("https://www.saucedemo.com/", wait_until="domcontentloaded")
	await page.fill('[name="user-name"]', "standard_user")
	await page.fill('[name="password"]', "secret_sauce")
	await page.click('[name="login-button"]')
	await page.wait_for_timeout(1500)
	print(f"logged in -> {page.url}\n")

	await page.click('[name="add-to-cart-sauce-labs-backpack"]')
	await page.wait_for_timeout(800)
	badge = await page.locator(".shopping_cart_badge").count()
	badge_text = await page.locator(".shopping_cart_badge").inner_text() if badge else "(none)"
	print(f"cart badge present: {bool(badge)} text={badge_text!r}  <- did add-to-cart work?\n")

	print("--- every anchor in the header ---")
	info = await page.evaluate("""() => {
		const out = [];
		document.querySelectorAll('a').forEach(a => {
			const r = a.getBoundingClientRect();
			out.push({
				cls: a.className,
				href: a.getAttribute('href'),
				text: (a.innerText || '').trim().slice(0, 24),
				x: Math.round(r.x), y: Math.round(r.y),
				w: Math.round(r.width), h: Math.round(r.height),
			});
		});
		return out.slice(0, 12);
	}""")
	for a in info:
		print(f"  class={a['cls']!r:26s} href={a['href']!r:22s} text={a['text']!r:16s} box={a['w']}x{a['h']} at ({a['x']},{a['y']})")

	print("\n--- what does our cached xpath resolve to? ---")
	xp = "html/body/div[1]/div/div/div[1]/div[1]/div[3]/a"
	loc = page.locator(f"xpath={xp}")
	print(f"  count={await loc.count()}  class={await loc.first.get_attribute('class')!r}  href={await loc.first.get_attribute('href')!r}")

	for label, selector in [
		("a.shopping_cart_link", "a.shopping_cart_link"),
		("#shopping_cart_container", "#shopping_cart_container"),
	]:
		before = page.url
		try:
			el = page.locator(selector).first
			box = await el.bounding_box()
			await el.click(timeout=4000)
			await page.wait_for_timeout(1500)
			print(f"\nclick {label}: box={box} -> {page.url} ({'MOVED' if page.url != before else 'no navigation'})")
		except Exception as e:
			print(f"\nclick {label}: {type(e).__name__}: {str(e)[:80]}")
		if page.url != before:
			await page.go_back(wait_until="domcontentloaded")
			await page.wait_for_timeout(800)

	# Does the destination even work when reached directly?
	await page.goto("https://www.saucedemo.com/cart.html", wait_until="domcontentloaded")
	print(f"\ndirect goto cart.html -> {page.url}")
	items = await page.locator(".cart_item").count()
	print(f"items in cart: {items}")

	await pw.stop()
	await session.stop()


if __name__ == "__main__":
	asyncio.run(main())
