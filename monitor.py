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
# NORMALISATION
# ============================================================

def norm(text):
    if not text:
        return ""

    text = text.lower()

    text = text.replace(
        "pokémon",
        "pokemon"
    )

    text = text.replace(
        "&",
        " and "
    )

    text = text.replace(
        "-",
        " "
    )

    text = text.replace(
        "/",
        " "
    )

    text = re.sub(
        r"[^a-z0-9\s]",
        " ",
        text
    )

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


# ============================================================
# PRODUCT MATCHING
# ============================================================

def match_score(target, candidate):
    """
    Determines whether a search result appears to be the
    requested 30th Celebration product.

    IMPORTANT:

    We do NOT allow a generic Pokémon product to become a
    confirmed match.

    The actual product page is checked later for:
        30th
        celebration
    """

    target_n = norm(target)
    candidate_n = norm(candidate)

    # --------------------------------------------------------
    # Hard 30th Celebration requirement
    # --------------------------------------------------------

    if "30th" not in candidate_n:
        return 0

    if "celebration" not in candidate_n:
        return 0

    # --------------------------------------------------------
    # Variant protection
    # --------------------------------------------------------

    for variant in VARIANTS:

        if variant in target_n:

            if variant not in candidate_n:
                return 0

    # --------------------------------------------------------
    # Product-type protection
    # --------------------------------------------------------

    product_terms = [
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

    for term in product_terms:

        if term in target_n:

            if term not in candidate_n:
                return 0

    # --------------------------------------------------------
    # Token matching
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

    target_tokens = {
        word
        for word in target_n.split()
        if word not in stop_words
    }

    candidate_tokens = set(
        candidate_n.split()
    )

    if not target_tokens:
        return 0

    matched = sum(
        1
        for token in target_tokens
        if token in candidate_tokens
    )

    score = matched / len(target_tokens)

    if score < 0.75:
        return 0

    return score


# ============================================================
# STOCK DETECTION
# ============================================================

def stock_state(text):

    t = norm(text)

    # Negative phrases FIRST.
    negative = [
        "out of stock",
        "currently unavailable",
        "sold out",
        "unavailable",
        "not available",
        "no stock",
    ]

    if any(
        phrase in t
        for phrase in negative
    ):
        return "OUT"

    positive = [
        "add to cart",
        "add to basket",
        "add to trolley",
        "buy now",
        "in stock",
        "available",
    ]

    if any(
        phrase in t
        for phrase in positive
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
            "State load error:",
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
            "State save error:",
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
    """
    Extract possible product links from retailer search results.

    We deliberately do NOT require the search-card title itself
    to contain "30th Celebration".

    Some retailers shorten search-result titles.

    The actual product page is verified later.
    """

    soup = BeautifulSoup(
        html,
        "html.parser"
    )

    candidates =
