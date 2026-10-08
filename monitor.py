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
    "the",
    "and",
    "for",
    "with",
    "of",
    "a",
    "an",
}


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
        if token not in STOPWORDS
        and len(token) > 1
    ]


def title_matches(product_name, title):
    """
    Strict product matching.

    The candidate title MUST contain:
      - 30th
      - celebration
      - every meaningful product-specific word

    This is deliberately strict so a generic Pokémon product
    can never trigger a 30th Celebration alert.
    """

    candidate_tokens = set(
        norm(title).split()
    )

    # Mandatory 30th Celebration protection.
    for required in REQUIRED_GLOBAL:
        if required not in candidate_tokens:
            return False

    required_tokens = tokens(
        product_name
    )

    if not required_tokens:
        return False

    # Every meaningful watchlist token must exist.
    for token in required_tokens:
        if token not in candidate_tokens:
            return False

    return True


# ============================================================
# STOCK DETECTION
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


def stock_state(text):
    value = norm(text)

    # OUT is checked first.
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
            f"WARN | state read failed | {exc}"
        )

    return {}


def save_state(state):
    """
    Save atomically so a Render restart does not easily
    leave a half-written state.json.
    """

    temp_file = STATE_FILE.with_suffix(
        ".tmp"
    )

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


def normalise_saved_entry(entry):
    """
    Supports both the new state format and simple legacy
    URL/string entries.
    """

    if isinstance(entry, dict):
        return entry

    if isinstance(entry, str):
        return {
            "url": entry
        }

    return {}


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

        print(
            "TELEGRAM | Alert sent"
        )

    except Exception as exc:
        print(
            f"ERROR | Telegram failed | {exc}"
        )


# ============================================================
# GENERAL HELPERS
# ============================================================

def clean_title(
    text,
    limit=700,
):
    return re.sub(
        r"\s+",
        " ",
        text or "",
    ).strip()[:limit]


# ============================================================
# TAKEALOT
# ============================================================

def extract_takealot(
    html,
    base_url,
):
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    results = []
    seen = set()

    anchors = soup.select(
        'a[href*="/product/"]'
    )

    for anchor in anchors:

        href = anchor.get(
            "href",
            "",
        )

        title = clean_title(
            anchor.get_text(
                " ",
                strip=True,
            )
        )

        if len(title) < 8:

            parent = anchor.find_parent()

            if parent:
                title = clean_title(
                    parent.get_text(
                        " ",
                        strip=True,
                    )
                )

        if (
            len(title) < 8
            or len(title) > 700
        ):
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

    return results[:60]


# ============================================================
# CHECKERS
# ============================================================

def extract_checkers(
    html,
    base_url,
):
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    results = []
    seen = set()

    selectors = [
        'a[href*="/products/"]',
        'a[href*="/product/"]',
    ]

    anchors = []

    for selector in selectors:
        anchors.extend(
            soup.select(selector)
        )

    for anchor in anchors:

        href = anchor.get(
            "href",
            "",
        )

        title = clean_title(
            anchor.get_text(
                " ",
                strip=True,
            )
        )

        if len(title) < 8:

            parent = anchor.find_parent()

            if parent:
                title = clean_title(
                    parent.get_text(
                        " ",
                        strip=True,
                    )
                )

        if (
            len(title) < 8
            or len(title) > 900
        ):
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

    return results[:60]


# ============================================================
# AMAZON
# ============================================================

def extract_amazon(
    html,
    base_url,
):
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    results = []
    seen = set()

    # --------------------------------------------------------
    # NORMAL AMAZON SEARCH RESULT STRUCTURE
    # --------------------------------------------------------

    cards = soup.select(
        'div[data-component-type="s-search-result"], '
        'div.s-result-item[data-asin], '
        'div[data-asin]'
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

        title = clean_title(
            link.get_text(
                " ",
                strip=True,
            )
        )

        href = link.get(
            "href",
            "",
        )

        if (
            not title
            or "/dp/" not in href
        ):
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
                "text": clean_title(
                    card.get_text(
                        " ",
                        strip=True,
                    ),
                    2500,
                ),
            }
        )

    # --------------------------------------------------------
    # AMAZON FALLBACK
    #
    # Some Amazon layouts don't expose the normal search-result
    # card wrappers. In that case, inspect /dp/ links directly.
    #
    # IMPORTANT:
    # We still DO NOT accept these as matches until
    # title_matches() approves the title.
    # --------------------------------------------------------

    if not results:

        for link in soup.select(
            'a[href*="/dp/"]'
        ):

            title = clean_title(
                link.get_text(
                    " ",
                    strip=True,
                )
            )

            href = link.get(
                "href",
                "",
            )

            if (
                not title
                or len(title) < 8
            ):
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

            if len(results) >= 60:
                break

    return results[:60]


