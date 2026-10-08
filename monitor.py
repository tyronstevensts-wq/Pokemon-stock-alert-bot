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
# CONFIGURATION
# ============================================================

WATCHLIST = json.loads(
    Path("watchlist.json").read_text(encoding="utf-8")
)

STATE_FILE = Path("state.json")
INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "60"))

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not TOKEN or not CHAT_ID:
    raise SystemExit(
        "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID environment variable."
    )


RETAILERS = {
    "Takealot": {
        "search": os.getenv(
            "TAKEALOT_SEARCH_URL",
            "https://www.takealot.com/all?q={query}",
        ),
        "base": "https://www.takealot.com",
    },
    "Checkers": {
        "search": os.getenv(
            "CHECKERS_SEARCH_URL",
            "https://www.checkers.co.za/search/all?q={query}",
        ),
        "base": "https://www.checkers.co.za",
    },
    "Amazon": {
        "search": os.getenv(
            "AMAZON_SEARCH_URL",
            "https://www.amazon.co.za/s?k={query}",
        ),
        "base": "https://www.amazon.co.za",
    },
}


# ============================================================
# TEXT HELPERS
# ============================================================

def norm(text):
    if not text:
        return ""

    text = text.lower()
    text = text.replace("pokémon", "pokemon")
    text = text.replace("–", "-")
    text = text.replace("—", "-")
    text = text.replace("&", " and ")

    text = re.sub(r"[^a-z0-9]+", " ", text)

    return re.sub(
        r"\s+",
        " ",
        text,
    ).strip()


# ============================================================
# HARD 30TH CELEBRATION SAFETY FILTER
# ============================================================

def is_30th_celebration(text):
    """
    HARD SAFETY FILTER.

    A listing MUST contain both:
        30th
        celebration

    Generic Pokémon products are rejected.
    """

    t = norm(text)

    return (
        "30th" in t
        and "celebration" in t
    )


# ============================================================
# PRODUCT MATCHING
# ============================================================

PRODUCT_ALIASES = {
    "elite trainer box": [
        "elite trainer box",
        "etb",
    ],
    "ultra premium collection": [
        "ultra premium collection",
        "ultra-premium collection",
        "upc",
    ],
    "battle deck": [
        "battle deck",
    ],
    "poster collection": [
        "poster collection",
    ],
    "booster bundle": [
        "booster bundle",
    ],
    "knock out collection": [
        "knock out collection",
        "knockout collection",
    ],
    "premium collection": [
        "premium collection",
    ],
    "tech sticker collection": [
        "tech sticker collection",
    ],
    "mini tin": [
        "mini tin",
    ],
    "ex tin": [
        "ex tin",
    ],
    "ex box": [
        "ex box",
    ],
    "2 pack blister": [
        "2 pack blister",
        "2-pack blister",
        "2 pack",
    ],
    "mega expansion pack": [
        "mega expansion pack",
        "expansion pack",
    ],
    "binder collection": [
        "binder collection",
    ],
    "figure collection": [
        "figure collection",
    ],
}


VARIANTS = {
    "mewtwo",
    "umbreon",
    "espeon",
    "zapdos",
    "lucario",
    "sylveon",
    "greninja",
    "ditto",
}


def product_type_matches(target, candidate):
    target_n = norm(target)
    candidate_n = norm(candidate)

    for product_type, aliases in PRODUCT_ALIASES.items():

        if product_type in target_n:

            return any(
                norm(alias) in candidate_n
                for alias in aliases
            )

    return False


def variant_matches(target, candidate):
    target_n = norm(target)
    candidate_n = norm(candidate)

    for variant in VARIANTS:

        if variant in target_n:

            if variant not in candidate_n:
                return False

    return True


