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


# ============================================================
# 30TH CELEBRATION SAFETY FILTER
# ============================================================

CELEBRATION_REQUIRED = [
    "30th celebration",
    "30th-celebration",
    "30th anniversary",
    "30th-anniversary",
]


# ============================================================
# PRODUCT TYPES
# ============================================================

PRODUCT_TERMS = [
    "elite trainer box",
    "ultra premium collection",
    "ultra-premium collection",
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
    "2-pack blister",
    "mega expansion pack",
    "expansion pack",
    "binder collection",
    "figure collection",
]


# ============================================================
# VARIANTS
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

    text = re.sub(
        r"[^a-z0-9]+",
        " ",
        text
    )

    return re.sub(
        r"\s+",
        " ",
        text
    ).strip()


def tokens(text):
    return set(
        norm(text).split()
    )


# ============================================================
# HARD 30TH CELEBRATION CHECK
# ============================================================

def is_30th_celebration(text):
    """
    HARD SAFETY FILTER.

    A listing MUST contain a recognizable 30th Celebration
    reference.

    Generic Pokémon products are rejected.
    """

    t = norm(text)

    if "30th celebration" in t:
        return True

    if "30th anniversary" in t:
        return True

    # Some retailers separate the words.
    if (
        "30th" in t
        and "celebration" in t
    ):
        return True

    return False


# ============================================================
# PRODUCT TYPE CHECK
# ============================================================

def get_product_terms(target):
    target_n = norm(target)

    return [
        term
        for term in PRODUCT_TERMS
        if norm(term) in target_n
    ]


# ============================================================
# STRICT MATCHING
# ============================================================