# ============================================================
# RETAILER EXTRACTION
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
# MATCH CANDIDATES
# ============================================================

def rank_candidates(
    product_name,
    listings,
):
    return [
        listing
        for listing in listings
        if title_matches(
            product_name,
            listing["title"],
        )
    ]


# ============================================================
# PLAYWRIGHT
# ============================================================

BLOCKED_RESOURCE_TYPES = {
    "image",
    "media",
    "font",
    "stylesheet",
}


async def block_heavy(route):

    if (
        route.request.resource_type
        in BLOCKED_RESOURCE_TYPES
    ):
        await route.abort()
    else:
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
            f"{url} | "
            f"{type(exc).__name__}"
        )

        return False


# ============================================================
# PRODUCT PAGE CHECK
# ============================================================

async def inspect_detail(
    page,
    product_name,
    listing,
):

    if not await goto(
        page,
        listing["url"],
        timeout=9000,
    ):
        return "UNKNOWN"

    await page.wait_for_timeout(
        400
    )

    # --------------------------------------------------------
    # Get actual product title.
    # --------------------------------------------------------

    try:

        h1 = await page.locator(
            "h1"
        ).first.text_content(
            timeout=1500
        )

    except Exception:

        h1 = ""

    try:

        page_title = await page.title()

    except Exception:

        page_title = ""

    detail_title = clean_title(
        h1 or page_title
    )

    # --------------------------------------------------------
    # CRITICAL SAFETY CHECK
    #
    # Even if we have a saved URL, it cannot be trusted unless
    # the actual page still identifies the correct product.
    # --------------------------------------------------------

    if not title_matches(
        product_name,
        detail_title,
    ):

        print(
            "WARN | detail title rejected | "
            f"{detail_title[:180]}"
        )

        return "UNKNOWN"

    # --------------------------------------------------------
    # Read page text.
    # --------------------------------------------------------

    try:

        body = await page.locator(
            "body"
        ).inner_text(
            timeout=2500
        )

    except Exception:

        return "UNKNOWN"

    body = norm(
        body[:40000]
    )

    # Mandatory 30th Celebration protection.
    if (
        "30th" not in body
        or "celebration"
        not in body
    ):
        return "UNKNOWN"

    return stock_state(
        body
    )