def match_score(
    target,
    candidate_title,
    candidate_text="",
):
    """
    Returns 0 when the listing is not a safe 30th
    Celebration match.

    Otherwise returns a score between 0 and 1.
    """

    combined = (
        f"{candidate_title} "
        f"{candidate_text[:1500]}"
    )

    combined_n = norm(combined)
    title_n = norm(candidate_title)
    target_n = norm(target)

    # --------------------------------------------------------
    # ABSOLUTE SAFETY RULE
    # --------------------------------------------------------

    if not is_30th_celebration(combined):
        return 0.0

    # --------------------------------------------------------
    # PRODUCT TYPE
    # --------------------------------------------------------

    if not product_type_matches(
        target,
        combined,
    ):
        return 0.0

    # --------------------------------------------------------
    # VARIANT
    # --------------------------------------------------------

    if not variant_matches(
        target,
        combined,
    ):
        return 0.0

    # --------------------------------------------------------
    # TOKEN MATCH
    # --------------------------------------------------------

    stop_words = {
        "pokemon",
        "tcg",
        "the",
        "and",
        "card",
        "cards",
        "game",
        "trading",
        "celebration",
        "30th",
    }

    target_tokens = {
        word
        for word in target_n.split()
        if word not in stop_words
        and len(word) > 2
    }

    candidate_tokens = set(
        combined_n.split()
    )

    if not target_tokens:
        return 0.0

    matched = sum(
        1
        for word in target_tokens
        if word in candidate_tokens
    )

    score = (
        matched /
        len(target_tokens)
    )

    # Give the actual title extra importance.
    title_tokens = set(
        title_n.split()
    )

    title_matched = sum(
        1
        for word in target_tokens
        if word in title_tokens
    )

    if title_matched:

        score = max(
            score,
            title_matched /
            len(target_tokens),
        )

    if score < 0.75:
        return 0.0

    return score


# ============================================================
# STOCK DETECTION
# ============================================================

def stock_state(text):

    t = norm(text)

    negative = [
        "out of stock",
        "currently unavailable",
        "sold out",
        "unavailable",
        "not available",
        "no stock",
        "temporarily out of stock",
    ]

    positive = [
        "add to cart",
        "add to basket",
        "add to trolley",
        "buy now",
        "in stock",
    ]

    # Negative wins.
    if any(
        term in t
        for term in negative
    ):
        return "OUT"

    if any(
        term in t
        for term in positive
    ):
        return "IN"

    return "UNKNOWN"


# ============================================================
# STATE
# ============================================================

def load_state():

    try:

        if STATE_FILE.exists():

            return json.loads(
                STATE_FILE.read_text(
                    encoding="utf-8"
                )
            )

    except Exception as exc:

        print(
            "STATE LOAD ERROR:",
            repr(exc),
        )

    return {}


def save_state(state):

    try:

        STATE_FILE.write_text(
            json.dumps(
                state,
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    except Exception as exc:

        print(
            "STATE SAVE ERROR:",
            repr(exc),
        )


# ============================================================
# TELEGRAM
# ============================================================

def telegram(message):

    response = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": False,
        },
        timeout=20,
    )

    response.raise_for_status()


def send_stock_alert(
    product,
    retailer,
    listing,
):

    message = (
        "🚨 POKÉMON 30TH CELEBRATION STOCK ALERT 🚨\n\n"
        f"🎴 Product: {product}\n"
        f"🏪 Retailer: {retailer}\n"
        f"📦 Listing: {listing['title']}\n\n"
        "🟢 STATUS: IN STOCK\n\n"
        f"🔗 BUY NOW:\n{listing['url']}\n\n"
        f"🎯 Match score: {listing['score']:.0%}\n"
        f"⏰ Detected: "
        f"{time.strftime('%Y-%m-%d %H:%M:%S %Z')}"
    )

    try:

        telegram(message)

        print(
            "📱 TELEGRAM ALERT SENT:",
            retailer,
            "|",
            product,
        )

    except Exception as exc:

        print(
            "TELEGRAM ERROR:",
            repr(exc),
        )


# ============================================================
# SEARCH RESULT EXTRACTION
# ============================================================

