import asyncio
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import quote_plus, urljoin

import requests
from playwright.async_api import async_playwright


# ============================================================
# CONFIGURATION
# ============================================================

WATCHLIST_FILE = Path("watchlist.json")
STATE_FILE = Path("state.json")

WATCHLIST = json.loads(
    WATCHLIST_FILE.read_text(encoding="utf-8")
)

INTERVAL = int(
    os.getenv("CHECK_INTERVAL_SECONDS", "60")
)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

if not TOKEN or not CHAT_ID:
    raise SystemExit(
        "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID."
    )


# ============================================================
# RETAILERS
# ============================================================

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
# 30TH CELEBRATION SAFETY
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


def is_30th_celebration(text):
    """
    HARD SAFETY FILTER.

    A product must explicitly contain:
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
# PRODUCT TYPES
# ============================================================

PRODUCT_ALIASES = {
    "elite trainer box": [
        "elite trainer box",
        "etb",
    ],

    "ultra premium collection": [
        "ultra premium collection",
        "ultra premium",
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


# ============================================================
# PRODUCT MATCHING
# ============================================================

def target_product_type(target):
    target_n = norm(target)

    for product_type in PRODUCT_ALIASES:

        if product_type in target_n:
            return product_type

    return None


def product_type_matches(
    target,
    candidate
):
    target_n = norm(target)
    candidate_n = norm(candidate)

    product_type = target_product_type(
        target_n
    )

    if not product_type:
        return False

    aliases = PRODUCT_ALIASES[
        product_type
    ]

    return any(
        norm(alias) in candidate_n
        for alias in aliases
    )


def variants_required(target):
    target_n = norm(target)

    return {
        variant
        for variant in VARIANTS
        if variant in target_n
    }


def variant_matches(
    target,
    candidate
):
    candidate_n = norm(candidate)

    required = variants_required(
        target
    )

    for variant in required:

        if variant not in candidate_n:
            return False

    return True


def match_score(
    target,
    title,
    surrounding_text=""
):
    """
    Returns:
        0.0 = not a match
        >0 = valid 30th Celebration match
    """

    combined = (
        f"{title} "
        f"{surrounding_text}"
    )

    # --------------------------------------------------------
    # HARD 30TH CELEBRATION FILTER
    # --------------------------------------------------------

    if not is_30th_celebration(
        combined
    ):
        return 0.0

    # --------------------------------------------------------
    # PRODUCT TYPE
    # --------------------------------------------------------

    if not product_type_matches(
        target,
        combined
    ):
        return 0.0

    # --------------------------------------------------------
    # VARIANT
    # --------------------------------------------------------

    if not variant_matches(
        target,
        combined
    ):
        return 0.0

    # --------------------------------------------------------
    # TOKEN SCORING
    # --------------------------------------------------------

    target_n = norm(target)
    candidate_n = norm(combined)

    stop_words = {
        "pokemon",
        "tcg",
        "the",
        "and",
        "card",
        "cards",
        "game",
        "trading",
        "30th",
        "celebration",
    }

    target_tokens = {
        token
        for token in target_n.split()
        if token not in stop_words
        and len(token) > 2
    }

    candidate_tokens = set(
        candidate_n.split()
    )

    if not target_tokens:
        return 0.0

    matched = sum(
        1
        for token in target_tokens
        if token in candidate_tokens
    )

    score = (
        matched /
        len(target_tokens)
    )

    if score < 0.65:
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
    for phrase in negative:

        if phrase in t:
            return "OUT"

    for phrase in positive:

        if phrase in t:
            return "IN"

    return "UNKNOWN"


# ============================================================
# STATE
# ============================================================

def load_state():

    if not STATE_FILE.exists():
        return {}

    try:

        return json.loads(
            STATE_FILE.read_text(
                encoding="utf-8"
            )
        )

    except Exception as exc:

        print(
            "⚠️ STATE LOAD ERROR:",
            repr(exc)
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

    except Exception as exc:

        print(
            "⚠️ STATE SAVE ERROR:",
            repr(exc)
        )


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):

    url = (
        f"https://api.telegram.org/"
        f"bot{TOKEN}/sendMessage"
    )

    response = requests.post(
        url,
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
    listing
):

    message = (
        "🚨 POKÉMON 30TH CELEBRATION "
        "STOCK ALERT 🚨\n\n"
        f"🎴 {product}\n"
        f"🏪 {retailer}\n"
        f"📦 {listing['title']}\n\n"
        "🟢 IN STOCK\n\n"
        f"🔗 BUY NOW:\n"
        f"{listing['url']}\n\n"
        f"🎯 Match: "
        f"{listing['score']:.0%}\n"
        f"⏰ "
        f"{time.strftime('%Y-%m-%d %H:%M:%S %Z')}"
    )

    try:

        send_telegram(
            message
        )

        print(
            "📱 TELEGRAM ALERT SENT:",
            retailer,
            "|",
            product
        )

    except Exception as exc:

        print(
            "❌ TELEGRAM ERROR:",
            repr(exc)
        )


# ============================================================
# LOW-MEMORY PLAYWRIGHT
# ============================================================

async def block_heavy_resources(
    route
):

    resource_type = (
        route.request.resource_type
    )

    if resource_type in {
        "image",
        "media",
        "font",
        "websocket",
    }:

        await route.abort()

    else:

        await route.continue_()


async def launch_browser(
    playwright
):

    browser = await playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-features=Translate",
            "--disable-sync",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    )

    return browser


async def create_context(
    browser
):

    context = await browser.new_context(
        locale="en-ZA",
        timezone_id="Africa/Johannesburg",
        service_workers="block",
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
# SEARCH PAGE
# ============================================================

async def search_retailer(
    page,
    retailer,
    product
):

    template = RETAILERS[
        retailer
    ]["search"]

    search_url = template.format(
        query=quote_plus(product)
    )

    try:

        await page.goto(
            search_url,
            wait_until="domcontentloaded",
            timeout=30000
        )

        await page.wait_for_timeout(
            1500
        )

        return (
            True,
            page.url,
            None
        )

    except Exception as exc:

        return (
            False,
            search_url,
            str(exc)
        )


# ============================================================
# EXTRACT SEARCH RESULTS
# ============================================================

async def extract_candidates(
    page,
    retailer
):
    """
    Extract a small number of links directly from
    the DOM.

    We deliberately DO NOT call page.content().
    """

    try:

        results = await page.locator(
            "a[href]"
        ).evaluate_all(
            """
            (links) => {

                const output = [];
                const seen = new Set();

                for (const link of links) {

                    if (output.length >= 100) {
                        break;
                    }

                    const href =
                        link.href || "";

                    if (!href) {
                        continue;
                    }

                    if (seen.has(href)) {
                        continue;
                    }

                    seen.add(href);

                    let title =
                        (link.innerText || "")
                        .replace(/\\s+/g, " ")
                        .trim();

                    if (title.length < 5) {
                        continue;
                    }

                    let node = link;
                    let surrounding = "";

                    for (
                        let i = 0;
                        i < 3 && node;
                        i++
                    ) {

                        if (node.innerText) {

                            surrounding =
                                node.innerText
                                .replace(
                                    /\\s+/g,
                                    " "
                                )
                                .trim();

                            if (
                                surrounding.length
                                >= 20
                            ) {
                                break;
                            }
                        }

                        node = node.parentElement;
                    }

                    output.push({
                        title:
                            title.slice(0, 400),

                        text:
                            surrounding.slice(
                                0,
                                1200
                            ),

                        url: href
                    });
                }

                return output;
            }
            """
        )

        # ----------------------------------------------------
        # Hard cap to keep memory low.
        # ----------------------------------------------------

        results = results[:100]

        return results

    except Exception as exc:

        print(
            "⚠️ RESULT EXTRACTION ERROR:",
            retailer,
            repr(exc)
        )

        return []


# ============================================================
# PRODUCT PAGE CHECK
# ============================================================

async def inspect_product(
    page,
    retailer,
    listing,
    target_product
):

    try:

        await page.goto(
            listing["url"],
            wait_until="domcontentloaded",
            timeout=30000
        )

        await page.wait_for_timeout(
            1200
        )

        # ----------------------------------------------------
        # Only retrieve visible body text.
        # No page.content().
        # ----------------------------------------------------

        body = await page.locator(
            "body"
        ).inner_text(
            timeout=8000
        )

        body = body[:12000]

        # ----------------------------------------------------
        # HARD 30TH CELEBRATION CHECK
        # ----------------------------------------------------

        if not is_30th_celebration(
            body
        ):

            return (
                "UNKNOWN",
                page.url,
                "Product page is not confirmed as 30th Celebration."
            )

        # ----------------------------------------------------
        # PRODUCT MATCH CHECK
        # ----------------------------------------------------

        score = match_score(
            target_product,
            body[:4000],
            body[:8000]
        )

        if score <= 0:

            return (
                "UNKNOWN",
                page.url,
                "Product page did not match watchlist item."
            )

        # ----------------------------------------------------
        # STOCK
        # ----------------------------------------------------

        status = stock_state(
            body
        )

        return (
            status,
            page.url,
            ""
        )

    except Exception as exc:

        return (
            "UNKNOWN",
            listing.get(
                "url",
                ""
            ),
            repr(exc)
        )


# ============================================================
# SCAN ONE RETAILER
# ============================================================

async def scan_retailer(
    playwright,
    retailer,
    state
):

    print()
    print(
        "=" * 60
    )

    print(
        "🏪 STARTING RETAILER:",
        retailer
    )

    print(
        "🧠 Memory strategy:",
        "browser recycled after retailer"
    )

    print(
        "=" * 60
    )

    browser = None
    context = None

    try:

        browser = await launch_browser(
            playwright
        )

        context = await create_context(
            browser
        )

        page = await context.new_page()

        for index, product in enumerate(
            WATCHLIST,
            start=1
        ):

            print(
                f"🔎 [{index}/{len(WATCHLIST)}]",
                retailer,
                "|",
                product
            )

            key = (
                f"{retailer}|{product}"
            )

            success, final_url, error = (
                await search_retailer(
                    page,
                    retailer,
                    product
                )
            )

            if not success:

                print(
                    "⚠️ SEARCH ERROR:",
                    retailer,
                    product,
                    error
                )

                continue

            # ------------------------------------------------
            # Extract lightweight candidates.
            # ------------------------------------------------

            candidates = (
                await extract_candidates(
                    page,
                    retailer
                )
            )

            # ------------------------------------------------
            # Score candidates.
            # ------------------------------------------------

            matches = []

            for listing in candidates:

                score = match_score(
                    product,
                    listing.get(
                        "title",
                        ""
                    ),
                    listing.get(
                        "text",
                        ""
                    )
                )

                if score > 0:

                    listing["score"] = (
                        score
                    )

                    matches.append(
                        listing
                    )

            # Release candidate list ASAP.
            del candidates

            matches.sort(
                key=lambda item:
                item["score"],
                reverse=True
            )

            # ------------------------------------------------
            # Diagnostic information.
            # ------------------------------------------------

            print(
                "   Candidate matches:",
                len(matches)
            )

            if matches:

                for listing in matches[:3]:

                    print(
                        "   ➜",
                        listing["title"][:100],
                        "|",
                        f"{listing['score']:.0%}"
                    )

            else:

                print(
                    "   ⚪ No confirmed "
                    "30th Celebration match."
                )

            # ------------------------------------------------
            # Inspect only top 3.
            # ------------------------------------------------

            best_in_stock = None
            statuses = []

            for listing in matches[:3]:

                (
                    status,
                    product_url,
                    diagnostic
                ) = await inspect_product(
                    page,
                    retailer,
                    listing,
                    product
                )

                listing["status"] = (
                    status
                )

                listing["url"] = (
                    product_url
                )

                statuses.append(
                    status
                )

                print(
                    "   📦",
                    listing["title"][:80],
                    "|",
                    status
                )

                if diagnostic:

                    print(
                        "   ℹ️",
                        diagnostic
                    )

                if status == "IN":

                    best_in_stock = (
                        listing
                    )

                    break

            # ------------------------------------------------
            # Determine current state.
            # ------------------------------------------------

            if best_in_stock:

                current = "IN"

            elif (
                statuses
                and all(
                    status == "OUT"
                    for status in statuses
                )
            ):

                current = "OUT"

            elif matches:

                current = "UNKNOWN"

            else:

                current = "UNKNOWN"

            old = state.get(
                key,
                "UNKNOWN"
            )

            print(
                "   RESULT:",
                retailer,
                "|",
                product,
                "| matches:",
                len(matches),
                "| status:",
                current
            )

            # ------------------------------------------------
            # Duplicate suppression.
            #
            # Alert only when transitioning to IN.
            # ------------------------------------------------

            if (
                best_in_stock is not None
                and old != "IN"
            ):

                send_stock_alert(
                    product,
                    retailer,
                    best_in_stock
                )

            # ------------------------------------------------
            # Save known state.
            # ------------------------------------------------

            if current != "UNKNOWN":

                state[key] = current

                save_state(
                    state
                )

            # ------------------------------------------------
            # Close page between products.
            # ------------------------------------------------

            try:

                await page.close()

            except Exception:

                pass

            # Create a fresh page.
            page = await context.new_page()

            # Small pause.
            await asyncio.sleep(
                0.4
            )

            # ------------------------------------------------
            # Extra safety:
            # recycle Chromium every 10 products.
            # ------------------------------------------------

            if (
                index % 10 == 0
                and index < len(WATCHLIST)
            ):

                print(
                    "♻️ Recycling Chromium "
                    "after 10 products..."
                )

                try:

                    await page.close()

                except Exception:

                    pass

                try:

                    await context.close()

                except Exception:

                    pass

                try:

                    await browser.close()

                except Exception:

                    pass

                browser = await launch_browser(
                    playwright
                )

                context = await create_context(
                    browser
                )

                page = await context.new_page()

        # End product loop.

    except Exception as exc:

        print(
            "🔴 RETAILER SCAN ERROR:",
            retailer,
            repr(exc)
        )

    finally:

        # ----------------------------------------------------
        # VERY IMPORTANT:
        # Completely destroy retailer browser.
        # ----------------------------------------------------

        try:

            if context is not None:

                await context.close()

        except Exception:

            pass

        try:

            if browser is not None:

                await browser.close()

        except Exception:

            pass

        print(
            "🧹 CLOSED RETAILER:",
            retailer
        )


# ============================================================
# COMPLETE SCAN
# ============================================================

async def run_scan(
    playwright,
    state
):

    print()
    print(
        "=" * 70
    )

    print(
        "🟢 SCAN STARTED",
        time.strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    )

    print(
        "🎯 HARD FILTER:",
        "30TH CELEBRATION ONLY"
    )

    print(
        "📦 PRODUCTS:",
        len(WATCHLIST)
    )

    print(
        "🏪 RETAILERS:",
        ", ".join(
            RETAILERS.keys()
        )
    )

    print(
        "=" * 70
    )

    # --------------------------------------------------------
    # CRITICAL MEMORY RULE:
    # Retailers are processed ONE AT A TIME.
    # --------------------------------------------------------

    for retailer in RETAILERS:

        await scan_retailer(
            playwright,
            retailer,
            state
        )

        # Give OS time to reclaim resources.
        await asyncio.sleep(
            2
        )

    print()
    print(
        "=" * 70
    )

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
        "=" * 70
    )


# ============================================================
# MAIN
# ============================================================
