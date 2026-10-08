import asyncio
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import quote_plus, urljoin

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

WATCHLIST = json.loads(Path("watchlist.json").read_text(encoding="utf-8"))

STATE_FILE = Path("state.json")
INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

RETAILERS = {
    "Takealot": "https://www.takealot.com/all?q={query}",
    "Checkers": "https://www.checkers.co.za/search/all?q={query}",
    "Amazon": "https://www.amazon.co.za/s?k={query}",
}

VARIANTS = (
    "mewtwo",
    "umbreon",
    "espeon",
    "zapdos",
    "lucario",
    "sylveon",
    "greninja",
    "ditto",
)

PRODUCT_TERMS = (
    "blister",
    "tin",
    "poster",
    "elite trainer box",
    "ultra premium collection",
    "battle deck",
    "premium collection",
    "booster bundle",
    "mega expansion pack",
    "celebration box",
    "mini tin",
    "tech sticker collection",
    "knock out collection",
    "ex box",
    "binder collection",
    "figure collection",
)

NEGATIVE_STOCK = (
    "out of stock",
    "sold out",
    "currently unavailable",
    "unavailable",
    "not available",
    "outofstock",
)

POSITIVE_STOCK = (
    "add to cart",
    "add to basket",
    "buy now",
    "in stock",
    "available",
)

BAD_URL_PARTS = (
    "/cart",
    "/checkout",
    "/account",
    "/login",
    "/help",
    "/customer",
    "javascript:",
    "mailto:",
)

USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 13; SM-S918B) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Mobile Safari/537.36"
)


def load_state():
    if not STATE_FILE.exists():
        return {}

    try:
        return json.loads(
            STATE_FILE.read_text(encoding="utf-8")
        )
    except Exception:
        return {}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(
            state,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def norm(value):
    value = (value or "").lower()
    value = value.replace("pokémon", "pokemon")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def product_tokens(product):
    return set(norm(product).split())


def candidate_score(product, candidate_title):
    candidate = norm(candidate_title)

    if "pokemon" not in candidate:
        return 0.0

    wanted_tokens = product_tokens(product)
    candidate_tokens = set(candidate.split())

    overlap = len(
        wanted_tokens & candidate_tokens
    )

    score = overlap / max(
        1,
        len(wanted_tokens),
    )

    if "30th" in candidate:
        score += 0.15

    if "celebration" in candidate:
        score += 0.15

    for variant in VARIANTS:
        if (
            variant in norm(product)
            and variant in candidate
        ):
            score += 0.10

    for term in PRODUCT_TERMS:
        if (
            term in norm(product)
            and term in candidate
        ):
            score += 0.10

    return score if score >= 0.35 else 0.0


def stock_state(text):
    text = norm(text)

    for phrase in NEGATIVE_STOCK:
        if phrase in text:
            return "OUT"

    for phrase in POSITIVE_STOCK:
        if phrase in text:
            return "IN"

    return "UNKNOWN"


def extract_search_candidates(html, base_url):
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    results = []
    seen = set()

    for anchor in soup.find_all(
        "a",
        href=True,
    ):
        href = anchor.get(
            "href",
            "",
        ).strip()

        if not href or href.startswith("#"):
            continue

        if any(
            part in href.lower()
            for part in BAD_URL_PARTS
        ):
            continue

        url = urljoin(
            base_url,
            href,
        )

        if url in seen:
            continue

        text = anchor.get_text(
            " ",
            strip=True,
        )

        if not text:
            continue

        parent = anchor
        combined = text

        for _ in range(4):
            parent = parent.parent

            if parent is None:
                break

            parent_text = parent.get_text(
                " ",
                strip=True,
            )

            if len(parent_text) > len(combined):
                combined = parent_text

            if len(combined) >= 2000:
                break

        combined = re.sub(
            r"\s+",
            " ",
            combined,
        ).strip()

        if len(combined) < 8:
            continue

        low = norm(combined)

        if "pokemon" not in low:
            continue

        if any(
            x in low
            for x in (
                "sign in",
                "create account",
                "customer service",
            )
        ):
            continue

        seen.add(url)

        results.append(
            {
                "title": combined[:2500],
                "url": url,
            }
        )

        if len(results) >= 50:
            break

    return results


async def get_page_html(
    page,
    url,
    wait_ms=1400,
):
    try:
        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=45000,
        )

        await page.wait_for_timeout(
            wait_ms
        )

        return await page.content()

    except Exception as exc:
        print(
            f"ERROR | page load failed | "
            f"{url} | {exc}"
        )
        return ""


