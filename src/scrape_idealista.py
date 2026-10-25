"""Scrape Idealista listings starting from a supplied search or listing URL.

Examples:
    python src/scrape_idealista.py "https://www.idealista.com/venta-viviendas/madrid-madrid/"
    python src/scrape_idealista.py "https://www.idealista.com/inmueble/123456789/" --max-listings 100
    python src/scrape_idealista.py --address "Calle de Alcalá, Madrid" --max-listings 100
"""

import argparse
import asyncio
import json
import random
import re
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright


SRC_DIR = Path(__file__).resolve().parent
ROOT_DIR = SRC_DIR.parent

DEFAULT_DATA_FILE = ROOT_DIR / "data" / "scraped_listings_madrid.json"
EXTRACTOR_FILE = SRC_DIR / "extractor.js"


def parse_args():
    parser = argparse.ArgumentParser(description="Extract Idealista listing data into a JSON file.")
    parser.add_argument(
        "initial_url",
        nargs="?",
        default="https://www.idealista.com/",
        help="Idealista search-results URL or an individual /inmueble/<id>/ listing URL.",
    )
    parser.add_argument(
        "--max-listings",
        type=int,
        default=100_000,
        help="Maximum listings to process in this run (default: 100000).",
    )
    parser.add_argument(
        "--address",
        help="Address or area to search from the Idealista homepage before scraping.",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_DATA_FILE),
        help=f"JSON output file (default: {DEFAULT_DATA_FILE}). Relative paths use the current directory.",
    )
    return parser.parse_args()


def validate_initial_url(url):
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not (hostname == "idealista.com" or hostname.endswith(".idealista.com")):
        raise ValueError("initial_url must be an http(s) URL on idealista.com")
    if parsed.path not in {"", "/"} and "/inmueble/" not in parsed.path and "/venta-viviendas/" not in parsed.path:
        raise ValueError("initial_url must be a property listing or a /venta-viviendas/ results page")


async def search_by_address(page, address):
    """Submit a homepage location search and wait for its results page."""
    reject_cookies = page.get_by_text("Rechazar", exact=True).first
    try:
        await reject_cookies.wait_for(state="visible", timeout=8_000)
        await reject_cookies.click(timeout=10_000)
        await reject_cookies.wait_for(state="hidden", timeout=10_000)
    except Exception as error:
        # The consent dialog may not appear if its choice was saved already.
        if await reject_cookies.is_visible():
            raise RuntimeError(f"Could not dismiss the cookie dialog with Rechazar: {error}") from error

    search_input = page.locator("#campoBus")
    await search_input.wait_for(state="visible", timeout=30_000)
    await search_input.fill(address)

    # Idealista may show an autocomplete list. Select its first suggestion
    # when available, then submit the search.
    suggestion = page.locator(
        '[role="option"], .suggestion, .suggestion-item, '
        '[class*="autocomplete"] li, [class*="suggestion"] li'
    ).first
    await search_input.press("Enter")

    search_button = page.locator("#btn-free-search")
    await search_button.wait_for(state="visible", timeout=10_000)
    await search_button.click()

    await page.wait_for_url(
        lambda url: "/venta-viviendas/" in url and "/inmueble/" not in url,
        timeout=60_000,
    )


