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

if not TOKEN or not CHAT_ID:
    raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID first.")

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

STOP = {
    "pokemon", "pokémon", "tcg", "the", "and", "for", "with",
    "card", "cards", "game", "trading", "celebration"
}

VARIANTS = {
    "mewtwo", "umbreon", "espeon", "zapdos", "lucario",
    "sylveon", "greninja", "ditto"
}


def norm(s):
    s = s.lower().replace("pokémon", "pokemon")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def tokens(s):
    return set(norm(s).split())


def match_score(target, candidate):
    """
    Strict product matching.

    A listing must:
    1. Be a 30th Celebration product.
    2. Match the important product words.
    3. Match character/variant names where applicable.
    """

    def normalize(text):
        text = text.lower()
        text = text.replace("-", " ")
        text = text.replace("/", " ")
        text = text.replace("&", " and ")
        text = re.sub(r"[^a-z0-9\s]", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    target_n = normalize(target)
    candidate_n = normalize(candidate)

    # ---------------------------------------------------------
    # HARD REQUIREMENT:
    # The listing MUST be from the 30th Celebration range.
    # ---------------------------------------------------------
    if "30th" not in candidate_n:
        return 0

    if "celebration" not in candidate_n:
        return 0

    # ---------------------------------------------------------
    # Important variant/product words.
    # These MUST match when they appear in the watchlist item.
    # ---------------------------------------------------------
    required_variants = [
        "mewtwo",
        "umbreon",
        "espeon",
        "zapdos",
        "lucario",
        "sylveon",
        "greninja",
        "ditto",
    ]

    for variant in required_variants:
        if variant in target_n and variant not in candidate_n:
            return 0

    # ---------------------------------------------------------
    # Important product terms.
    # ---------------------------------------------------------
    required_terms = [
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

    for term in required_terms:
        if term in target_n and term not in candidate_n:
            return 0

    # ---------------------------------------------------------
    # Token-based similarity for the remaining words.
    # ---------------------------------------------------------
    stop_words = {
        "pokemon",
        "tcg",
        "the",
        "and",
        "card",
        "game",
        "cards",
    }

    target_tokens = {
        word for word in target_n.split()
        if word not in stop_words
    }

    candidate_tokens = set(candidate_n.split())

    if not target_tokens:
        return 0

    matched = sum(
        1 for token in target_tokens
        if token in candidate_tokens
    )

    score = matched / len(target_tokens)

    # ---------------------------------------------------------
    # Require a very strong match.
    # ---------------------------------------------------------
    if score < 0.90:
        return 0

    return score


def stock_state(text):
    t = norm(text)

    negative = [
        "out of stock", "currently unavailable", "sold out",
        "unavailable", "not available", "no stock"
    ]

    positive = [
        "add to cart", "add to basket", "add to trolley",
        "buy now", "in stock", "available"
    ]

    if any(x in t for x in negative):
        return "OUT"

    if any(x in t for x in positive):
        return "IN"

    return "UNKNOWN"


def load_state():
    return json.loads(
        STATE_FILE.read_text()
    ) if STATE_FILE.exists() else {}


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(
            state,
            indent=2,
            ensure_ascii=False
        )
    )


def telegram(message):
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        json={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": False,
        },
        timeout=20,
    )

    r.raise_for_status()


def extract_search_candidates(retailer, html, base_url):
    """
    Extract likely product cards/links from search results.

    We intentionally inspect links individually so unrelated text
    elsewhere on the page cannot cause a false match.
    """

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    candidates = []

    # Product-looking anchors are the most portable signal
    # across all 3 sites.
    for a in soup.find_all("a", href=True):

        title = a.get_text(
            " ",
            strip=True
        )

        href = urljoin(
            base_url,
            a["href"]
        )

        if len(title) < 8:
            continue

        # Avoid obvious navigation/account/cart links.
        h = href.lower()

        if any(
            x in h
            for x in [
                "/cart",
                "/account",
                "/help",
                "/customer",
                "javascript:"
            ]
        ):
            continue

        # Walk up a few levels to capture the listing/card text.
        container = a

        for _ in range(4):

            if container.parent:
                container = container.parent

        card_text = container.get_text(
            " ",
            strip=True
        )

        if len(card_text) > 2500:
            card_text = card_text[:2500]

        candidates.append(
            {
                "title": title,
                "text": card_text,
                "url": href,
            }
        )

    # Deduplicate URLs.
    seen = set()
    unique = []

    for c in candidates:

        if c["url"] in seen:
            continue

        seen.add(c["url"])
        unique.append(c)

    return unique


async def search_retailer(page, retailer, query):

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


async def inspect_listing(page, listing):
    """
    Open the matching listing itself when possible.
    This avoids declaring 'in stock' merely because the
    search-result page contains generic words.
    """

    try:

        await page.goto(
            listing["url"],
            wait_until="domcontentloaded",
            timeout=45000
        )

        await page.wait_for_timeout(
            1800
        )

        body = await page.locator(
            "body"
        ).inner_text(
            timeout=10000
        )

        html = await page.content()

        return (
            stock_state(body),
            html,
            body,
            page.url
        )

    except Exception:

        return (
            stock_state(
                listing["text"]
            ),
            "",
            listing["text"],
            listing["url"]
        )


async def main():

    state = load_state()

    async with async_playwright() as pw:

        browser = await pw.chromium.launch(
            headless=True
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

        search_page = await context.new_page()
        detail_page = await context.new_page()

        while True:

            for product in WATCHLIST:

                for retailer in RETAILERS:

                    key = (
                        f"{retailer}|{product}"
                    )

                    html, search_url, error = (
                        await search_retailer(
                            search_page,
                            retailer,
                            product
                        )
                    )

                    if error:

                        print(
                            retailer,
                            product,
                            "
