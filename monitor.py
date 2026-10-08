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

INTERVAL = int(
    os.getenv("CHECK_INTERVAL_SECONDS", "60")
)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not TOKEN or not CHAT_ID:
    raise SystemExit(
        "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first."
    )


RETAILERS = {
    "Takealot": {
        "search": os.getenv(
            "TAKEALOT_SEARCH_URL",
            "https://www.takealot.com/all?q={query}"
        ),
        "base": "https://www.takealot.com",
    },

    "Checkers": {
        "search": os.getenv(
            "CHECKERS_SEARCH_URL",
            "https://www.checkers.co.za/search/all?q={query}"
        ),
        "base": "https://www.checkers.co.za",
    },

    "Amazon": {
        "search": os.getenv(
            "AMAZON_SEARCH_URL",
            "https://www.amazon.co.za/s?k={query}"
        ),
        "base": "https://www.amazon.co.za",
    },
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


PRODUCT_TERMS = [
    "elite trainer box",
    "ultra premium collection",
    "battle deck",
    "poster collection",
    "booster bundle",
    "knock out collection",
    "premium collection",
    "tech sticker collection",
    "mini tin",
    "ex tin",
    "ex box",
    "2 pack blister",
    "mega expansion pack",
]


# ============================================================
# TEXT HELPERS
# ============================================================

def norm(text):
    text = text.lower()
    text = text.replace("pokémon", "pokemon")
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def tokens(text):
    return set(norm(text).split())


# ============================================================
# STRICT PRODUCT MATCHING
# ============================================================

def match_score(target, candidate_title, candidate_text=""):
    """
    Very strict matching.

    The candidate must:
    - contain 30th
    - contain celebration
    - contain the correct product type
    - contain required variant names
    - achieve a high token match
    """

    target_n = norm(target)
    title_n = norm(candidate_title)
    text_n = norm(candidate_text)

    # --------------------------------------------------------
    # HARD REQUIREMENT:
    # The actual listing title must identify the 30th
    # Celebration range.
    # --------------------------------------------------------

    if "30th" not in title_n:
        return 0

    if "celebration" not in title_n:
        return 0

    # --------------------------------------------------------
    # Variant protection
    # --------------------------------------------------------

    for variant in VARIANTS:
        if variant in target_n:
            if variant not in title_n:
                return 0

    # --------------------------------------------------------
    # Product type protection
    # --------------------------------------------------------

    matched_product_term = False

    for term in PRODUCT_TERMS:
        if term in target_n:
            if term not in title_n:
                return 0

            matched_product_term = True

    if not matched_product_term:
        return 0

    # --------------------------------------------------------
    # Token comparison
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
    }

    target_tokens = {
        word
        for word in target_n.split()
        if word not in stop_words
    }

    title_tokens = tokens(title_n)

    if not target_tokens:
        return 0

    matched = sum(
        1
        for word in target_tokens
        if word in title_tokens
    )

    score = matched / len(target_tokens)

    if score < 0.90:
        return 0

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
    ]

    positive = [
        "add to cart",
        "add to basket",
        "add to trolley",
        "buy now",
        "in stock",
        "available",
    ]

    # Negative first
    if any(term in t for term in negative):
        return "OUT"

    if any(term in t for term in positive):
        return "IN"

    return "UNKNOWN"


# ============================================================
# STATE
# ============================================================

def load_state():
    try:
        if STATE_FILE.exists():
            return json.loads(
                STATE_FILE.read_text(encoding="utf-8")
            )
    except Exception as e:
        print("STATE LOAD ERROR:", e)

    return {}


def save_state(state):
    try:
        STATE_FILE.write_text(
            json.dumps(
                state,
                indent=2,
                ensure_ascii=False
            ),
            encoding="utf-8"
        )
    except Exception as e:
        print("STATE SAVE ERROR:", e)


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


# ============================================================
# SEARCH RESULT EXTRACTION
# ============================================================