def extract_search_candidates(
    html,
    base_url,
):

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    candidates = []
    seen_urls = set()

    for anchor in soup.find_all(
        "a",
        href=True,
    ):

        if len(candidates) >= 150:
            break

        title = anchor.get_text(
            " ",
            strip=True,
        )

        if len(title) < 8:
            continue

        href = urljoin(
            base_url,
            anchor["href"],
        )

        lower_href = href.lower()

        if any(
            blocked in lower_href
            for blocked in (
                "/cart",
                "/account",
                "/help",
                "/customer",
                "javascript:",
                "#",
            )
        ):
            continue

        if href in seen_urls:
            continue

        seen_urls.add(href)

        container = anchor

        for _ in range(3):

            if container.parent is not None:

                container = container.parent

        card_text = container.get_text(
            " ",
            strip=True,
        )[:1500]

        # ----------------------------------------------------
        # HARD 30TH FILTER
        # ----------------------------------------------------

        if not is_30th_celebration(
            f"{title} {card_text}"
        ):
            continue

        candidates.append(
            {
                "title": title[:500],
                "text": card_text,
                "url": href,
            }
        )

    return candidates


# ============================================================
# PLAYWRIGHT
# ============================================================

async def block_heavy_resources(
    route,
):

    if route.request.resource_type in {
        "image",
        "media",
        "font",
    }:

        await route.abort()

    else:

        await route.continue_()


async def create_browser_context(
    browser,
):

    context = await browser.new_context(
        locale="en-ZA",
        timezone_id="Africa/Johannesburg",
        user_agent=(
            "Mozilla/5.0 "
            "(Linux; Android 14) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/140.0 "
            "Mobile Safari/537.36"
        ),
    )

    await context.route(
        "**/*",
        block_heavy_resources,
    )

    return context


# ============================================================
# RETAILER SEARCH
# ============================================================

async def search_retailer(
    page,
    retailer,
    query,
):

    template = RETAILERS[
        retailer
    ]["search"]

    url = template.format(
        query=quote_plus(query),
    )

    try:

        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=45000,
        )

        await page.wait_for_timeout(
            2500
        )

        html = await page.content()

        return (
            html,
            page.url,
            None,
        )

    except Exception as exc:

        return (
            "",
            url,
            str(exc),
        )


# ============================================================
# PRODUCT PAGE INSPECTION
# ============================================================

async def inspect_listing(
    page,
    listing,
    target_product,
):

    try:

        await page.goto(
            listing["url"],
            wait_until="domcontentloaded",
            timeout=45000,
        )

        await page.wait_for_timeout(
            1800
        )

        body = await page.locator(
            "body"
        ).inner_text(
            timeout=10000
        )

        body = body[:20000]

        # ----------------------------------------------------
        # PRODUCT PAGE MUST ALSO BE 30TH CELEBRATION
        # ----------------------------------------------------

        if not is_30th_celebration(
            body
        ):

            return (
                "UNKNOWN",
                body,
                page.url,
            )

        # ----------------------------------------------------
        # PRODUCT PAGE MUST MATCH WATCHLIST PRODUCT
        # ----------------------------------------------------

        page_score = match_score(
            target_product,
            body[:3000],
            body[:12000],
        )

        if page_score <= 0:

            return (
                "UNKNOWN",
                body,
                page.url,
            )

        return (
            stock_state(body),
            body,
            page.url,
        )

    except Exception as exc:

        print(
            "PRODUCT PAGE ERROR:",
            repr(exc),
            "|",
            listing.get("url"),
        )

        return (
            "UNKNOWN",
            listing.get(
                "text",
                "",
            ),
            listing.get(
                "url",
                "",
            ),
        )


# ============================================================
# ONE COMPLETE SCAN
# ============================================================