# ============================================================
# MAIN
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

        # Keep memory usage low on Render.
        await context.route(
            "**/*",
            block_heavy,
        )

        # ONE browser
        # ONE context
        # ONE page
        page = await context.new_page()

        page.set_default_timeout(
            6000
        )

        try:

            while True:

                cycle_start = time.time()

                checks = 0

                print("")
                print(
                    "=" * 60
                )
                print(
                    "STARTING STOCK SCAN"
                )
                print(
                    "=" * 60
                )

                # ====================================================
                # ALL WATCHLIST PRODUCTS
                # ====================================================

                for product in WATCHLIST:

                    # =================================================
                    # ALL RETAILERS
                    # =================================================

                    for retailer, template in RETAILERS.items():

                        checks += 1

                        key = (
                            f"{retailer}|{product}"
                        )

                        # ---------------------------------------------
                        # LOAD PREVIOUS STATE
                        # ---------------------------------------------

                        record = normalise_saved_entry(
                            state.get(
                                key,
                                {},
                            )
                        )

                        previous = record.get(
                            "status",
                            "UNKNOWN",
                        )

                        known_url = record.get(
                            "url",
                            "",
                        )

                        # ---------------------------------------------
                        # SEARCH URL
                        # ---------------------------------------------

                        query = quote_plus(
                            product
                        )

                        search_url = (
                            template.format(
                                query=query
                            )
                        )

                        html = ""

                        listings = []

                        ranked = []

                        # ---------------------------------------------
                        # SEARCH
                        #
                        # Search is primarily for:
                        #
                        # 1. Discovering a product URL.
                        # 2. Discovering a replacement URL.
                        #
                        # We NEVER assume "not found" means OUT.
                        # ---------------------------------------------

                        if await goto(
                            page,
                            search_url,
                            timeout=12000,
                        ):

                            await page.wait_for_timeout(
                                700
                            )

                            try:

                                html = await page.content()

                                listings = (
                                    extract_candidates(
                                        retailer,
                                        html,
                                        search_url,
                                    )
                                )

                                ranked = (
                                    rank_candidates(
                                        product,
                                        listings,
                                    )
                                )

                            except Exception as exc:

                                print(
                                    f"WARN | extraction failed | "
                                    f"{retailer} | {exc}"
                                )

                        print(
                            f"DEBUG | {retailer} | "
                            f"{product} | "
                            f"HTML={len(html)} | "
                            f"candidates={len(listings)} | "
                            f"exact={len(ranked)}"
                        )

                        result = "UNKNOWN"

                        listing = None

                        # =================================================
                        # 1. CHECK EXACT SEARCH RESULTS
                        # =================================================

                        if ranked:

                            # At most two genuine matches.
                            #
                            # Importantly, unrelated Pokémon products
                            # NEVER reach inspect_detail().
                            for candidate in ranked[:2]:

                                result = (
                                    await inspect_detail(
                                        page,
                                        product,
                                        candidate,
                                    )
                                )

                                if result in (
                                    "IN",
                                    "OUT",
                                ):

                                    listing = candidate

                                    break

                        # =================================================
                        # 2. CHECK PREVIOUSLY DISCOVERED PRODUCT URL
                        #
                        # THIS IS THE IMPORTANT NEW FEATURE.
                        #
                        # If a product disappears from search because
                        # it is sold out, we can still monitor its direct
                        # product page.
                        # =================================================

                        if (
                            result == "UNKNOWN"
                            and known_url
                        ):

                            known_listing = {
                                "title": record.get(
                                    "title",
                                    product,
                                ),
                                "url": known_url,
                                "text": record.get(
                                    "title",
                                    product,
                                ),
                            }

                            result = (
                                await inspect_detail(
                                    page,
                                    product,
                                    known_listing,
                                )
                            )

                            if result in (
                                "IN",
                                "OUT",
                            ):

                                listing = (
                                    known_listing
                                )

                        # =================================================
                        # FINAL STATUS
                        # =================================================

                        if result in (
                            "IN",
                            "OUT",
                        ):

                            current = result

                        else:

                            current = "UNKNOWN"

                        # =================================================
                        # SAVE STATE
                        #
                        # IMPORTANT:
                        #
                        # UNKNOWN DOES NOT ERASE A KNOWN URL.
                        # UNKNOWN DOES NOT TURN OUT INTO UNKNOWN.
                        # UNKNOWN DOES NOT TRIGGER TELEGRAM.
                        # =================================================

                        if (
                            listing
                            and current in (
                                "IN",
                                "OUT",
                            )
                        ):

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

                        elif key not in state:

                            state[key] = {
                                "status": "UNKNOWN",
                                "title": product,
                                "url": known_url,
                                "checked": int(
                                    time.time()
                                ),
                            }

                        # =================================================
                        # TELEGRAM
                        #
                        # ONLY alert when:
                        #
                        # previous != IN
                        # current == IN
                        #
                        # Therefore:
                        #
                        # OUT -> IN       ALERT
                        # UNKNOWN -> IN   ALERT
                        # IN -> IN        NO ALERT
                        # IN -> UNKNOWN   NO ALERT
                        # =================================================

                        if (
                            current == "IN"
                            and previous != "IN"
                        ):

                            alert_title = (
                                listing["title"]
                                if listing
                                else record.get(
                                    "title",
                                    product,
                                )
                            )

                            alert_url = (
                                listing["url"]
                                if listing
                                else known_url
                            )

                            if alert_url:

                                telegram(
                                    "🟢 IN STOCK\n\n"
                                    f"{product}\n"
                                    f"{retailer}\n"
                                    f"{alert_title}\n\n"
                                    "BUY NOW:\n"
                                    f"{alert_url}"
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

                # ====================================================
                # END OF SCAN
                # ====================================================

                elapsed = (
                    time.time()
                    - cycle_start
                )

                expected_checks = (
                    len(WATCHLIST)
                    * len(RETAILERS)
                )

                print("")
                print(
                    f"SCAN COMPLETE | "
                    f"checks={checks}/{expected_checks} | "
                    f"{elapsed:.1f}s"
                )

                # ----------------------------------------------------
                # TIMING
                #
                # If the scan takes LESS than 60 seconds:
                # wait until the 60-second mark.
                #
                # If the scan takes MORE than 60 seconds:
                # start the next scan immediately.
                #
                # This means a 2-3 minute scan does NOT get followed
                # by another unnecessary 60-second wait.
                # ----------------------------------------------------

                remaining = (
                    INTERVAL
                    - elapsed
                )

                if remaining > 0:

                    print(
                        f"WAITING | "
                        f"{remaining:.1f}s"
                    )

                    await asyncio.sleep(
                        remaining
                    )

                else:

                    print(
                        "NEXT SCAN | "
                        "starting immediately"
                    )

        finally:

            await context.close()
            await browser.close()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    asyncio.run(main())
