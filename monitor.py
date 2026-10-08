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


# ============================================================
# CONFIG
# ============================================================

WATCHLIST = json.loads(
    Path("watchlist.json").read_text(encoding="utf-8")
)

STATE_FILE = Path("state.json")

INTERVAL = int(
    os.getenv("CHECK_INTERVAL_SECONDS", "60")
)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


RETAILERS = {
    "Takealot": "https://www.takealot.com/all?q={query}",
    "Checkers": "https://www.checkers.co.za/search/all?q={query}",
    "Amazon": "https://www.amazon.co.za/s?k={query}",
}


# ============================================================
# MATCHING
# ============================================================

REQUIRED_GLOBAL = (
    "30th",
    "celebration",
)

STOPWORDS = {
    "pokemon",
    "pokémon",
    "trading",
    "card",
    "cards",
    "game",
    "tcg",
    "30th",
    "celebration",
    "the",
    "and",
    "for",
    "with",
    "of",
    "a",
    "an",
}


# ============================================================
# STOCK WORDS
# ============================================================

NEGATIVE_STOCK = (
    "currently unavailable",
    "out of stock",
    "sold out",
    "not available",
    "unavailable",
    "temporarily out of stock",
    "currently out of stock",
)

POSITIVE_STOCK = (
    "in stock",
    "add to cart",
    "buy now",
    "add to basket",
    "available for delivery",
    "available to ship",
    "only 1 left",
    "only 2 left",
    "only 3 left",
    "only 4 left",
    "only 5 left",
)


# ============================================================
# MEMORY REDUCTION
# ============================================================

BLOCKED_RESOURCE_TYPES = {
    "image",
    "media",
    "font",
    "stylesheet",
}


# ============================================================
# TEXT HELPERS
# ============================================================

def norm(value):
    value = (value or "").lower()

    value = (
        value
        .replace("é", "e")
        .replace("–", "-")
        .replace("—", "-")
    )

    value = re.sub(
        r"[^a-z0-9]+",
        " ",
        value,
    )

    return re.sub(
        r"\s+",
        " ",
        value,
    ).strip()


def tokens(value):
    return [
        token
        for token in norm(value).split()
        if token not in STOPWORDS and len(token) > 1
    ]


def critical_tokens(product_name):
    """
    Return the meaningful words that identify the product.

    Example:

    30th Celebration ex Tin Sylveon Greninja

    becomes approximately:

    ex / tin / sylveon / greninja
    """

    return tokens(product_name)


def title_matches(product_name, title):
    """
    Extremely strict product-title matching.

    A candidate MUST contain:
      - 30th
      - celebration
      - every meaningful product token

    This prevents unrelated Pokémon products from qualifying.
    """

    product_normalized = norm(product_name)
    title_normalized = norm(title)

    candidate_tokens = set(
        title_normalized.split()
    )

    # Absolutely mandatory.
    for required in REQUIRED_GLOBAL:
        if required not in candidate_tokens:
            return False

    required_tokens = critical_tokens(
        product_normalized
    )

    if not required_tokens:
        return False

    # Every meaningful product word must appear.
    for token in required_tokens:
        if token not in candidate_tokens:
            return False

    return True


# ============================================================
# STOCK DETECTION
# ============================================================

def stock_state(text):
    value = norm(text)

    # Check OUT first because some pages can contain both
    # "In Stock" and "Out of Stock" in hidden/recommendation text.
    if any(
        phrase in value
        for phrase in NEGATIVE_STOCK
    ):
        return "OUT"

    if any(
        phrase in value
        for phrase in POSITIVE_STOCK
    ):
        return "IN"

    return "UNKNOWN"


# ============================================================
# STATE
# ============================================================

def load_state():
    if not STATE_FILE.exists():
        return {}

    try:
        data = json.loads(
            STATE_FILE.read_text(
                encoding="utf-8"
            )
        )

        if isinstance(data, dict):
            return data

    except Exception as exc:
        print(
            f"WARN | Could not read state.json | {exc}"
        )

    return {}


