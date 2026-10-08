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


# ============================================================
# PRODUCT MATCHING
# ============================================================

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
    "binder collection",
    "figure collection",
]


def norm(text):
    if not text:
        return ""

    text = text.lower()
    text = text.replace("pokémon", "pokemon")
    text = text.replace("&", " and ")
    text = text.replace("-", " ")
    text = text.replace("/", " ")

    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def match_score(target, candidate):
    """
    VERY IMPORTANT:

    A candidate MUST clearly belong to the
    Pokémon TCG 30th Celebration range.

    Generic Pokémon tins/products are rejected.
    """

    target_n = norm(target)
    candidate_n = norm(candidate)

    # --------------------------------------------------------
    # HARD 30TH CELEBRATION REQUIREMENT
    # --------------------------------------------------------

    if "30th" not in candidate_n:
        return 0

    if "celebration" not in candidate_n:
        return 0

    # --------------------------------------------------------
    # VARIANT PROTECTION
    # --------------------------------------------------------

    for variant in VARIANTS:
        if variant in target_n:
            if variant not in candidate_n:
                return 0

    # --------------------------------------------------------
    # PRODUCT TYPE PROTECTION
    # --------------------------------------------------------

    for term in PRODUCT_TERMS:
        if term in target_n:
            if term not in candidate_n:
                return 0

    # --------------------------------------------------------
    # PRODUCT-SPECIFIC WORD MATCHING
    # --------------------------------------------------------

    stop_words = {
        "pokemon",
        "tcg",
        "the",
        "and",
        "for",
        "with",
        "card",
        "cards",
        "game",
        "trading",
        "30th",
        "celebration",
    }

    target_words = [
        word
        for word in target_n.split()
        if word not in stop_words
    ]

    if not target_words:
        return 0

    matched = 0

    for word in target_words:
        if word in candidate_n.split():
            matched += 1

    score = matched / len(target_words)

    # Strong match required
    if score < 0.75:
        return 0

    return score


# ============================================================
# STOCK DETECTION
# ============================================================

def stock_state(text):
    t = norm(text)

    # Check negative FIRST.
    negative = [
        "out of stock",
        "currently unavailable",
        "sold out",
        "unavailable",
        "not available",
        "no stock",
    ]

    for phrase in negative:
        if phrase in t:
            return "OUT"

    positive = [
        "add to cart",
        "add to basket",
        "add to trolley",
        "buy now",
        "in stock",
        "available",
    ]

    for phrase in positive:
        if phrase in t:
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
    except Exception as e:
        print("State load error:", e)

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
        print("State save error:", e)


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

def extract_search_candidates(html, base_url):
    """
    Extract product links from retailer search results.

    We keep the amount of data small to prevent Render
    memory usage from growing.
    """

    soup = BeautifulSoup(html, "html.parser")

    candidates = []
    seen_urls = set()

    for a in soup.find_all("a", href=True):

        title = a.get_text(" ", strip=True)

        if len(title) < 8:
            continue

        href = urljoin(
            base_url,
            a.get("href", "")
        )

        if not href.startswith("http"):
            continue

        href_lower = href.lower()

        # Ignore navigation
        if any(
            x in href_lower
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

        # Get a SMALL amount of surrounding product-card text.
        parent = a.parent

        card_text = title

        if parent:
            parent_text = parent.get_text(
                " ",
                strip=True
            )

            if len(parent_text) <= 1000:
                card_text = parent_text

        candidates.append(
            {
                "title": title[:500],
                "text": card_text[:1200],
                "url": href,
            }
        )

        # Prevent enormous search-result lists.
        if len(candidates) >= 50:
            break

    return candidates


# ============================================================
# SEARCH RETAILER
# ============================================================

async def search_retailer(page, retailer, query):

    template = RETAILERS[retailer]["search"]

    url = template.format(
        query=quote_plus(query)
    )

    try:

        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=30000,
        )

        # Short wait for JS-rendered products.
        await page.wait_for_timeout(1200)

        html = await page.content()

        return html, page.url, None

    except Exception as e:

        return "", url, str(e)


# ============================================================
# INSPECT PRODUCT
# ============================================================

async def inspect_listing(page, listing):

    try:

        await page.goto(
            listing["url"],
            wait_until="domcontentloaded",
            timeout=30000,
        )

        await page.wait_for_timeout(800)

        body = await page.locator(
            "body"
        ).inner_text(
            timeout=8000
        )

        # Limit memory.
        body = body[:15000]

        return (
            stock_state(body),
            body,
            page.url,
        )

    except Exception:

        # If product page cannot be opened,
        # fall back to search-result text.
        return (
            stock_state(listing["text"]),
            listing["text"],
            listing["url"],
        )


# ============================================================
# BROWSER
# ============================================================

async def create_browser(pw):

    browser = await pw.chromium.launch(
        headless=True,
        args=[
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--no-sandbox",
        ],
    )

    context = await browser.new_context(
        locale="en-ZA",
        timezone_id="Africa/Johannesburg",
        user_agent=(
            "Mozilla/5.0 (Linux; Android 14) "
            "AppleWebKit/537.36 "
            "(KHTML, like Gecko) "
            "Chrome/140.0 Mobile Safari/537.36"
        ),
    )

    # Block heavy resources.
    async def block_heavy(route):

        resource_type = route.request.resource_type

        if resource_type in {
            "image",
            "media",
            "font",
        }:
            await route.abort()
        else:
            await route.continue_()

    await context.route(
        "**/*",
        block_heavy
    )

    page = await context.new_page()

    return browser, context, page


# ============================================================
# MAIN SCAN
# ============================================================

async def main():

    print("")
    print("==========================================")
    print("🚀 POKÉMON STOCK ALERT BOT")
    print("==========================================")
    print(
        f"Products: {len(WATCHLIST)}"
    )
    print(
        f"Retailers: {len(RETAILERS)}"
    )
    print(
        f"Scan interval: {INTERVAL} seconds"
    )
    print("30th Celebration filter: ON")
    print("Memory optimisation: ON")
    print("==========================================")
    print("")

    state = load_state()

    async with async_playwright() as pw:

        browser, context, page = await create_browser(
            pw
        )

        try:

            while True:

                scan_started = time.time()

                print("")
                print(
                    "========== NEW SCAN =========="
                )
                print(
                    time.strftime(
                        "%Y-%m-%d %H:%M:%S %Z"
                    )
                )

                for product in WATCHLIST:

                    print("")
                    print(
                        f"🎴 {product}"
                    )

                    for retailer in RETAILERS:

                        key = (
                            f"{retailer}|{product}"
                        )

                        # ------------------------------------------------
                        # SEARCH
                        # ------------------------------------------------

                        html, search_url, error = (
                            await search_retailer(
                                page,
                                retailer,
                                product,
                            )
                        )

                        if error:

                            print(
                                f" 