async def scrape(initial_url, max_listings, output_file, address=None):
    validate_initial_url(initial_url)
    if max_listings < 1:
        raise ValueError("--max-listings must be at least 1")
    if not EXTRACTOR_FILE.is_file():
        raise FileNotFoundError(f"JavaScript extractor not found: {EXTRACTOR_FILE}")

    data_file = Path(output_file).expanduser().resolve()
    data_file.parent.mkdir(parents=True, exist_ok=True)

    js_extractor_code = EXTRACTOR_FILE.read_text(encoding="utf-8")
    if data_file.exists():
        scraped_listings = json.loads(data_file.read_text(encoding="utf-8"))
        if not isinstance(scraped_listings, list):
            raise ValueError(f"Expected a JSON array in {data_file}")
    else:
        scraped_listings = []

    listing_index = {
        str(item.get("id")): index
        for index, item in enumerate(scraped_listings)
        if isinstance(item, dict) and item.get("id") is not None
    }
    print(f"Loaded {len(scraped_listings)} existing listings from {data_file}")

    visited_listing_ids = set()
    waited_milestones = set()
    search_results_url = None
    start_path = urlparse(initial_url).path
    if "/venta-viviendas/" in start_path and "/inmueble/" not in start_path:
        search_results_url = initial_url

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        try:
            cdp_session = await browser.new_browser_cdp_session()
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                viewport={"width": 1366, "height": 768},
                locale="es-ES",
                timezone_id="Europe/Madrid",
            )
            browser_contexts = await cdp_session.send("Target.getBrowserContexts")
            context_ids = browser_contexts.get("browserContextIds", [])
            if context_ids:
                await cdp_session.send(
                    "Browser.setPermission",
                    {
                        "permission": {"name": "geolocation"},
                        "setting": "denied",
                        "origin": "https://www.idealista.com",
                        "browserContextId": context_ids[-1],
                    },
                )
            page = await context.new_page()

            def remember_search_results(frame):
                nonlocal search_results_url
                frame_url = frame.url
                if (
                    frame == page.main_frame
                    and "/inmueble/" not in frame_url
                    and "/venta-viviendas/" in frame_url
                ):
                    search_results_url = frame_url

            page.on("framenavigated", remember_search_results)
            await page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', { get: () => false });"
            )

            print(f"Opening initial URL: {initial_url}")
            await page.goto(initial_url, wait_until="domcontentloaded", timeout=60_000)

            initial_path = urlparse(initial_url).path
            if initial_path in {"", "/"}:
                if address:
                    print(f"Searching Idealista for: {address}")
                    await search_by_address(page, address)
                    search_results_url = page.url
                    print(f"Search results loaded: {search_results_url}")
                    first_listing = page.locator("a.item-link:visible").first
                    await first_listing.wait_for(state="visible", timeout=30_000)
                    print(f"Opening first listing: {await first_listing.get_attribute('href')}")
                    await first_listing.click()
                    await page.wait_for_timeout(5_000)
                else:
                    print("In the browser, complete any site prompts, search for listings, and open the first property.")
                    while re.search(r"/inmueble/\d+/?", urlparse(page.url).path) is None:
                        await asyncio.sleep(1)
            elif re.search(r"/inmueble/\d+/?", initial_path) is None:
                first_listing = page.locator("a.item-link:visible").first
                await first_listing.wait_for(state="visible", timeout=30_000)
                await first_listing.click()
                await page.wait_for_timeout(5_000)

            print("Starting listing extraction and 'Siguiente' navigation...")
            if "/inmueble/" not in page.url:
                raise RuntimeError(f"Expected a property page before scraping, got: {page.url}")

            for iteration in range(max_listings):
                await page.evaluate("window.scrollTo(0, 0)")
                await asyncio.sleep(1)

                current_url = page.url
                match = re.search(r"/inmueble/(\d+)/", current_url)
                listing_id = match.group(1) if match else None
                if listing_id:
                    visited_listing_ids.add(listing_id)

                    full_js_evaluation = f"""
                    async (id) => {{
                        {js_extractor_code}
                        return await extractPropertyData(id);
                    }}
                    """
                    property_data = await page.evaluate(full_js_evaluation, listing_id)

                    # Close the map overlay opened by the extractor before moving on.
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(300)

                    listing_key = str(listing_id)
                    if listing_key in listing_index:
                        existing = scraped_listings[listing_index[listing_key]]
                        existing.update(
                            {
                                key: value
                                for key, value in property_data.items()
                                if value is not None and value != ""
                            }
                        )
                        action = "Updated"
                    else:
                        listing_index[listing_key] = len(scraped_listings)
                        scraped_listings.append(property_data)
                        action = "Saved"

                    data_file.write_text(
                        json.dumps(scraped_listings, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    print(
                        f"[{iteration + 1}/{max_listings}] {action} ID: {listing_id}"
                        f" | Price: {property_data.get('price')}"
                        f" | Geo: ({property_data.get('latitude')}, {property_data.get('longitude')})"
                        f" | Total saved: {len(scraped_listings)}"
                    )

                visited_count = len(visited_listing_ids)
                if (
                    visited_count > 0
                    and visited_count % 30 == 0
                    and visited_count not in waited_milestones
                ):
                    waited_milestones.add(visited_count)
                    print(f"Reached {visited_count} unique listings; waiting 15 seconds...")
                    await asyncio.sleep(15)

                next_button = page.locator('a:has-text("Siguiente")')
                if await next_button.count() == 0:
                    print("No more 'Siguiente' button found. Returning to search results.")
                    if not search_results_url:
                        print(
                            "No search-results URL is known. Start from a /venta-viviendas/ "
                            "URL to enable results-page fallback."
                        )
                        break

                    current_page_match = re.search(
                        r"/pagina-(\d+)\.htm", urlparse(search_results_url).path
                    )
                    current_search_page = int(current_page_match.group(1)) if current_page_match else 1
                    saved_listing_count = len(scraped_listings)
                    page_from_count = saved_listing_count // 30 + 1
                    next_search_page = max(current_search_page + 1, page_from_count)
                    clean_search_url = re.split(r"[?#]", search_results_url, maxsplit=1)[0].rstrip("/")
                    base_search_url = re.sub(
                        r"/pagina-\d+\.htm$", "", clean_search_url
                    ).rstrip("/") + "/"
                    if next_search_page == 1:
                        next_search_url = base_search_url
                    else:
                        next_search_url = (
                            base_search_url.rstrip("/")
                            + f"/pagina-{next_search_page}.htm"
                        )

                    print(
                        f"{saved_listing_count} unique listings already saved; "
                        f"opening results page {next_search_page}: {next_search_url}"
                    )
                    await page.goto(
                        next_search_url,
                        wait_until="domcontentloaded",
                        timeout=60_000,
                    )
                    first_listing = page.locator("a.item-link:visible").first
                    try:
                        await first_listing.wait_for(state="visible", timeout=15_000)
                    except Exception:
                        print(f"No listing cards found on results page {next_search_page}; stopping.")
                        break
                    await first_listing.click()
                    await page.wait_for_timeout(5_000)
                    continue

                box = await next_button.bounding_box()
                if box:
                    await page.mouse.move(
                        box["x"] + box["width"] / 2,
                        box["y"] + box["height"] / 2,
                        steps=12,
                    )

                await asyncio.sleep(random.uniform(3, 3))
                try:
                    async with page.expect_navigation(
                        wait_until="domcontentloaded", timeout=30_000
                    ):
                        await next_button.click()
                except Exception:
                    pass

            print(f"Finished. {len(scraped_listings)} property objects are in {data_file}")
        finally:
            await browser.close()


def main():
    args = parse_args()
    try:
        asyncio.run(scrape(args.initial_url, args.max_listings, args.output, args.address))
    except KeyboardInterrupt:
        print("Interrupted by user; data saved so far remains in the output JSON.")


if __name__ == "__main__":
    main()
