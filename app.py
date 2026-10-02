import os
import re
import sqlite3
import urllib.parse
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, Response


# =========================================================
# CARD RADAR
# VERSION 6.2
# =========================================================

VERSION = "6.2"

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "cardradar.db")

CZK_PER_EUR = 24.4618

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "sk-SK,sk;q=0.9,cs;q=0.8,en;q=0.7",
}


# =========================================================
# DATABASE
# =========================================================

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT,
            shop TEXT,
            title TEXT,
            price_eur REAL,
            checked_at TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS discovered_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            shop TEXT,
            country TEXT,
            title TEXT,
            url TEXT,
            price_eur REAL,
            condition TEXT,
            discovered_at TEXT
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS suggestion_cache (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT UNIQUE,
            normalized TEXT,
            suggestion_type TEXT,
            updated_at TEXT
        )
    """)

    conn.commit()
    conn.close()


init_db()


# =========================================================
# BASIC HELPERS
# =========================================================

def now_iso():
    return datetime.now(timezone.utc).isoformat()


def clean_text(value):
    if not value:
        return ""

    value = str(value)
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def normalize_spaces(value):
    return clean_text(value)


def words(value):
    return re.findall(r"[a-z0-9áäčďéíľĺňóôŕšťúýž]+", value.lower())


def normalize_compare(value):
    value = clean_text(value).lower()

    replacements = {
        "á": "a",
        "ä": "a",
        "č": "c",
        "ď": "d",
        "é": "e",
        "í": "i",
        "ľ": "l",
        "ĺ": "l",
        "ň": "n",
        "ó": "o",
        "ô": "o",
        "ŕ": "r",
        "š": "s",
        "ť": "t",
        "ú": "u",
        "ý": "y",
        "ž": "z",
    }

    for old, new in replacements.items():
        value = value.replace(old, new)

    value = re.sub(r"[^a-z0-9/]+", " ", value)
    return normalize_spaces(value)


def absolute_url(base, href):
    if not href:
        return ""

    return urllib.parse.urljoin(base, href)


def get(url, timeout=15):
    try:
        response = requests.get(
            url,
            headers=HEADERS,
            timeout=timeout,
            allow_redirects=True,
        )

        if response.status_code >= 400:
            return None

        return response

    except requests.RequestException:
        return None


# =========================================================
# ALIASES
# =========================================================

SET_ALIASES = {
    "sv1": "Scarlet & Violet Base",
    "scarlet violet": "Scarlet & Violet Base",

    "sv2": "Paldea Evolved",
    "paldea evolved": "Paldea Evolved",

    "sv3": "Obsidian Flames",
    "obsidian flames": "Obsidian Flames",

    "sv4": "Paradox Rift",
    "paradox rift": "Paradox Rift",

    "sv5": "Temporal Forces",
    "temporal forces": "Temporal Forces",

    "sv6": "Twilight Masquerade",
    "twilight masquerade": "Twilight Masquerade",

    "sv7": "Stellar Crown",
    "stellar crown": "Stellar Crown",

    "sv8": "Surging Sparks",
    "surging sparks": "Surging Sparks",

    "sv8a": "Terastal Festival",
    "terastal festival": "Terastal Festival",

    "sv9": "Journey Together",
    "journey together": "Journey Together",

    "sv9a": "Destined Rivals",
    "destined rivals": "Destined Rivals",

    "sv10": "Destined Rivals",
    "sv10.5": "Destined Rivals",

    "sv11": "Black Bolt White Flare",
    "black bolt": "Black Bolt",
    "white flare": "White Flare",

    "151": "Pokémon 151",
    "pokemon 151": "Pokémon 151",

    "prismatic evolutions": "Prismatic Evolutions",

    "phantasmal flames": "Phantasmal Flames",
    "pfl": "Phantasmal Flames",

    "paldean fates": "Paldean Fates",

    "mega evolution": "Mega Evolution",
    "me01": "Mega Evolution",
    "me02": "Phantasmal Flames",
}


POKEMON_ALIASES = {
    "pikachu": "Pikachu",
    "charizard": "Charizard",
    "mega charizard x": "Mega Charizard X",
    "mega charizard": "Mega Charizard",
    "umbreon": "Umbreon",
    "eevee": "Eevee",
    "vaporeon": "Vaporeon",
    "jolteon": "Jolteon",
    "flareon": "Flareon",
    "espeon": "Espeon",
    "glaceon": "Glaceon",
    "leafeon": "Leafeon",
    "sylveon": "Sylveon",
    "mew": "Mew",
    "mewtwo": "Mewtwo",
    "gengar": "Gengar",
    "lucario": "Lucario",
    "greninja": "Greninja",
    "rayquaza": "Rayquaza",
    "gardevoir": "Gardevoir",
    "dragonite": "Dragonite",
    "gyarados": "Gyarados",
    "blastoise": "Blastoise",
    "venusaur": "Venusaur",
    "lugia": "Lugia",
    "ho oh": "Ho-Oh",
    "arceus": "Arceus",
    "dialga": "Dialga",
    "palkia": "Palkia",
    "zekrom": "Zekrom",
    "reshiram": "Reshiram",
    "celebi": "Celebi",
}


PRODUCT_ALIASES = {
    "etb": "Elite Trainer Box",
    "elite trainer box": "Elite Trainer Box",

    "booster box": "Booster Box",
    "boosterbox": "Booster Box",
    "bb": "Booster Box",

    "booster bundle": "Booster Bundle",
    "bundle": "Booster Bundle",

    "collection box": "Collection Box",
    "collection": "Collection Box",

    "premium collection": "Premium Collection",
    "premium": "Premium Collection",

    "blister": "Blister",
    "tin": "Tin",
    "box": "Box",
}


MERCH_WORDS = [
    "plush",
    "plys",
    "peluche",
    "figurka",
    "figure",
    "figurine",
    "mug",
    "hrncek",
    "shirt",
    "tricko",
    "tričko",
    "hoodie",
    "mikina",
    "socks",
    "ponozky",
    "ponožky",
    "puzzle",
    "playmat",
    "podlozka",
    "podložka",
    "album",
    "binder",
    "sleeves",
    "obaly",
    "obal",
    "keychain",
    "klucenka",
    "kľúčenka",
    "backpack",
    "batoh",
    "bag",
    "taska",
    "taška",
    "poster",
    "sticker",
    "samolep",
    "coin",
]


SEALED_BLOCK_WORDS = [
    "case",
    "10x",
    "12x",
    "6x",
    "display",
    "carton",
]


# =========================================================
# QUERY PARSING
# =========================================================

def detect_product_type(query):
    q = normalize_compare(query)

    for alias, canonical in PRODUCT_ALIASES.items():
        if alias in q:
            return canonical

    return "Card"


def detect_set(query):
    q = normalize_compare(query)

    # Longer aliases first.
    for alias in sorted(SET_ALIASES.keys(), key=len, reverse=True):
        if normalize_compare(alias) in q:
            return SET_ALIASES[alias]

    return ""


def detect_pokemon(query):
    q = normalize_compare(query)

    for alias in sorted(POKEMON_ALIASES.keys(), key=len, reverse=True):
        if normalize_compare(alias) in q:
            return POKEMON_ALIASES[alias]

    return ""


def parse_query(query):
    original = clean_text(query)
    q = normalize_compare(original)

    card_number = ""

    match = re.search(r"\b(\d{1,3}\s*/\s*\d{1,3})\b", original)
    if match:
        card_number = match.group(1).replace(" ", "")

    product_type = detect_product_type(original)
    set_name = detect_set(original)
    pokemon = detect_pokemon(original)

    suffix = ""

    for s in ["vmax", "vstar", "ex", "gx", "v"]:
        if re.search(r"\b" + re.escape(s) + r"\b", q):
            suffix = s.upper()
            break

    normalized_parts = []

    if pokemon:
        normalized_parts.append(pokemon)

    if suffix:
        normalized_parts.append(suffix)

    if card_number:
        normalized_parts.append(card_number)

    if set_name:
        normalized_parts.append(set_name)

    if product_type != "Card":
        normalized_parts.append(product_type)

    # Generic fallback.
    if not normalized_parts:
        cleaned = q

        for bad in [
            "pokemon",
            "pokémon",
            "card",
            "karta",
            "full art",
        ]:
            cleaned = re.sub(
                r"\b" + re.escape(normalize_compare(bad)) + r"\b",
                " ",
                cleaned,
            )

        cleaned = normalize_spaces(cleaned)

        if cleaned:
            normalized_parts.append(cleaned)

    normalized_query = normalize_spaces(" ".join(normalized_parts))

    return {
        "original": original,
        "normalized": normalized_query,
        "product_type": product_type,
        "set": set_name,
        "pokemon": pokemon,
        "card_number": card_number,
        "suffix": suffix,
    }


# =========================================================
# PRODUCT CLASSIFICATION
# =========================================================

def contains_merch(title):
    q = normalize_compare(title)

    for word in MERCH_WORDS:
        if normalize_compare(word) in q:
            return True

    return False


def is_sealed_title(title):
    q = normalize_compare(title)

    sealed_words = [
        "elite trainer box",
        "booster box",
        "booster bundle",
        "collection box",
        "premium collection",
        "blister",
        "tin",
        "display",
    ]

    return any(word in q for word in sealed_words)


def parse_price(value):
    if value is None:
        return None

    text = clean_text(value).lower()

    text = text.replace("eur", "€")
    text = text.replace("kč", " czk")
    text = text.replace("kc", " czk")

    # Remove spaces around numbers.
    text = text.replace(" ", "")

    match = re.search(
        r"(\d{1,6}(?:[.,]\d{1,2})?)",
        text
    )

    if not match:
        return None

    number = match.group(1)

    # Czech style: 1 299,90 already lost spaces above.
    if "," in number and "." in number:
        if number.rfind(",") > number.rfind("."):
            number = number.replace(".", "").replace(",", ".")
        else:
            number = number.replace(",", "")

    elif "," in number:
        number = number.replace(",", ".")

    try:
        price = float(number)
    except ValueError:
        return None

    if "czk" in text or "kč" in text or " kc" in text:
        price = price / CZK_PER_EUR

    if price <= 0:
        return None

    return round(price, 2)


# =========================================================
# QUERY MATCHING
# =========================================================

def card_matches_query(title, query):
    title_clean = clean_text(title)

    if not title_clean:
        return False

    if contains_merch(title_clean):
        return False

    if is_sealed_title(title_clean):
        return False

    parsed = parse_query(query)

    t = normalize_compare(title_clean)

    if parsed["product_type"] != "Card":
        return False

    if parsed["pokemon"]:
        if normalize_compare(parsed["pokemon"]) not in t:
            return False

    if parsed["suffix"]:
        if normalize_compare(parsed["suffix"]) not in t:
            return False

    if parsed["card_number"]:
        number = parsed["card_number"].replace(" ", "")
        if number not in t.replace(" ", ""):
            return False

    if parsed["set"]:
        if normalize_compare(parsed["set"]) not in t:
            return False

    # Generic word match.
    if not (
        parsed["pokemon"]
        or parsed["suffix"]
        or parsed["card_number"]
        or parsed["set"]
    ):
        query_words = words(query)

        if query_words:
            matches = sum(
                1
                for word in query_words
                if normalize_compare(word) in t
            )

            if matches == 0:
                return False

    return True


def sealed_matches_query(title, query):
    title_clean = clean_text(title)

    if not title_clean:
        return False

    if contains_merch(title_clean):
        return False

    t = normalize_compare(title_clean)
    parsed = parse_query(query)

    for block in SEALED_BLOCK_WORDS:
        if block in t:
            return False

    if parsed["product_type"] == "Card":
        # A set-only search can still mean sealed products.
        if not parsed["set"]:
            return False
    else:
        product = normalize_compare(parsed["product_type"])

        if product not in t:
            return False

    if parsed["set"]:
        if normalize_compare(parsed["set"]) not in t:
            return False

    if parsed["pokemon"]:
        if normalize_compare(parsed["pokemon"]) not in t:
            return False

    return True


def result_matches_query(title, query):
    parsed = parse_query(query)

    if parsed["product_type"] == "Card":
        if is_sealed_title(title):
            # Set-only searches may intentionally return sealed products.
            if parsed["set"]:
                return sealed_matches_query(title, query)

            return False

        return card_matches_query(title, query)

    return sealed_matches_query(title, query)


# =========================================================
# DISCOVERY DATABASE
# =========================================================

def save_discovered(
    shop,
    country,
    title,
    url,
    price_eur,
    condition="Nové",
):
    if not title:
        return

    conn = db()

    conn.execute("""
        INSERT INTO discovered_products
        (
            shop,
            country,
            title,
            url,
            price_eur,
            condition,
            discovered_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        shop,
        country,
        clean_text(title),
        url,
        price_eur,
        condition,
        now_iso(),
    ))

    conn.commit()
    conn.close()