def extract_search_candidates(
    retailer,
    html,
    base_url
):
    """
    Memory-efficient search result extraction.

    Important:
    We DO NOT keep thousands of large parent-card HTML/text
    strings in memory.

    We only keep a limited number of useful candidates.
    """

    soup = BeautifulSoup(html, "html.parser")

    candidates = []
    seen_urls = set()

    MAX_CANDIDATES = 80

    for a in soup.find_all("a", href=True):

        if len(candidates) >= MAX_CANDIDATES:
            break

        title = a.get_text(
            " ",
            strip=True
        )

        if len(title) < 8:
            continue

        href = urljoin(
            base_url,
            a["href"]
        )

        lower_href = href.lower()

        # Ignore navigation
        if any(
            x in lower_href
            for x in [
                "/cart",
                "/account",
                "/help",
                "/customer",
                "javascript:",
                "#",
            ]
        ):
            continue

        if href in seen_urls:
            continue

        seen_urls.add(href)

        # Only take a small amount of surrounding text.
        container = a

        for _ in range(2):
            if container.parent:
                container = container.parent

        card_text = container.get_text(
            " ",
            strip=True
        )

        # Prevent giant strings from consuming memory.
        card_text = card_text[:700]

        candidates.append(
            {
                "title": title[:300],
                "text": card_text,
                "url": href,
            }
        )

    return candidates


# ============================================================
# PLAYWRIGHT MEMORY OPTIMISATION
# ============================================================

async def block_heavy_resources(route):
    """
    Prevent Chromium from downloading unnecessary resources.

    Product text is still loaded, but images/video/fonts are
    blocked to dramatically reduce RAM usage.
    """

    resource_type = route.request.resource_type

    if resource_type in {
        "image",
        "media",
        "font",
    }:
        await route.abort()
    else:
        await route.continue_()


async def create_browser_context(browser):
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
        block_heavy_resources
    )

    return context


# ============================================================
# RETAILER SEARCH
# ============================================================

async def search_retailer(
    page,
    retailer,
    query
):

    template = RETAILERS[retailer]["search"]

    url = template.format(
        query=quote_plus(query)
    )

    try:

        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=45000
        )

        await page.wait_for_timeout(1800)

        html = await page.content()

        return html, page.url, None

    except Exception as e:

        return "", url, str(e)


# ============================================================
# PRODUCT PAGE INSPECTION
# ============================================================

async def inspect_listing(
    page,
    listing
):

    try:

        await page.goto(
            listing["url"],
            wait_until="domcontentloaded",
            timeout=45000
        )

        await page.wait_for_timeout(1200)

        body = await page.locator(
            "body"
        ).inner_text(
            timeout=10000
        )

        body = body[:12000]

        return (
            stock_state(body),
            body,
            page.url
        )

    except Exception:

        return (
            stock_state(
                listing.get("text", "")
            ),
            listing.get("text", ""),
            listing["url"]
        )


# ============================================================
# TELEGRAM ALERT
# ============================================================

def send_stock_alert(
    product,
    retailer,
    listing
):

    message = (
        "🚨🚨 POKÉMON STOCK ALERT 🚨🚨\n\n"
        f"🎴 {product}\n"
        f"🏪 {retailer}\n"
        "🟢 MATCHING LISTING AVAILABLE\n\n"
        f"📦 Listing: {listing['title']}\n"
        f"🔗 BUY NOW:\n{listing['url']}\n\n"
        f"🎯 Match score: {listing['score']:.0%}\n"
        "⚡ Detected: "
        f"{time.strftime('%Y-%m-%d %H:%M:%S %Z')}"
    )

    try:
        telegram(message)
        print(
            "📱 TELEGRAM ALERT SENT:",
            retailer,
            "|",
            product
        )

    except Exception as e:

        print(
            "TELEGRAM ERROR:",
            e
        )


# ============================================================
# ONE COMPLETE SCAN
# ============================================================

