# Pokémon Cross-Retailer Stock Alert Bot

This version uses the products you supplied as a **master watchlist**.

The bot is designed around the important rule you requested:

> If a Pokémon product on the master list appears at another retailer, alert me — even if I originally found it on a different retailer.

For example, if the Amazon list contains:
`Pokémon TCG 30th Celebration Elite Trainer Box`

the bot can search/check:
- Amazon
- Takealot
- Checkers

and alert when a matching product becomes available.

## Matching
Product names are normalised so differences such as:
- Pokémon / Pokemon
- TCG punctuation
- hyphens
- capitalisation
- extra retailer wording

do not prevent a match.

Specific character/product variants such as Mewtwo, Umbreon, Espeon, Zapdos, Lucario, Sylveon/Greninja are retained so the bot does not accidentally treat them as the generic Mini Tin.

## Important
Retailer pages are dynamic and may use anti-bot protection or location-specific stock. The monitor therefore treats uncertain pages as UNKNOWN rather than falsely claiming stock.

The production version should use retailer search/result pages in addition to the supplied product URLs. This is preferable to assuming a product has the same URL on every retailer.

## Alert behaviour
Only alert on:
UNKNOWN/OUT -> IN

No repeated alerts while a product remains in stock.


## Version 3 — retailer-search monitoring

The monitor no longer depends on the original product URLs.

For every product in `watchlist.json`, it:
1. Builds a search query.
2. Searches Takealot, Checkers and Amazon.
3. Extracts individual product listings from the search results.
4. Uses conservative fuzzy matching against each listing.
5. Protects named variants (Mewtwo, Umbreon, Espeon, Zapdos, Lucario, Sylveon, Greninja, Ditto).
6. Opens the best matching listing to confirm availability.
7. Alerts Telegram when a matching listing is confirmed available.
8. Does not repeatedly alert while that retailer/product remains available.

This means a product discovered originally on Amazon can trigger an alert when the same product first appears on Takealot or Checkers.

The search URL templates remain environment variables because retailer search endpoints can change. The supplied defaults are the current starting points; test them against the live sites before relying on the monitor for time-critical drops.