def save_history(query, results):
    conn = db()

    checked = now_iso()

    for item in results:
        if item.get("price_eur") is None:
            continue

        conn.execute("""
            INSERT INTO price_history
            (
                query,
                shop,
                title,
                price_eur,
                checked_at
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            query,
            item.get("shop", ""),
            item.get("title", ""),
            item.get("price_eur"),
            checked,
        ))

    conn.commit()
    conn.close()


# =========================================================
# CARDYX
# =========================================================

def cardyx_discover(query):
    encoded = urllib.parse.quote_plus(query)

    url = (
        "https://www.cardyx.sk/search?q="
        + encoded
    )

    response = get(url)

    if not response:
        return []

    soup = BeautifulSoup(response.text, "html.parser")

    results = []

    for link in soup.select('a[href*="/products/"]'):
        href = link.get("href", "")

        if not href:
            continue

        title = clean_text(link.get_text(" ", strip=True))

        if not title:
            image = link.find("img")

            if image:
                title = clean_text(
                    image.get("alt", "")
                )

        if not title:
            continue

        parent = link

        for _ in range(5):
            if not parent:
                break

            text = clean_text(
                parent.get_text(" ", strip=True)
            )

            price = parse_price(text)

            if price is not None:
                break

            parent = parent.parent
        else:
            price = None

        if price is None:
            price = parse_price(
                link.get("data-price")
            )

        if price is None:
            continue

        product_url = absolute_url(
            "https://www.cardyx.sk/",
            href,
        )

        item = {
            "shop": "CardyX",
            "country": "SK",
            "title": title,
            "url": product_url,
            "price_eur": price,
            "condition": "Nové",
        }

        results.append(item)

        if len(results) >= 30:
            break

    return results


def cardyx_search(query):
    discovered = cardyx_discover(query)

    filtered = []

    for item in discovered:
        if result_matches_query(
            item["title"],
            query
        ):
            save_discovered(
                item["shop"],
                item["country"],
                item["title"],
                item["url"],
                item["price_eur"],
                item["condition"],
            )

            filtered.append(item)

    return filtered


# =========================================================
# SHOP CONFIGURATION
# =========================================================

GENERIC_SHOPS = [
    {
        "name": "Veselý Drak",
        "country": "CZ",
        "url": "https://www.vesely-drak.sk/",
        "search": "https://www.vesely-drak.sk/vyhledavani/?q={query}",
    },
    {
        "name": "iHRYsko",
        "country": "SK",
        "url": "https://www.ihrysko.sk/",
        "search": "https://www.ihrysko.sk/vyhladavanie/?search={query}",
    },
    {
        "name": "Černý Rytíř",
        "country": "CZ",
        "url": "https://www.cernyrytir.cz/",
        "search": "https://www.cernyrytir.cz/?q={query}",
    },
    {
        "name": "Pokébol",
        "country": "SK",
        "url": "https://pokebol.sk/",
        "search": "https://pokebol.sk/search?q={query}",
    },
    {
        "name": "Pokéholik",
        "country": "SK",
        "url": "https://www.pokeholikk.sk/",
        "search": "https://www.pokeholikk.sk/search?q={query}",
    },
    {
        "name": "Beardex",
        "country": "SK",
        "url": "https://www.beardex.eu/",
        "search": "https://www.beardex.eu/?s={query}",
    },
    {
        "name": "Cardmania",
        "country": "SK",
        "url": "https://www.cardmania.sk/",
        "search": "https://www.cardmania.sk/?s={query}",
    },
    {
        "name": "PokecTCG",
        "country": "CZ",
        "url": "https://pokectcg.cz/",
        "search": "https://pokectcg.cz/?s={query}",
    },
    {
        "name": "CardCave",
        "country": "CZ",
        "url": "https://www.cardcave.cz/",
        "search": "https://www.cardcave.cz/?s={query}",
    },
]


# =========================================================
# GENERIC SHOP SCRAPER
# =========================================================

PRODUCT_SELECTORS = [
    "article",
    ".product",
    ".product-item",
    ".product-box",
    ".product-card",
    ".product-item-box",
    "li.product",
    ".item",
    ".product-list-item",
]


def extract_product_from_block(
    block,
    shop,
    country,
    base_url,
):
    link = block.find(
        "a",
        href=True
    )

    if not link:
        return None

    href = link.get("href", "")

    if not href:
        return None

    title = clean_text(
        link.get_text(" ", strip=True)
    )

    if not title:
        image = block.find("img")

        if image:
            title = clean_text(
                image.get("alt", "")
            )

    if not title:
        title_el = block.select_one(
            ".product-title, .title, h2, h3, h4"
        )

        if title_el:
            title = clean_text(
                title_el.get_text(" ", strip=True)
            )

    if not title:
        return None

    block_text = clean_text(
        block.get_text(" ", strip=True)
    )

    price = parse_price(block_text)

    if price is None:
        for selector in [
            ".price",
            ".product-price",
            ".price-final",
            ".current-price",
            "[class*='price']",
        ]:
            el = block.select_one(selector)

            if el:
                price = parse_price(
                    el.get_text(" ", strip=True)
                )

                if price is not None:
                    break

    if price is None:
        return None

    return {
        "shop": shop,
        "country": country,
        "title": title,
        "url": absolute_url(base_url, href),
        "price_eur": price,
        "condition": "Nové",
    }


def generic_shop_search(shop, query):
    encoded = urllib.parse.quote_plus(query)

    search_template = shop.get("search")

    if not search_template:
        return []

    url = search_template.format(
        query=encoded
    )

    response = get(url)

    if not response:
        return []

    soup = BeautifulSoup(
        response.text,
        "html.parser"
    )

    results = []

    # First try product blocks.
    seen_urls = set()

    for selector in PRODUCT_SELECTORS:
        blocks = soup.select(selector)

        for block in blocks:
            item = extract_product_from_block(
                block,
                shop["name"],
                shop["country"],
                shop["url"],
            )

            if not item:
                continue

            if item["url"] in seen_urls:
                continue

            seen_urls.add(item["url"])

            if not result_matches_query(
                item["title"],
                query
            ):
                continue

            save_discovered(
                item["shop"],
                item["country"],
                item["title"],
                item["url"],
                item["price_eur"],
                item["condition"],
            )

            results.append(item)

            if len(results) >= 20:
                return results

        if results:
            return results

    # Fallback: inspect product-like links.
    for link in soup.find_all(
        "a",
        href=True
    ):
        href = link.get("href", "")

        if not href:
            continue

        title = clean_text(
            link.get_text(" ", strip=True)
        )

        if len(title) < 3:
            continue

        if contains_merch(title):
            continue

        parent = link

        for _ in range(4):
            if not parent:
                break

            text = clean_text(
                parent.get_text(" ", strip=True)
            )

            price = parse_price(text)

            if price is not None:
                break

            parent = parent.parent
        else:
            price = None

        if price is None:
            continue

        product_url = absolute_url(
            shop["url"],
            href,
        )

        if product_url in seen_urls:
            continue

        if not result_matches_query(
            title,
            query
        ):
            continue

        seen_urls.add(product_url)

        item = {
            "shop": shop["name"],
            "country": shop["country"],
            "title": title,
            "url": product_url,
            "price_eur": price,
            "condition": "Nové",
        }

        save_discovered(
            item["shop"],
            item["country"],
            item["title"],
            item["url"],
            item["price_eur"],
            item["condition"],
        )

        results.append(item)

        if len(results) >= 20:
            break

    return results


# =========================================================
# SHOP-SPECIFIC SEARCH HELPERS
# =========================================================

def shop_search(shop, query):
    """
    Central dispatcher.

    CardyX has its own scraper.
    Other shops use the common scraper for now.
    This makes it easy to add dedicated parsers later
    without changing search_all().
    """

    try:
        return generic_shop_search(
            shop,
            query
        )
    except Exception:
        return []


# =========================================================
# SEARCH ENGINE
# =========================================================

def deduplicate_results(results):
    unique = {}
    output = []

    for item in results:
        title = normalize_compare(
            item.get("title", "")
        )

        shop = normalize_compare(
            item.get("shop", "")
        )

        price = item.get("price_eur")

        key = (
            shop,
            title,
            price,
        )

        if key in unique:
            continue

        unique[key] = True
        output.append(item)

    return output


def search_all(query):
    query = clean_text(query)

    if not query:
        return []

    results = []

    # -----------------------------------------------------
    # CARDYX
    # -----------------------------------------------------

    try:
        results.extend(
            cardyx_search(query)
        )
    except Exception:
        pass

    # -----------------------------------------------------
    # OTHER SHOPS
    # -----------------------------------------------------

    max_workers = 5

    with ThreadPoolExecutor(
        max_workers=max_workers
    ) as executor:

        futures = {
            executor.submit(
                shop_search,
                shop,
                query,
            ): shop
            for shop in GENERIC_SHOPS
        }

        for future in as_completed(futures):
            try:
                data = future.result()

                if data:
                    results.extend(data)

            except Exception:
                continue

    results = deduplicate_results(
        results
    )

    # Cheapest first.
    results.sort(
        key=lambda x: (
            x.get("price_eur")
            if x.get("price_eur") is not None
            else 999999
        )
    )

    return results


# =========================================================
# SUGGESTIONS
# =========================================================

STATIC_SUGGESTIONS = [
    "Pikachu",
    "Charizard",
    "Mega Charizard X",
    "Umbreon",
    "Eevee",
    "Mew",
    "Mewtwo",
    "Gengar",
    "Lucario",
    "Greninja",
    "Rayquaza",
    "Gardevoir",
    "Dragonite",
    "Gyarados",
    "Blastoise",
    "Venusaur",
    "Lugia",
    "Ho-Oh",
    "Arceus",
    "Dialga",
    "Palkia",
    "Zekrom",
    "Reshiram",
    "Celebi",

    "Pokémon 151",
    "Prismatic Evolutions",
    "Surging Sparks",
    "Destined Rivals",
    "Journey Together",
    "Mega Evolution",
    "Phantasmal Flames",
    "Terastal Festival",
    "Black Bolt",
    "White Flare",

    "Elite Trainer Box",
    "Booster Box",
    "Booster Bundle",
    "Collection Box",
    "Premium Collection",
]


def get_suggestions(query):
    q = normalize_compare(query)

    if not q:
        return STATIC_SUGGESTIONS[:12]

    suggestions = []

    for item in STATIC_SUGGESTIONS:
        if q in normalize_compare(item):
            suggestions.append(item)

    conn = db()

    rows = conn.execute("""
        SELECT DISTINCT title
        FROM discovered_products
        WHERE title IS NOT NULL
        ORDER BY discovered_at DESC
        LIMIT 300
    """).fetchall()

    conn.close()

    for row in rows:
        title = clean_text(
            row["title"]
        )

        if q in normalize_compare(title):
            suggestions.append(title)

    # Unique, preserve order.
    output = []
    seen = set()

    for item in suggestions:
        key = normalize_compare(item)

        if not key:
            continue

        if key in seen:
            continue

        seen.add(key)
        output.append(item)

        if len(output) >= 20:
            break

    return output


# =========================================================
# API - SUGGESTIONS
# =========================================================

@app.route("/api/suggestions")
def api_suggestions():
    query = request.args.get(
        "q",
        "",
        type=str
    )

    return jsonify({
        "query": query,
        "suggestions": get_suggestions(query),
    })


# =========================================================
# API - PARSE
# =========================================================

@app.route("/api/parse")
def api_parse():
    query = request.args.get(
        "q",
        "",
        type=str
    )

    parsed = parse_query(query)

    return jsonify({
        "query": query,
        "normalized": parsed["normalized"],
        "normalized_query": parsed["normalized"],
        "product_type": parsed["product_type"],
        "set": parsed["set"],
        "pokemon": parsed["pokemon"],
        "card_number": parsed["card_number"],
        "suffix": parsed["suffix"],
    })


# =========================================================
# API - SEARCH
# =========================================================

@app.route("/api/search")
def api_search():
    original_query = request.args.get(
        "q",
        "",
        type=str
    )

    original_query = clean_text(
        original_query
    )

    if not original_query:
        return jsonify({
            "query": "",
            "normalized_query": "",
            "results": [],
            "summary": {
                "count": 0,
                "lowest": None,
                "average": None,
            },
        })

    parsed = parse_query(
        original_query
    )

    normalized_query = (
        parsed["normalized"]
        or original_query
    )

    results = search_all(
        normalized_query
    )

    save_history(
        original_query,
        results
    )

    prices = [
        x["price_eur"]
        for x in results
        if x.get("price_eur") is not None
    ]

    lowest = (
        round(min(prices), 2)
        if prices
        else None
    )

    average = (
        round(
            sum(prices) / len(prices),
            2
        )
        if prices
        else None
    )

    return jsonify({
        "query": original_query,
        "normalized_query": normalized_query,
        "normalized": normalized_query,

        "parsed": {
            "product_type": parsed["product_type"],
            "set": parsed["set"],
            "pokemon": parsed["pokemon"],
            "card_number": parsed["card_number"],
            "suffix": parsed["suffix"],
        },

        "results": results,

        "summary": {
            "count": len(results),
            "lowest": lowest,
            "average": average,
        },

        "czk_per_eur": CZK_PER_EUR,

        "checked_at": now_iso(),
    })


# =========================================================
# API - PRODUCTS
# =========================================================

@app.route("/api/products")
def api_products():
    limit = request.args.get(
        "limit",
        100,
        type=int
    )

    limit = max(
        1,
        min(limit, 500)
    )

    conn = db()

    rows = conn.execute("""
        SELECT
            shop,
            country,
            title,
            url,
            price_eur,
            condition,
            discovered_at
        FROM discovered_products
        ORDER BY discovered_at DESC
        LIMIT ?
    """, (
        limit,
    )).fetchall()

    conn.close()

    return jsonify([
        dict(row)
        for row in rows
    ])


# =========================================================
# API - PRICE HISTORY
# =========================================================

@app.route("/api/history")
def api_history():
    query = request.args.get(
        "q",
        "",
        type=str
    )

    conn = db()

    if query:
        rows = conn.execute("""
            SELECT
                query,
                shop,
                title,
                price_eur,
                checked_at
            FROM price_history
            WHERE query = ?
            ORDER BY checked_at DESC
            LIMIT 200
        """, (
            query,
        )).fetchall()
    else:
        rows = conn.execute("""
            SELECT
                query,
                shop,
                title,
                price_eur,
                checked_at
            FROM price_history
            ORDER BY checked_at DESC
            LIMIT 200
        """).fetchall()

    conn.close()

    return jsonify([
        dict(row)
        for row in rows
    ])


# =========================================================
# HEALTH
# =========================================================

@app.route("/health")
def health():
    conn = db()

    try:
        discovered = conn.execute("""
            SELECT COUNT(*) AS count
            FROM discovered_products
        """).fetchone()["count"]

        history = conn.execute("""
            SELECT COUNT(*) AS count
            FROM price_history
        """).fetchone()["count"]

    finally:
        conn.close()

    return jsonify({
        "service": "CardRadar",
        "status": "ok",
        "version": VERSION,
        "base_dir": BASE_DIR,
        "index_exists": find_index()[0],
        "index_path": find_index()[1],
        "discovered_products": discovered,
        "price_history": history,
    })


# =========================================================
# INDEX FINDER
# =========================================================

def find_index():
    possible_paths = [
        os.path.join(
            BASE_DIR,
            "templates",
            "index.html"
        ),
        os.path.join(
            BASE_DIR,
            "Templates",
            "index.html"
        ),
        os.path.join(
            BASE_DIR,
            "index.html"
        ),
    ]

    for path in possible_paths:
        if os.path.isfile(path):
            return True, path

    return False, possible_paths[0]


# =========================================================
# FRONTEND
# =========================================================

@app.route("/")
def index():
    exists, path = find_index()

    if exists:
        try:
            with open(
                path,
                "r",
                encoding="utf-8"
            ) as f:
                html = f.read()

            return Response(
                html,
                mimetype="text/html"
            )

        except Exception as e:
            return Response(
                "<h1>CardRadar</h1>"
                "<p>Chyba pri načítaní index.html:</p>"
                f"<pre>{clean_text(e)}</pre>",
                status=500,
                mimetype="text/html",
            )

    return Response(
        """
        <!doctype html>
        <html lang="sk">
        <head>
            <meta charset="utf-8">
            <title>CardRadar</title>
        </head>
        <body>
            <h1>CardRadar</h1>
            <p>
                index.html sa nenašiel.
            </p>
        </body>
        </html>
        """,
        status=404,
        mimetype="text/html",
    )


# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    port = int(
        os.environ.get(
            "PORT",
            5000
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