def save_state(state):
    """
    Atomic-ish state save.

    Write to a temporary file first, then replace the
    existing state file.
    """

    temp_file = STATE_FILE.with_suffix(".tmp")

    temp_file.write_text(
        json.dumps(
            state,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    temp_file.replace(
        STATE_FILE
    )


# ============================================================
# TELEGRAM
# ============================================================

def telegram(message):
    if not TOKEN or not CHAT_ID:
        print(
            "WARN | Telegram credentials missing"
        )
        return

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": message,
                "disable_web_page_preview": False,
            },
            timeout=10,
        )

        response.raise_for_status()

        print("TELEGRAM | Alert sent")

    except Exception as exc:
        print(
            f"ERROR | Telegram failed | {exc}"
        )


# ============================================================
# TAKEALOT EXTRACTION
# ============================================================

def extract_takealot(html, base_url):
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    results = []
    seen = set()

    for anchor in soup.select(
        'a[href]'
    ):
        href = anchor.get(
            "href",
            "",
        )

        # Takealot product URLs normally contain /product/
        if "/product/" not in href:
            continue

        title = anchor.get_text(
            " ",
            strip=True,
        )

        if not title:
            parent = anchor.find_parent()

            if parent:
                title = parent.get_text(
                    " ",
                    strip=True,
                )

        title = re.sub(
            r"\s+",
            " ",
            title,
        ).strip()

        if len(title) < 8:
            continue

        if len(title) > 500:
            continue

        url = urljoin(
            base_url,
            href.split("?")[0],
        )

        if url in seen:
            continue

        seen.add(url)

        results.append(
            {
                "title": title,
                "url": url,
                "text": title,
            }
        )

    return results[:40]


# ============================================================
# CHECKERS EXTRACTION
# ============================================================

def extract_checkers(html, base_url):
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    results = []
    seen = set()

    for anchor in soup.select(
        'a[href]'
    ):
        href = anchor.get(
            "href",
            "",
        )

        if not re.search(
            r"/(products?|product)/",
            href,
            re.I,
        ):
            continue

        title = anchor.get_text(
            " ",
            strip=True,
        )

        parent = anchor.find_parent()

        if parent:
            parent_text = parent.get_text(
                " ",
                strip=True,
            )

            if len(parent_text) > len(title):
                title = parent_text

        title = re.sub(
            r"\s+",
            " ",
            title,
        ).strip()

        if len(title) < 8:
            continue

        if len(title) > 700:
            continue

        url = urljoin(
            base_url,
            href.split("?")[0],
        )

        if url in seen:
            continue

        seen.add(url)

        results.append(
            {
                "title": title,
                "url": url,
                "text": title,
            }
        )

    return results[:40]


# ============================================================
# AMAZON EXTRACTION
# ============================================================

def extract_amazon(html, base_url):
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    results = []
    seen = set()

    # Amazon search result cards.
    cards = soup.select(
        'div[data-component-type="s-search-result"]'
    )

    for card in cards:

        link = (
            card.select_one(
                'h2 a[href]'
            )
            or card.select_one(
                'a.a-link-normal[href*="/dp/"]'
            )
        )

        if not link:
            continue

        title = link.get_text(
            " ",
            strip=True,
        )

        if not title:
            continue

        href = link.get(
            "href",
            "",
        )

        url = urljoin(
            base_url,
            href.split("?")[0],
        )

        if url in seen:
            continue

        seen.add(url)

        card_text = card.get_text(
            " ",
            strip=True,
        )

        results.append(
            {
                "title": title,
                "url": url,
                "text": card_text[:2500],
            }
        )

    return results[:20]


# ============================================================
# RETAILER DISPATCH
# ============================================================