async def run_scan(
    browser,
    state,
):

    print()
    print("=" * 70)

    print(
        "🟢 SCAN STARTED",
        time.strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
    )

    print(
        "🎯 HARD FILTER: "
        "30TH CELEBRATION ONLY"
    )

    print("=" * 70)

    context = None
    page = None

    try:

        context = await create_browser_context(
            browser
        )

        page = await context.new_page()

        for product in WATCHLIST:

            for retailer in RETAILERS:

                print(
                    "🔎",
                    retailer,
                    "|",
                    product,
                )

                key = (
                    f"{retailer}|{product}"
                )

                (
                    html,
                    search_url,
                    error,
                ) = await search_retailer(
                    page,
                    retailer,
                    product,
                )

                if error:

                    print(
                        "⚠️ SEARCH ERROR:",
                        retailer,
                        product,
                        error,
                    )

                    continue

                candidates = (
                    extract_search_candidates(
                        html,
                        RETAILERS[
                            retailer
                        ]["base"],
                    )
                )

                del html

                matches = []

                for listing in candidates:

                    score = match_score(
                        product,
                        listing["title"],
                        listing["text"],
                    )

                    if score >= 0.75:

                        listing["score"] = score

                        matches.append(
                            listing
                        )

                del candidates

                matches.sort(
                    key=lambda item:
                    item["score"],
                    reverse=True,
                )

                best_in_stock = None
                inspected_statuses = []

                for listing in matches[:5]:

                    (
                        status,
                        body,
                        final_url,
                    ) = await inspect_listing(
                        page,
                        listing,
                        product,
                    )

                    listing["status"] = status
                    listing["url"] = final_url

                    inspected_statuses.append(
                        status
                    )

                    if status == "IN":

                        best_in_stock = listing

                        break

                    del body

                if best_in_stock:

                    current = "IN"

                elif (
                    inspected_statuses
                    and all(
                        status == "OUT"
                        for status
                        in inspected_statuses
                    )
                ):

                    current = "OUT"

                elif matches:

                    current = "UNKNOWN"

                else:

                    current = "UNKNOWN"

                old = state.get(
                    key,
                    "UNKNOWN",
                )

                print(
                    retailer,
                    "|",
                    product,
                    "| matches:",
                    len(matches),
                    "| status:",
                    current,
                )

                # ------------------------------------------------
                # ALERT ONLY ON TRANSITION TO IN STOCK
                # ------------------------------------------------

                if (
                    best_in_stock is not None
                    and old != "IN"
                ):

                    send_stock_alert(
                        product,
                        retailer,
                        best_in_stock,
                    )

                # ------------------------------------------------
                # SAVE ONLY KNOWN STATES
                # ------------------------------------------------

                if current != "UNKNOWN":

                    state[key] = current

                    save_state(
                        state
                    )

                await asyncio.sleep(
                    0.5
                )

        print()
        print("=" * 70)

        print(
            "✅ SCAN COMPLETE",
            time.strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        )

        print(
            "⏱ Next scan in",
            INTERVAL,
            "seconds",
        )

        print("=" * 70)

    except Exception as exc:

        print(
            "🔴 SCAN ERROR:",
            repr(exc),
        )

    finally:

        try:

            if page is not None:

                await page.close()

        except Exception:

            pass

        try:

            if context is not None:

                await context.close()

        except Exception:

            pass


# ============================================================
# MAIN
# ============================================================

async def main():

    print("=" * 70)

    print(
        "🟢 POKÉMON SA "
        "30TH CELEBRATION STOCK MONITOR"
    )

    print(
        "Products:",
        len(WATCHLIST),
    )

    print(
        "Retailers:",
        ", ".join(
            RETAILERS.keys()
        ),
    )

    print(
        "Interval:",
        INTERVAL,
        "seconds",
    )

    print(
        "🎯 ONLY 30TH CELEBRATION "
        "PRODUCTS ARE ALLOWED"
    )

    print("=" * 70)

    state = load_state()

    async with async_playwright() as playwright:

        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--no-sandbox",
                "--disable-background-networking",
                "--disable-background-timer-throttling",
            ],
        )

        try:

            while True:

                await run_scan(
                    browser,
                    state,
                )

                print(
                    "💤 Sleeping for",
                    INTERVAL,
                    "seconds...",
                )

                await asyncio.sleep(
                    INTERVAL
                )

        finally:

            print(
                "🛑 Closing Chromium"
            )

            await browser.close()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    asyncio.run(main())