async def run_scan(browser, state):

    print()
    print("=" * 60)
    print(
        "🟢 SCAN STARTED",
        time.strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    )
    print("=" * 60)

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
                    product
                )

                key = (
                    f"{retailer}|{product}"
                )

                html, search_url, error = (
                    await search_retailer(
                        page,
                        retailer,
                        product
                    )
                )

                if error:

                    print(
                        "⚠️ SEARCH ERROR:",
                        retailer,
                        product,
                        error
                    )

                    continue

                candidates = (
                    extract_search_candidates(
                        retailer,
                        html,
                        RETAILERS[
                            retailer
                        ]["base"]
                    )
                )

                # Release the large HTML string immediately.
                del html

                matches = []

                for listing in candidates:

                    score = match_score(
                        product,
                        listing["title"],
                        listing["text"]
                    )

                    if score >= 0.90:

                        matches.append(
                            (
                                score,
                                listing
                            )
                        )

                # Release candidates once matches are made.
                del candidates

                matches.sort(
                    key=lambda x: x[0],
                    reverse=True
                )

                best_in_stock = None

                # Only inspect top 3.
                for score, listing in matches[:3]:

                    status, body, final_url = (
                        await inspect_listing(
                            page,
                            listing
                        )
                    )

                    listing["score"] = score
                    listing["status"] = status
                    listing["url"] = final_url

                    if status == "IN":

                        best_in_stock = listing

                        break

                    # Release body immediately.
                    del body

                # ------------------------------------------------
                # Determine current state correctly.
                # ------------------------------------------------

                if best_in_stock:

                    current = "IN"

                elif matches:

                    inspected_statuses = [
                        listing.get(
                            "status",
                            "UNKNOWN"
                        )
                        for _, listing
                        in matches[:3]
                    ]

                    if all(
                        status == "OUT"
                        for status
                        in inspected_statuses
                    ):
                        current = "OUT"

                    else:
                        current = "UNKNOWN"

                else:

                    current = "UNKNOWN"

                old = state.get(
                    key,
                    "UNKNOWN"
                )

                print(
                    retailer,
                    "|",
                    product,
                    "| matches:",
                    len(matches),
                    "| status:",
                    current
                )

                # ------------------------------------------------
                # Alert only on transition to IN.
                # ------------------------------------------------

                if (
                    best_in_stock
                    and old != "IN"
                ):

                    send_stock_alert(
                        product,
                        retailer,
                        best_in_stock
                    )

                # Only save known states.
                if current != "UNKNOWN":

                    state[key] = current

                    save_state(state)

                await asyncio.sleep(0.5)

        print()
        print("=" * 60)
        print(
            "✅ SCAN COMPLETE",
            time.strftime(
                "%Y-%m-%d %H:%M:%S"
            )
        )
        print(
            "⏱ Next scan in",
            INTERVAL,
            "seconds"
        )
        print(
            "🧹 Closing browser context"
        )
        print("=" * 60)

    except Exception as e:

        print(
            "🔴 SCAN ERROR:",
            repr(e)
        )

    finally:

        # --------------------------------------------------------
        # VERY IMPORTANT:
        # Close the entire context after EVERY scan.
        # This releases Chromium pages/resources.
        # --------------------------------------------------------

        try:

            if page:
                await page.close()

        except Exception:
            pass

        try:

            if context:
                await context.close()

        except Exception:
            pass


# ============================================================
# MAIN
# ============================================================

async def main():

    print("=" * 60)
    print("🟢 POKÉMON SA STOCK MONITOR STARTING")
    print(
        "Products:",
        len(WATCHLIST)
    )
    print(
        "Retailers:",
        ", ".join(RETAILERS.keys())
    )
    print(
        "Interval:",
        INTERVAL,
        "seconds"
    )
    print("=" * 60)

    state = load_state()

    async with async_playwright() as pw:

        # Browser stays alive, but the context is recycled
        # after every complete scan.
        browser = await pw.chromium.launch(
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
                    state
                )

                print(
                    "💤 Sleeping for",
                    INTERVAL,
                    "seconds..."
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