def extract_candidates(
    retailer,
    html,
    base_url,
):
    if retailer == "Takealot":
        return extract_takealot(
            html,
            base_url,
        )

    if retailer == "Checkers":
        return extract_checkers(
            html,
            base_url,
        )

    if retailer == "Amazon":
        return extract_amazon(
            html,
            base_url,
        )

    return []


# ============================================================
# RANK / FILTER
# ============================================================

def rank_candidates(
    product_name,
    listings,
):
    ranked = []

    for listing in listings:

        # THIS IS THE IMPORTANT SAFETY FILTER.
        #
        # If the search result title itself does not exactly
        # identify the requested 30th Celebration product,
        # it never reaches the product page.
        if not title_matches(
            product_name,
            listing["title"],
        ):
            continue

        ranked.append(
            listing
        )

    return ranked


# ============================================================
# PLAYWRIGHT
# ============================================================

async def block_heavy(route):
    if (
        route.request.resource_type
        in BLOCKED_RESOURCE_TYPES
    ):
        await route.abort()
        return

    await route.continue_()


async def goto(
    page,
    url,
    timeout=12000,
):
    try:

        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=timeout,
        )

        return True

    except Exception as exc:

        print(
            f"WARN | navigation failed | "
            f"{url} | {type(exc).__name__}"
        )

        return False


# ============================================================
# DETAIL PAGE CHECK
# ============================================================

async def inspect_detail(
    page,
    product_name,
    listing,
):
    """
    Only called after the search-result title has already
    passed the strict 30th Celebration product match.

    This second check protects against bad retailer search
    results and lets us determine actual stock.
    """

    if not await goto(
        page,
        listing["url"],
        timeout=9000,
    ):
        return "UNKNOWN"

    await page.wait_for_timeout(
        500
    )

    # Try the actual H1 first.
    try:
        h1 = await page.locator(
            "h1"
        ).first.text_content(
            timeout=2000
        )

    except Exception:
        h1 = ""

    # Fallback to browser title.
    try:
        page_title = await page.title()

    except Exception:
        page_title = ""

    detail_title = (
        h1
        or page_title
        or ""
    )

    # The detail page itself must still match the requested
    # product. If it doesn't, reject it.
    if not title_matches(
        product_name,
        detail_title,
    ):
        print(
            "WARN | detail title rejected | "
            f"{detail_title[:180]}"
        )

        return "UNKNOWN"

    try:
        body = await page.locator(
            "body"
        ).inner_text(
            timeout=3000
        )

    except Exception:
        return "UNKNOWN"

    # Only inspect a reasonable amount of text.
    body_normalized = norm(
        body[:30000]
    )

    # Mandatory protection against generic Pokémon products.
    if (
        "30th" not in body_normalized
        or "celebration"
        not in body_normalized
    ):
        return "UNKNOWN"

    return stock_state(
        body_normalized
    )


# ============================================================
# MAIN SCANNER
# ============================================================

