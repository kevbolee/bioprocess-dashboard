"""
Playwright screenshot tour of the Bioreactor SCADA Dashboard.
Saves PNGs to presentation/screenshots/.
"""

import asyncio
from pathlib import Path
from playwright.async_api import async_playwright

BASE = "http://localhost:8000"
OUT  = Path(__file__).parent / "screenshots"
OUT.mkdir(exist_ok=True)

VP = {"width": 1600, "height": 900}


async def shot(page, name: str, msg: str = ""):
    path = str(OUT / f"{name}.png")
    await page.screenshot(path=path, full_page=False)
    print(f"  [{name}] {msg}")


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(viewport=VP)
        page = await ctx.new_page()

        # ── 01  Overview (reactor grid) ─────────────────────────────────────
        print("01 Overview...")
        await page.goto(BASE, wait_until="networkidle")
        await page.wait_for_timeout(2000)
        await shot(page, "01_overview", "reactor KPI grid")

        # ── 02  Bioreactor detail (click first .br-card) ────────────────────
        print("02 Bioreactor detail...")
        card = page.locator(".br-card").first
        if await card.count():
            await card.click()
            await page.wait_for_timeout(2000)
            await shot(page, "02_reactor_detail", "single reactor detail view")
            # Back to overview via nav link
            back = page.locator("text=All Bioreactors").first
            if await back.count():
                await back.click()
                await page.wait_for_timeout(800)
        else:
            print("  (no .br-card found, skipping)")

        # ── 03  Connection log ───────────────────────────────────────────────
        print("03 Connection log...")
        await page.evaluate("App.showConnectionLog()")
        await page.wait_for_timeout(1000)
        await shot(page, "03_connection_log", "live connection event log")
        await page.evaluate("App.closeModal()")
        await page.wait_for_timeout(400)

        # ── 04  Tag browser ──────────────────────────────────────────────────
        print("04 Tag browser...")
        await page.evaluate("App.showTagBrowser()")
        await page.wait_for_timeout(1000)
        await shot(page, "04_tag_browser", "OPC UA tag browser")
        await page.evaluate("App.closeModal()")
        await page.wait_for_timeout(400)

        # ── 05  Analytical > BioHT tab ───────────────────────────────────────
        print("05 BioHT tab...")
        await page.evaluate("App.showAnalytical()")
        await page.wait_for_timeout(500)
        await page.evaluate("App._switchAnalTab('bioht')")
        await page.wait_for_timeout(2500)
        await shot(page, "05_bioht_tab", "BioHT / MAST analytical tab")

        # ── 06  BioHT – first sample selected ───────────────────────────────
        print("06 BioHT first sample...")
        first_bh = page.locator("#bioht-sample-select option").nth(1)
        if await first_bh.count():
            val = await first_bh.get_attribute("value")
            await page.select_option("#bioht-sample-select", val)
            await page.wait_for_timeout(1500)
            await shot(page, "06_bioht_sample", "BioHT sample card with source badge")
        else:
            await shot(page, "06_bioht_sample", "no BioHT samples in DB")

        # ── 07  BioHT – pick an analyte and show chart ───────────────────────
        print("07 BioHT chart...")
        analyte_opt = page.locator("#bioht-analyte-select option").nth(1)
        if await analyte_opt.count():
            val = await analyte_opt.get_attribute("value")
            await page.select_option("#bioht-analyte-select", val)
            await page.wait_for_timeout(1200)
            await shot(page, "07_bioht_chart", "BioHT trend chart")

        # ── 08  BioHT consolidate checkbox ───────────────────────────────────
        print("08 BioHT consolidate...")
        cb = page.locator("#bioht-consolidate")
        if await cb.count() and not await cb.is_checked():
            await cb.check()
            await page.wait_for_timeout(800)
        await shot(page, "08_bioht_consolidate", "analyte consolidation active")

        # ── 09  Nova Flex2 tab ───────────────────────────────────────────────
        print("09 Nova tab...")
        await page.evaluate("App._switchAnalTab('nova')")
        await page.wait_for_timeout(2500)
        await shot(page, "09_nova_tab", "Nova Flex2 OPC analytical tab")

        # ── 10  Nova – first sample selected ─────────────────────────────────
        print("10 Nova first sample...")
        first_nova = page.locator("#nova-sample-select option").nth(1)
        if await first_nova.count():
            val = await first_nova.get_attribute("value")
            await page.select_option("#nova-sample-select", val)
            await page.wait_for_timeout(1500)
            await shot(page, "10_nova_sample", "Nova sample analyte card grid")
        else:
            await shot(page, "10_nova_sample", "no Nova samples in DB")

        # ── 11  Nova – pick an analyte and show chart ────────────────────────
        print("11 Nova chart...")
        nova_analyte = page.locator("#nova-analyte-select option").nth(1)
        if await nova_analyte.count():
            val = await nova_analyte.get_attribute("value")
            await page.select_option("#nova-analyte-select", val)
            await page.wait_for_timeout(1200)
            await shot(page, "11_nova_chart", "Nova trend chart")

        await browser.close()
        saved = list(OUT.glob("*.png"))
        print(f"\nDone — {len(saved)} screenshots in {OUT}")
        for f in sorted(saved):
            print(f"  {f.name}")


asyncio.run(main())