async def inspect_listing(
    page,
    listing,
):
    try:
        await page.goto(
            listing["url"],
            wait_until="domcontentloaded",
            timeout=30000,
        )

        await page.wait_for_timeout(800)

        title = await page.title()

        body = await page.locator(
            "body"
        ).inner_text(
            timeout=10000
        )

        combined = f"{title} {body}"
        normal = norm(combined)

        # CRITICAL SAFETY CHECK:
        # Generic Pokémon products can NEVER trigger.
        # The actual product page must contain BOTH
        # "30th" and "celebration".
        if (
            "30th" not in normal
            or "celebration" not in normal
        ):
            return None

        status = stock_state(combined)

        return {
            "title": (
                title.strip()
                or listing["title"][:250]
            ),
            "url": listing["url"],
            "status": status,
        }

    except Exception as exc:
        print(
            f"ERROR | detail failed | "
            f"{listing['url']} | {exc}"
        )
        return None


def send_telegram(message):
    if not TOKEN or not CHAT_ID:
        print(
            "WARNING | Telegram credentials "
            "are not set"
        )
        return

    url = (
        f"https://api.telegram.org/"
        f"bot{TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": CHAT_ID,
        "text": message,
        "disable_web_page_preview": False,
    }

    try:
        response = requests.post(
            url,
            json=payload,
            timeout=15,
        )

        response.raise_for_status()

    except Exception as exc:
        print(
            f"ERROR | Telegram send failed | "
            f"{exc}"
        )


def alert_message(
    retailer,
    product,
    listing,
):
    return (
        "🚨 POKÉMON 30TH CELEBRATION "
        "IN STOCK 🚨\n\n"
        f"Product: {product}\n"
        f"Retailer: {retailer}\n\n"
        f"BUY NOW:\n{listing['url']}"
    )


async def check_retailer(
    page,
    retailer,
    product,
    state,
):
    search_url = RETAILERS[
        retailer
    ].format(
        query=quote_plus(product)
    )

    html = await get_page_html(
        page,
        search_url,
    )

    if not html:
        print(
            f"{retailer} | {product} | "
            "matches: 0 | status: UNKNOWN"
        )
        return

    listings = extract_search_candidates(
        html,
        search_url,
    )

    ranked = []

    for listing in listings:
        score = candidate_score(
            product,
            listing["title"],
        )

        if score > 0:
            ranked.append(
                (
                    score,
                    listing,
                )
            )

    ranked.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    print(
        f"DEBUG | {retailer} | "
        f"HTML size: {len(html)} | "
        f"candidates: {len(listings)} | "
        f"ranked: {len(ranked)}"
    )

    best_candidate = None
    best_in_stock = None

    # Only inspect the best few product pages.
    # This keeps memory and traffic lower.
    for _, listing in ranked[:5]:
        inspected = await inspect_listing(
            page,
            listing,
        )

        if inspected is None:
            continue

        if best_candidate is None:
            best_candidate = inspected

        if inspected["status"] == "IN":
            best_in_stock = inspected
            break

    if best_in_stock:
        current = "IN"

    elif (
        best_candidate
        and best_candidate["status"] == "OUT"
    ):
        current = "OUT"

    else:
        current = "UNKNOWN"

    key = (
        f"{retailer}|{product}"
    )

    previous = state.get(
        key,
        "UNKNOWN",
    )

    print(
        f"{retailer} | {product} | "
        f"matches: {len(ranked)} | "
        f"status: {current}"
    )

    # Alert ONLY when the product changes
    # from something other than IN to IN.
    if (
        current == "IN"
        and previous != "IN"
        and best_in_stock
    ):
        send_telegram(
            alert_message(
                retailer,
                product,
                best_in_stock,
            )
        )

    state[key] = current


async def main():
    state = load_state()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
                "--disable-renderer-backgrounding",
                "--disable-features="
                "Translate,BackForwardCache",
            ],
        )

        context = await browser.new_context(
            locale="en-ZA",
            timezone_id="Africa/Johannesburg",
            user_agent=USER_AGENT,
            viewport={
                "width": 390,
                "height": 844,
            },
        )

        async def block_heavy(route):
            if route.request.resource_type in {
                "image",
                "media",
                "font",
                "stylesheet",
            }:
                await route.abort()
            else:
                await route.continue_()

        await context.route(
            "**/*",
            block_heavy,
        )

        page = await context.new_page()

        try:
            while True:
                cycle_start = time.time()

                for product in WATCHLIST:
                    for retailer in RETAILERS:
                        try:
                            await check_retailer(
                                page,
                                retailer,
                                product,
                                state,
                            )

                        except Exception as exc:
                            print(
                                f"ERROR | "
                                f"{retailer} | "
                                f"{product} | "
                                f"{exc}"
                            )

                save_state(state)

                elapsed = (
                    time.time()
                    - cycle_start
                )

                sleep_for = max(
                    1,
                    INTERVAL
                    - int(elapsed),
                )

                print(
                    f"SCAN COMPLETE | "
                    f"{elapsed:.1f}s | "
                    f"sleeping "
                    f"{sleep_for}s"
                )

                await asyncio.sleep(
                    sleep_for
                )

        finally:
            await page.close()
            await context.close()
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