async def main():

    state = load_state()

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-dev-shm-usage",
                "--no-sandbox",
            ],
        )

        context = await browser.new_context(
            locale="en-ZA",
            timezone_id="Africa/Johannesburg",
            user_agent=(
                "Mozilla/5.0 "
                "(Linux; Android 13) "
                "AppleWebKit/537.36 "
                "Chrome/126 Mobile Safari/537.36"
            ),
            viewport={
                "width": 390,
                "height": 844,
            },
        )

        # Reduce Render memory usage.
        await context.route(
            "**/*",
            block_heavy,
        )

        # ONE PAGE ONLY.
        page = await context.new_page()

        page.set_default_timeout(
            6000
        )

        try:

            while True:

                cycle_start = time.time()

                print("")
                print("=" * 60)
                print(
                    "STARTING STOCK SCAN"
                )
                print("=" * 60)

                # ------------------------------------------------
                # PRODUCT LOOP
                # ------------------------------------------------

                for product in WATCHLIST:

                    for retailer, template in RETAILERS.items():

                        query = quote_plus(
                            product
                        )

                        search_url = (
                            template.format(
                                query=query
                            )
                        )

                        # ----------------------------------------
                        # SEARCH PAGE
                        # ----------------------------------------

                        if not await goto(
                            page,
                            search_url,
                        ):
                            continue

                        await page.wait_for_timeout(
                            900
                        )

                        try:
                            html = await page.content()

                        except Exception as exc:
                            print(
                                f"WARN | content failed | "
                                f"{retailer} | {exc}"
                            )
                            continue

                        listings = extract_candidates(
                            retailer,
                            html,
                            search_url,
                        )

                        ranked = rank_candidates(
                            product,
                            listings,
                        )

                        print(
                            f"DEBUG | {retailer} | "
                            f"{product} | "
                            f"HTML={len(html)} | "
                            f"candidates={len(listings)} | "
                            f"exact={len(ranked)}"
                        )

                        # Nothing that actually matches the
                        # requested product was found.
                        if not ranked:
                            continue

                        best = None

                        # Only inspect the first two genuinely
                        # matching products.
                        #
                        # This is the major difference from the
                        # old version: unrelated products never
                        # reach this point.
                        for listing in ranked[:2]:

                            # First look at stock text already
                            # present on the search result.
                            result = stock_state(
                                listing["text"]
                            )

                            # If search result does not tell us,
                            # inspect the exact product page.
                            if result == "UNKNOWN":

                                result = await inspect_detail(
                                    page,
                                    product,
                                    listing,
                                )

                            if result == "IN":

                                best = (
                                    "IN",
                                    listing,
                                )

                                break

                            if (
                                result == "OUT"
                                and best is None
                            ):

                                best = (
                                    "OUT",
                                    listing,
                                )

                        # ----------------------------------------
                        # RESULT
                        # ----------------------------------------

                        if best is None:

                            current = "UNKNOWN"
                            listing = ranked[0]

                        else:

                            current, listing = best

                        key = (
                            f"{retailer}|{product}"
                        )

                        previous = (
                            state
                            .get(key, {})
                            .get(
                                "status",
                                "UNKNOWN",
                            )
                        )

                        # ------------------------------------------------
                        # IMPORTANT:
                        #
                        # UNKNOWN DOES NOT ERASE STATE.
                        #
                        # If Amazon/Takealot/Checkers temporarily fails,
                        # we don't turn IN into UNKNOWN and then generate
                        # another alert the next time it becomes IN.
                        # ------------------------------------------------

                        if current != "UNKNOWN":

                            state[key] = {
                                "status": current,
                                "title": listing[
                                    "title"
                                ],
                                "url": listing[
                                    "url"
                                ],
                                "checked": int(
                                    time.time()
                                ),
                            }

                        # ------------------------------------------------
                        # TELEGRAM ALERT
                        #
                        # ONLY:
                        #
                        # previous != IN
                        # AND
                        # current == IN
                        # ------------------------------------------------

                        if (
                            current == "IN"
                            and previous != "IN"
                        ):

                            telegram(
                                "🟢 IN STOCK\n\n"
                                f"{product}\n"
                                f"{retailer}\n"
                                f"{listing['title']}\n\n"
                                "BUY NOW:\n"
                                f"{listing['url']}"
                            )

                        print(
                            f"{retailer} | "
                            f"{product} | "
                            f"status={current} | "
                            f"previous={previous}"
                        )

                        save_state(
                            state
                        )

                # ------------------------------------------------
                # END OF CYCLE
                # ------------------------------------------------

                elapsed = (
                    time.time()
                    - cycle_start
                )

                sleep_for = max(
                    1,
                    INTERVAL
                    - int(elapsed),
                )

                print("")
                print(
                    f"SCAN COMPLETE | "
                    f"{elapsed:.1f}s | "
                    f"sleeping {sleep_for}s"
                )
                print("")

                await asyncio.sleep(
                    sleep_for
                )

        finally:

            await context.close()
            await browser.close()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    asyncio.run(main())