def match_score(
    target,
    candidate_title,
    candidate_text=""
):

    target_n = norm(target)
    title_n = norm(candidate_title)
    text_n = norm(candidate_text)

    # --------------------------------------------------------
    # ABSOLUTE REQUIREMENT:
    # PRODUCT MUST BE 30TH CELEBRATION
    # --------------------------------------------------------

    if not is_30th_celebration(
        title_n
    ):
        return 0

    # --------------------------------------------------------
    # PRODUCT TYPE
    # --------------------------------------------------------

    target_terms = get_product_terms(
        target_n
    )

    if not target_terms:
        return 0

    product_type_found = False

    for term in target_terms:

        term_n = norm(term)

        if term_n in title_n:
            product_type_found = True
            break

    if not product_type_found:
        return 0

    # --------------------------------------------------------
    # VARIANT PROTECTION
    # --------------------------------------------------------

    for variant in VARIANTS:

        if variant in target_n:

            if variant not in title_n:

                # Allow variant to appear in nearby card
                # text only if the product title is clearly
                # 30th Celebration.
                if variant not in text_n:
                    return 0

    # --------------------------------------------------------
    # TARGET TOKEN MATCH
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
        "collectible",
    }

    target_tokens = {
        word
        for word in target_n.split()
        if word not in stop_words
        and len(word) > 2
    }

    title_tokens = tokens(
        title_n + " " + text_n[:500]
    )

    if not target_tokens:
        return 0

    matched = sum(
        1
        for word in target_tokens
        if word in title_tokens
    )

    score = (
        matched /
        len(target_tokens)
    )

    # --------------------------------------------------------
    # Slightly more forgiving than previous version,
    # but still strict.
    # --------------------------------------------------------

    if score < 0.75:
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
        "temporarily out of stock",
    ]

    positive = [
        "add to cart",
        "add to basket",
        "add to trolley",
        "buy now",
        "in stock",
        "available",
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

    except Exception as e:

        print(
            "STATE LOAD ERROR:",
            e
        )

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

        print(
            "STATE SAVE ERROR:",
            e
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


# ============================================================
# SEARCH RESULT EXTRACTION
# ============================================================

def extract_search_candidates(
    retailer,
    html,
    base_url
):

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    candidates = []
    seen_urls = set()

    MAX_CANDIDATES = 120

    # --------------------------------------------------------
    # Look through links AND useful heading elements.
    # --------------------------------------------------------

    elements = soup.find_all(
        ["a", "h1", "h2", "h3", "h4"]
    )

    for element in elements:

        if len(candidates) >= MAX_CANDIDATES:
            break

        title = element.get_text(
            " ",
            strip=True
        )

        if len(title) < 8:
            continue

        href = None

        if element.name == "a":

            href = element.get(
                "href"
            )

        else:

            parent = element.find_parent(
                "a",
                href=True
            )

            if parent:

                href = parent.get(
                    "href"
                )

        if not href:
            continue

        href = urljoin(
            base_url,
            href
        )

        lower_href = href.lower()

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

        container = element

        for _ in range(3):

            if container.parent:

                container = container.parent

        card_text = container.get_text(
            " ",
            strip=True
        )

        card_text = card_text[:1200]

        # ----------------------------------------------------
        # Early 30th Celebration filter.
        # This prevents generic tins from entering matches.
        # ----------------------------------------------------

        if not is_30th_celebration(
            title + " " + card_text
        ):
            continue

        candidates.append(
            {
                "title": title[:400],
                "text": card_text,
                "url": href,
            }
        )

    return candidates


# ============================================================
# PLAYWRIGHT RESOURCE CONTROL
# ============================================================

async def block_heavy_resources(route):

    resource_type = (
        route.request.resource_type
    )

    if resource_type in {
        "image",
        "media",
        "font",
    }:

        await route.abort()

    else:

        await route.continue_()


async def create_browser_context(
    browser
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

    template = RETAILERS[
        retailer
    ]["search"]

    url = template.format(
        query=quote_plus(query)
    )

    try:

        await page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=45000
        )

        await page.wait_for_timeout(
            2500
        )

        html = await page.content()

        return (
            html,
            page.url,
            None
        )

    except Exception as e:

        return (
            "",
            url,
            str(e)
        )


# ============================================================
# PRODUCT PAGE INSPECTION
# ============================================================

async def inspect_listing(
    page,
    listing,
    target_product
):

    try:

        await page.goto(
            listing["url"],
            wait_until="domcontentloaded",
            timeout=45000
        )

        await page.wait_for_timeout(
            1500
        )

        body = await page.locator(
            "body"
        ).inner_text(
            timeout=10000
        )

        body = body[:18000]

        # ----------------------------------------------------
        # IMPORTANT:
        # Verify the product page itself is still 30th
        # Celebration.
        # ----------------------------------------------------

        if not is_30th_celebration(
            body[:8000]
        ):

            return (
                "UNKNOWN",
                body,
                page.url
            )

        # ----------------------------------------------------
        # Verify product match on actual page.
        # ----------------------------------------------------

        page_score = match_score(
            target_product,
            body[:2000],
            body[:8000]
        )

        if page_score <= 0:

            return (
                "UNKNOWN",
                body,
                page.url
            )

        return (
            stock_state(body),
            body,
            page.url
        )

    except Exception:

        return (
            "UNKNOWN",
            listing.get(
                "text",
                ""
            ),
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
        "🚨🚨 POKÉMON 30TH CELEBRATION STOCK ALERT 🚨🚨\n\n"
        f"🎴 {product}\n"
        f"🏪 {retailer}\n\n"
        "🟢 MATCHING 30TH CELEBRATION LISTING AVAILABLE\n\n"
        f"📦 {listing['title']}\n\n"
        f"🔗 BUY NOW:\n{listing['url']}\n\n"
        f"🎯 Match score: {listing['score']:.0%}\n"
        f"⚡ Detected: "
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

async def run_scan(
    browser,
    state
):

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

                del html

                matches = []

                for listing in candidates:

                                        score = match_score(
                        product,
                        listing["title"],
                        listing["text"]
                    )

                    if score >= 0.75:

                        listing["score"] = score

                        matches.append(
                            listing
                        )

                del candidates

                matches.sort(
                    key=lambda x: x["score"],
                    reverse=True
                )

                best_in_stock = None
                inspected_statuses = []

                # ------------------------------------------------
                # Inspect top 5 potential matches.
                # ------------------------------------------------

                for listing in matches[:5]:

                    status, body, final_url = (
                        await inspect_listing(
                            page,
                            listing,
                            product
                        )
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
