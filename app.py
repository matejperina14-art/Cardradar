import os
import re
import sqlite3
import urllib.parse
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, Response


# =========================================================
# CARD RADAR
# VERSION 5.8
# =========================================================

VERSION = "5.8"

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CZK_PER_EUR = 24.4618

DB_PATH = os.path.join(BASE_DIR, "cardradar.db")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 "
        "(KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,image/avif,image/webp,"
        "*/*;q=0.8"
    ),
    "Accept-Language": "cs-CZ,sk-SK;q=0.9,en;q=0.8",
}


# =========================================================
# DATABASE
# =========================================================

def init_db():

    conn = sqlite3.connect(DB_PATH)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT,
            shop TEXT,
            title TEXT,
            price_eur REAL,
            link TEXT,
            checked_at TEXT
        )
    """)

    conn.commit()
    conn.close()


init_db()


# =========================================================
# INDEX FINDER
# =========================================================

def find_index():

    candidates = [
        os.path.join(BASE_DIR, "templates", "index.html"),
        os.path.join(BASE_DIR, "Templates", "index.html"),
        os.path.join(BASE_DIR, "index.html"),
    ]

    for path in candidates:

        if os.path.isfile(path):
            return path

    return None


# =========================================================
# TEXT HELPERS
# =========================================================

def clean_text(value):

    value = str(value or "")

    value = value.replace("\xa0", " ")

    value = re.sub(r"\s+", " ", value)

    return value.strip()


def words(text):

    text = clean_text(text).lower()

    return set(
        re.findall(
            r"[a-z0-9]+",
            text
        )
    )


def normalize_spaces(text):

    return re.sub(
        r"\s+",
        " ",
        clean_text(text)
    ).strip()


# =========================================================
# SEARCH ALIASES
# =========================================================

SET_ALIASES = {

    "sv8": "surging sparks",
    "sv8a": "terastal festival",
    "sv9": "journey together",
    "sv9a": "destined rivals",
    "sv10": "destined rivals",
    "sv10.5": "destined rivals",
    "sv11": "black bolt white flare",
    "sv6": "twilight masquerade",
    "sv7": "stellar crown",
    "sv5": "temporal forces",
    "sv4": "paradox rift",
    "sv3": "obsidian flames",
    "sv2": "paldea evolved",
    "sv1": "scarlet violet base",

    "151": "pokemon 151",
    "pokemon151": "pokemon 151",
    "pokemon 151": "pokemon 151",

    "prismatic": "prismatic evolutions",
    "prismatic evo": "prismatic evolutions",

    "surging": "surging sparks",
    "sparks": "surging sparks",

    "destined": "destined rivals",
    "journey": "journey together",

    "terastal": "terastal festival",
    "terastal festival": "terastal festival",

    "phantasmal": "phantasmal flames",
    "phantasmal flames": "phantasmal flames",

    "mega brave": "mega evolution mega brave",
    "mega evolution": "mega evolution",

}


PRODUCT_ALIASES = {

    "etb": "elite trainer box",
    "elite trainer": "elite trainer box",
    "elite trainer box": "elite trainer box",

    "booster box": "booster box",
    "boosterbox": "booster box",
    "bb": "booster box",

    "booster bundle": "booster bundle",
    "bundle": "booster bundle",

    "collection box": "collection box",
    "collection": "collection box",

    "premium collection": "premium collection",
    "premium box": "premium collection",

    "tin": "tin",
    "tins": "tin",

    "blister": "blister",
    "blister pack": "blister",

    "box": "box",

}


POKEMON_ALIASES = {

    "pikachu": "Pikachu",
    "pika": "Pikachu",

    "charizard": "Charizard",
    "char": "Charizard",

    "umbreon": "Umbreon",
    "eevee": "Eevee",

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
    "ho-oh": "Ho-Oh",
    "hooh": "Ho-Oh",

    "arceus": "Arceus",
    "dialga": "Dialga",
    "palkia": "Palkia",

    "zekrom": "Zekrom",
    "reshiram": "Reshiram",

    "celebi": "Celebi",
    "jolteon": "Jolteon",
    "vaporeon": "Vaporeon",
    "flareon": "Flareon",

    "espeon": "Espeon",
    "sylveon": "Sylveon",
    "leafeon": "Leafeon",
    "glaceon": "Glaceon",

}


# =========================================================
# AUTOCOMPLETE CATALOG
# =========================================================

SUGGESTION_CATALOG = []


# =========================================================
# NORMALIZATION
# =========================================================

def normalize_query(query):

    original = normalize_spaces(query)

    if not original:
        return {
            "original": "",
            "normalized": "",
            "pokemon": "",
            "set_name": "",
            "product_name": "",
            "product_type": "",
            "card_number": "",
        }

    q = original.lower()

    number_match = re.search(
        r"\b(\d{1,4})\s*/\s*(\d{1,4})\b",
        q
    )

    card_number = ""

    if number_match:

        card_number = (
            f"{number_match.group(1)}/"
            f"{number_match.group(2)}"
        )

    product_type = ""

    product_patterns = [
        ("elite trainer box", r"\belite\s+trainer\s+box\b"),
        ("elite trainer box", r"\betb\b"),
        ("booster box", r"\bbooster\s*box\b"),
        ("booster bundle", r"\bbooster\s*bundle\b"),
        ("collection box", r"\bcollection\s+box\b"),
        ("premium collection", r"\bpremium\s+collection\b"),
        ("blister", r"\bblister(?:\s+pack)?\b"),
        ("tin", r"\btins?\b"),
    ]

    for canonical, pattern in product_patterns:

        if re.search(pattern, q):

            product_type = canonical

            q = re.sub(
                pattern,
                " ",
                q
            )

            break

    set_name = ""

    for alias, canonical in sorted(
        SET_ALIASES.items(),
        key=lambda x: len(x[0]),
        reverse=True
    ):

        pattern = (
            r"\b" +
            re.escape(alias.lower()) +
            r"\b"
        )

        if re.search(pattern, q):

            set_name = canonical

            q = re.sub(
                pattern,
                " ",
                q
            )

            break

    if not set_name:

        known_sets = sorted(
            set(
                list(SET_ALIASES.values()) +
                [
                    "surging sparks",
                    "pokemon 151",
                    "prismatic evolutions",
                    "terastal festival",
                    "destined rivals",
                    "journey together",
                    "twilight masquerade",
                    "stellar crown",
                    "temporal forces",
                    "obsidian flames",
                    "mega evolution",
                    "phantasmal flames",
                ]
            ),
            key=len,
            reverse=True
        )

        for candidate in known_sets:

            pattern = (
                r"\b" +
                re.escape(candidate.lower()) +
                r"\b"
            )

            if re.search(pattern, q):

                set_name = candidate

                q = re.sub(
                    pattern,
                    " ",
                    q
                )

                break

    pokemon = ""

    for alias, canonical in sorted(
        POKEMON_ALIASES.items(),
        key=lambda x: len(x[0]),
        reverse=True
    ):

        pattern = (
            r"\b" +
            re.escape(alias.lower()) +
            r"\b"
        )

        if re.search(pattern, q):

            pokemon = canonical

            q = re.sub(
                pattern,
                " ",
                q
            )

            break

    suffix = ""

    suffix_match = re.search(
        r"\b(vmax|vstar|ex|gx|v)\b",
        q,
        re.IGNORECASE
    )

    if suffix_match:

        suffix = suffix_match.group(1)

        q = re.sub(
            r"\b(vmax|vstar|ex|gx|v)\b",
            " ",
            q,
            flags=re.IGNORECASE
        )

    q = re.sub(
        r"\bpokemon\b",
        " ",
        q,
        flags=re.IGNORECASE
    )

    q = normalize_spaces(q)

    parts = []

    if pokemon:
        parts.append(pokemon)

    if suffix:
        parts.append(suffix.lower())

    if q:
        parts.append(q)

    if card_number:
        parts.append(card_number)

    if set_name:
        parts.append(set_name)

    if product_type:
        parts.append(product_type)

    normalized = normalize_spaces(
        " ".join(parts)
    )

    if product_type == "elite trainer box":

        normalized_parts = []

        if set_name:
            normalized_parts.append(set_name)

        if pokemon:
            normalized_parts.append(pokemon)

        if suffix:
            normalized_parts.append(
                suffix.lower()
            )

        if card_number:
            normalized_parts.append(
                card_number
            )

        normalized_parts.append(
            "elite trainer box"
        )

        normalized = normalize_spaces(
            " ".join(normalized_parts)
        )

    return {
        "original": original,
        "normalized": normalized or original,
        "pokemon": pokemon,
        "set_name": set_name,
        "product_name": product_type,
        "product_type": product_type,
        "card_number": card_number,
    }


# =========================================================
# CLASSIFY
# =========================================================

def classify_query(parsed):

    if parsed.get("product_type"):
        return "sealed"

    if (
        parsed.get("set_name")
        and not parsed.get("pokemon")
    ):
        return "sealed"

    return "card"


# =========================================================
# CARD NUMBER HELPERS
# =========================================================

def normalize_card_number(value):

    value = clean_text(value)

    match = re.search(
        r"\b(\d{1,4})\s*/\s*(\d{1,4})\b",
        value
    )

    if not match:
        return ""

    return (
        f"{match.group(1)}/"
        f"{match.group(2)}"
    )


def card_number_from_query(query):

    return normalize_card_number(query)


# =========================================================
# ADVANCED CARD MATCHING
# =========================================================

def card_matches_query(
    title,
    query,
    extra_text=""
):

    title_clean = clean_text(
        title
    ).lower()

    extra_clean = clean_text(
        extra_text
    ).lower()

    searchable = (
        title_clean +
        " " +
        extra_clean
    )

    parsed = normalize_query(
        query
    )

    pokemon = clean_text(
        parsed.get(
            "pokemon",
            ""
        )
    ).lower()

    set_name = clean_text(
        parsed.get(
            "set_name",
            ""
        )
    ).lower()

    card_number = clean_text(
        parsed.get(
            "card_number",
            ""
        )
    ).lower()

    original_lower = clean_text(
        query
    ).lower()

    # -----------------------------------------------------
    # POKEMON
    # -----------------------------------------------------

    if pokemon:

        pokemon_found = (
            re.search(
                r"\b" +
                re.escape(pokemon) +
                r"\b",
                searchable,
                re.IGNORECASE
            )
            is not None
        )

        if not pokemon_found:
            return False

    # -----------------------------------------------------
    # CARD NUMBER
    # -----------------------------------------------------

    if card_number:

        normalized_searchable = re.sub(
            r"\s+",
            "",
            searchable
        )

        normalized_card_number = (
            card_number
            .replace(" ", "")
        )

        if normalized_card_number not in (
            normalized_searchable
        ):

            return False

    # -----------------------------------------------------
    # SET
    # -----------------------------------------------------

    if set_name:

        # Exact set words are preferred,
        # but allow common abbreviations.

        set_words = words(
            set_name
        )

        searchable_words = words(
            searchable
        )

        abbreviation = None

        for alias, canonical in SET_ALIASES.items():

            if canonical.lower() == set_name:

                abbreviation = alias.lower()

                break

        set_found = (
            set_words.issubset(
                searchable_words
            )
            or (
                abbreviation
                and abbreviation in searchable
            )
        )

        if not set_found:
            return False

    # -----------------------------------------------------
    # CARD VARIANTS
    # -----------------------------------------------------

    suffix_match = re.search(
        r"\b(vmax|vstar|ex|gx|v)\b",
        original_lower
    )

    if suffix_match:

        suffix = suffix_match.group(1)

        if not re.search(
            r"\b" +
            re.escape(suffix) +
            r"\b",
            searchable,
            re.IGNORECASE
        ):
            return False

    return True


# =========================================================
# SEALED MATCH
# =========================================================

def sealed_matches_query(
    title,
    query,
    extra_text=""
):

    title_clean = clean_text(
        title
    ).lower()

    extra_clean = clean_text(
        extra_text
    ).lower()

    searchable = (
        title_clean +
        " " +
        extra_clean
    )

    parsed = normalize_query(
        query
    )

    set_name = parsed.get(
        "set_name",
        ""
    ).lower()

    product_type = parsed.get(
        "product_type",
        ""
    ).lower()

    if set_name:

        set_words = words(
            set_name
        )

        searchable_words = words(
            searchable
        )

        abbreviation = None

        for alias, canonical in SET_ALIASES.items():

            if canonical.lower() == set_name:

                abbreviation = alias.lower()

                break

        if not (
            set_words.issubset(
                searchable_words
            )
            or (
                abbreviation
                and abbreviation in searchable
            )
        ):
            return False

    if product_type:

        if product_type == "elite trainer box":

            if not (
                "elite trainer box"
                in searchable
                or re.search(
                    r"\betb\b",
                    searchable
                )
            ):
                return False

        elif product_type not in searchable:

            return False

    if (
        product_type == "elite trainer box"
        and re.search(
            r"\b(case|10x|12x|6x)\b",
            searchable
        )
    ):
        return False

    return True


# =========================================================
# PRICE PARSER
# =========================================================

def parse_price(text):

    if not text:
        return None

    text = clean_text(text)

    eur_patterns = [
        r"(\d{1,6}(?:[.,]\d{1,2})?)\s*€",
        r"€\s*(\d{1,6}(?:[.,]\d{1,2})?)",
    ]

    for pattern in eur_patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if match:

            value = match.group(1)

            value = value.replace(
                " ",
                ""
            )

            if (
                "," in value
                and "." in value
            ):

                if (
                    value.rfind(",")
                    >
                    value.rfind(".")
                ):

                    value = value.replace(
                        ".",
                        ""
                    )

                    value = value.replace(
                        ",",
                        "."
                    )

                else:

                    value = value.replace(
                        ",",
                        ""
                    )

            else:

                value = value.replace(
                    ",",
                    "."
                )

            try:
                return float(value)

            except:
                pass

    czk_patterns = [
        r"(\d{1,8}(?:[.,]\d{1,2})?)\s*(?:Kč|CZK)",
        r"(?:Kč|CZK)\s*(\d{1,8}(?:[.,]\d{1,2})?)",
    ]

    for pattern in czk_patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if match:

            value = match.group(1)

            value = value.replace(
                ",",
                "."
            )

            try:

                czk = float(value)

                return (
                    czk /
                    CZK_PER_EUR
                )

            except:
                pass

    return None


# =========================================================
# MERCH BLACKLIST
# =========================================================

MERCH_BLACKLIST = [

    "plush",
    "plyš",
    "peluche",

    "figúrka",
    "figurka",
    "figure",
    "figurine",

    "hrnček",
    "hrnek",
    "mug",

    "tričko",
    "tricko",
    "shirt",

    "mikina",
    "hoodie",

    "ponožky",
    "ponozky",
    "socks",

    "puzzle",

    "podložka",
    "podlozka",
    "playmat",

    "album",
    "binder",

    "obal",
    "sleeves",
    "sleeve",

    "keychain",
    "kľúčenka",
    "klucenka",

    "batoh",
    "backpack",

    "taška",
    "taska",

    "poster",
    "plagát",
    "plagat",

    "sticker",
    "nálepka",
    "nalepka",
]


def is_merch(title):

    t = clean_text(
        title
    ).lower()

    return any(
        word in t
        for word in MERCH_BLACKLIST
    )


# =========================================================
# HTTP
# =========================================================

def fetch(url, timeout=20):

    start = time.monotonic()

    debug = {
        "url": url,
        "http_status": None,
        "elapsed_ms": 0,
        "status": "unknown",
        "error": "",
    }

    try:

        response = requests.get(
            url,
            headers=HEADERS,
            timeout=timeout,
            allow_redirects=True
        )

        debug["http_status"] = (
            response.status_code
        )

        debug["elapsed_ms"] = round(
            (
                time.monotonic()
                - start
            ) * 1000
        )

        if response.status_code != 200:

            debug["status"] = "http_error"

            debug["error"] = (
                f"HTTP {response.status_code}"
            )

            return None, debug

        debug["status"] = "http_ok"

        return response, debug

    except requests.Timeout:

        debug["elapsed_ms"] = round(
            (
                time.monotonic()
                - start
            ) * 1000
        )

        debug["status"] = "timeout"

        debug["error"] = (
            "Request timeout"
        )

        return None, debug

    except Exception as e:

        debug["elapsed_ms"] = round(
            (
                time.monotonic()
                - start
            ) * 1000
        )

        debug["status"] = "request_error"

        debug["error"] = str(e)

        return None, debug


def get(url, timeout=20):

    response, _ = fetch(
        url,
        timeout
    )

    return response


# =========================================================
# URL HELPERS
# =========================================================

def absolute_url(
    base_url,
    href
):

    if not href:
        return ""

    href = href.strip()

    if href.startswith(
        "javascript:"
    ):
        return ""

    if href.startswith("#"):
        return ""

    return urllib.parse.urljoin(
        base_url,
        href
    )


# =========================================================
# CARDYX
# =========================================================

def cardyx_search(
    query,
    return_debug=False
):

    results = []

    debug = {
        "shop": "CardyX",
        "query": query,
        "url": "",
        "status": "starting",
        "http_status": None,
        "results": 0,
        "links_scanned": 0,
        "elapsed_ms": 0,
        "error": "",
    }

    start = time.monotonic()

    try:

        url = (
            "https://www.cardyx.sk/search"
            "?q=" +
            urllib.parse.quote(query)
        )

        debug["url"] = url

        response, http_debug = fetch(
            url
        )

        debug["http_status"] = (
            http_debug["http_status"]
        )

        if not response:

            debug.update({
                "status": http_debug["status"],
                "error": http_debug["error"],
                "elapsed_ms": http_debug["elapsed_ms"],
            })

            if return_debug:
                return results, debug

            return results

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        links = soup.select(
            'a[href*="/products/"]'
        )

        debug["links_scanned"] = len(
            links
        )

        seen = set()

        for a in links:

            href = a.get("href")

            if not href:
                continue

            href = absolute_url(
                "https://www.cardyx.sk/",
                href
            )

            if not href:
                continue

            if href in seen:
                continue

            seen.add(href)

            title = clean_text(
                a.get_text(
                    " ",
                    strip=True
                )
            )

            if not title:
                continue

            parent = a

            for _ in range(5):

                if parent.parent:
                    parent = parent.parent

            block_text = clean_text(
                parent.get_text(
                    " ",
                    strip=True
                )
            )

            price = parse_price(
                block_text
            )

            if price is None:

                if a.parent:

                    price = parse_price(
                        a.parent.get_text(
                            " ",
                            strip=True
                        )
                    )

            if price is None:
                continue

            if is_merch(title):
                continue

            parsed = normalize_query(
                query
            )

            kind = classify_query(
                parsed
            )

            if kind == "card":

                if not card_matches_query(
                    title,
                    query,
                    block_text
                ):
                    continue

            else:

                if not sealed_matches_query(
                    title,
                    query,
                    block_text
                ):
                    continue

            results.append({
                "title": title,
                "shop": "CardyX",
                "country": "SK",
                "condition": "Nové",
                "price_eur": round(
                    price,
                    2
                ),
                "link": href,
            })

        unique = {}

        for item in results:

            key = (
                item["title"].lower(),
                item["price_eur"]
            )

            unique[key] = item

        results = list(
            unique.values()
        )

        debug["results"] = len(
            results
        )

        debug["status"] = (
            "ok"
            if results
            else "no_results"
        )

    except Exception as e:

        debug["status"] = (
            "parser_error"
        )

        debug["error"] = str(e)

    debug["elapsed_ms"] = round(
        (
            time.monotonic()
            - start
        ) * 1000
    )

    if return_debug:
        return results, debug

    return results


# =========================================================
# SHOP DEFINITIONS
# =========================================================

GENERIC_SHOPS = [

    {
        "name": "Veselý Drak",
        "country": "CZ",
        "url": "https://www.vesely-drak.cz/",
    },

]


# =========================================================
# VESelý DRAK
# =========================================================

def vesely_drak_search(
    query,
    return_debug=False
):

    results = []

    debug = {
        "shop": "Veselý Drak",
        "query": query,
        "url": "",
        "status": "starting",
        "http_status": None,
        "results": 0,
        "links_scanned": 0,
        "candidates": 0,
        "merch_filtered": 0,
        "match_filtered": 0,
        "products_with_price": 0,
        "elapsed_ms": 0,
        "error": "",
    }

    start = time.monotonic()

    try:

        # -------------------------------------------------
        # 1. STORE SEARCH
        # -------------------------------------------------

        url = (
            "https://www.vesely-drak.cz/"
            "?s=" +
            urllib.parse.quote(query)
        )

        debug["url"] = url

        response, http_debug = fetch(
            url
        )

        debug["http_status"] = (
            http_debug["http_status"]
        )

        if not response:

            debug.update({
                "status": http_debug["status"],
                "error": http_debug["error"],
                "elapsed_ms": http_debug["elapsed_ms"],
            })

            if return_debug:
                return results, debug

            return results

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        # -------------------------------------------------
        # 2. FIND PRODUCT LINKS
        #
        # We deliberately do NOT require a specific
        # CSS class here. The site can change classes.
        # -------------------------------------------------

        all_links = soup.find_all(
            "a",
            href=True
        )

        debug["links_scanned"] = len(
            all_links
        )

        seen = set()

        parsed_query = normalize_query(
            query
        )

        kind = classify_query(
            parsed_query
        )

        # -------------------------------------------------
        # 3. INSPECT LINKS
        # -------------------------------------------------

        for a in all_links:

            href = a.get(
                "href"
            )

            if not href:
                continue

            href = absolute_url(
                url,
                href
            )

            if not href:
                continue

            # We only want product-like links.
            # Skip navigation/category/search/cart/etc.
            href_lower = href.lower()

            blocked_url_parts = [
                "/kosik",
                "/cart",
                "/checkout",
                "/login",
                "/registrace",
                "/registracia",
                "/kontakt",
                "/blog",
                "/novinky",
                "/category",
                "/kategorie",
                "?s=",
                "&s=",
            ]

            if any(
                part in href_lower
                for part in blocked_url_parts
            ):
                continue

            # -------------------------------------------------
            # TITLE
            # -------------------------------------------------

            title = clean_text(
                a.get_text(
                    " ",
                    strip=True
                )
            )

            if not title:
                continue

            if len(title) < 3:
                continue

            # Avoid obvious menu/navigation text.
            if len(title) > 250:
                continue

            # -------------------------------------------------
            # PRODUCT BLOCK
            # -------------------------------------------------

            parent = a

            for _ in range(5):

                if not parent.parent:
                    break

                parent = parent.parent

                parent_text = clean_text(
                    parent.get_text(
                        " ",
                        strip=True
                    )
                )

                # A reasonable product card normally
                # contains a price or product text.
                if (
                    "Kč" in parent_text
                    or "CZK" in parent_text
                    or "€" in parent_text
                ):
                    break

            block_text = clean_text(
                parent.get_text(
                    " ",
                    strip=True
                )
            )

            # -------------------------------------------------
            # PRICE
            # -------------------------------------------------

            price = parse_price(
                block_text
            )

            if price is None:

                price = parse_price(
                    title
                )

            if price is None:
                continue

            debug[
                "products_with_price"
            ] += 1

            # -------------------------------------------------
            # DUPLICATES
            # -------------------------------------------------

            unique_key = (
                href.lower(),
                title.lower()
            )

            if unique_key in seen:
                continue

            seen.add(
                unique_key
            )

            # -------------------------------------------------
            # CANDIDATE
            # -------------------------------------------------

            debug[
                "candidates"
            ] += 1

            # -------------------------------------------------
            # MERCH FILTER
            # -------------------------------------------------

            if is_merch(
                title
            ):

                debug[
                    "merch_filtered"
                ] += 1

                continue

            # -------------------------------------------------
            # MATCHING
            #
            # IMPORTANT:
            # Search is performed over BOTH title and
            # the surrounding product block.
            #
            # This allows cases where the product title
            # is weak but the surrounding product data
            # contains Pikachu / set / card number.
            # -------------------------------------------------

            if kind == "card":

                matched = card_matches_query(
                    title,
                    query,
                    block_text
                )

            else:

                matched = sealed_matches_query(
                    title,
                    query,
                    block_text
                )

            if not matched:

                debug[
                    "match_filtered"
                ] += 1

                continue

            # -------------------------------------------------
            # RESULT
            # -------------------------------------------------

            results.append({
                "title": title,
                "shop": "Veselý Drak",
                "country": "CZ",
                "condition": "Nové",
                "price_eur": round(
                    price,
                    2
                ),
                "link": href,
            })

            # Safety limit.
            if len(results) >= 30:
                break

        # -------------------------------------------------
        # DEDUPLICATION
        # -------------------------------------------------

        unique = {}

        for item in results:

            key = (
                item["title"].lower(),
                item["price_eur"],
                item["link"].lower()
            )

            unique[key] = item

        results = list(
            unique.values()
        )

        debug["results"] = len(
            results
        )

        debug["status"] = (
            "ok"
            if results
            else "no_results"
        )

    except Exception as e:

        debug["status"] = (
            "parser_error"
        )

        debug["error"] = str(e)

    debug["elapsed_ms"] = round(
        (
            time.monotonic()
            - start
        ) * 1000
    )

    if return_debug:
        return results, debug

    return results


# =========================================================
# GENERIC SHOPS
# =========================================================

def generic_shop_search(
    shop,
    query,
    return_debug=False
):

    if shop["name"] == "Veselý Drak":

        return vesely_drak_search(
            query,
            return_debug
        )

    results = []

    debug = {
        "shop": shop["name"],
        "query": query,
        "url": "",
        "status": "not_implemented",
        "http_status": None,
        "results": 0,
        "links_scanned": 0,
        "elapsed_ms": 0,
        "error": "",
    }

    if return_debug:
        return results, debug

    return results


# =========================================================
# SHOP RUNNER
# =========================================================

def run_shop(
    name,
    query
):

    start = time.monotonic()

    try:

        if name == "CardyX":

            results, debug = cardyx_search(
                query,
                return_debug=True
            )

        elif name == "Veselý Drak":

            results, debug = vesely_drak_search(
                query,
                return_debug=True
            )

        else:

            results = []

            debug = {
                "shop": name,
                "query": query,
                "status": "not_implemented",
                "results": 0,
                "error": "",
            }

        debug["elapsed_ms"] = round(
            (
                time.monotonic()
                - start
            ) * 1000
        )

        return results, debug

    except Exception as e:

        return [], {
            "shop": name,
            "query": query,
            "status": "runner_error",
            "results": 0,
            "error": str(e),
            "elapsed_ms": round(
                (
                    time.monotonic()
                    - start
                ) * 1000
            ),
        }


# =========================================================
# SEARCH ALL
# =========================================================

def search_all(
    query,
    return_debug=False
):

    results = []

    diagnostics = []

    # -----------------------------------------------------
    # CURRENT BATCH:
    #
    # CardyX = known working baseline
    # Veselý Drak = current development target
    #
    # NO OTHER SHOPS YET.
    # -----------------------------------------------------

    shops = [
        "CardyX",
        "Veselý Drak",
    ]

    with ThreadPoolExecutor(
        max_workers=2
    ) as executor:

        futures = {
            executor.submit(
                run_shop,
                shop,
                query
            ): shop
            for shop in shops
        }

        for future in as_completed(
            futures
        ):

            try:

                shop_results, debug = (
                    future.result()
                )

                results.extend(
                    shop_results
                )

                diagnostics.append(
                    debug
                )

            except Exception as e:

                shop_name = futures[
                    future
                ]

                diagnostics.append({
                    "shop": shop_name,
                    "query": query,
                    "status": "future_error",
                    "results": 0,
                    "error": str(e),
                })

    # -----------------------------------------------------
    # SORT DEBUG BY SHOP NAME
    # -----------------------------------------------------

    diagnostics.sort(
        key=lambda x:
        x.get(
            "shop",
            ""
        )
    )

    # -----------------------------------------------------
    # REMOVE DUPLICATES
    # -----------------------------------------------------

    unique = {}

    for item in results:

        key = (
            clean_text(
                item.get(
                    "shop",
                    ""
                )
            ).lower(),

            clean_text(
                item.get(
                    "title",
                    ""
                )
            ).lower(),

            round(
                float(
                    item.get(
                        "price_eur",
                        0
                    )
                ),
                2
            )
        )

        unique[key] = item

    results = list(
        unique.values()
    )

    # -----------------------------------------------------
    # SORT BY PRICE
    # -----------------------------------------------------

    results.sort(
        key=lambda x:
        float(
            x.get(
                "price_eur",
                999999
            )
        )
    )

    if return_debug:

        return results, {
            "query": query,
            "shops": diagnostics,
            "total_results": len(
                results
            ),
        }

    return results


# =========================================================
# SAVE HISTORY
# =========================================================

def save_history(
    query,
    results
):

    if not results:
        return

    conn = sqlite3.connect(
        DB_PATH
    )

    now = datetime.utcnow().isoformat()

    for item in results:

        conn.execute(
            """
            INSERT INTO price_history
            (
                query,
                shop,
                title,
                price_eur,
                link,
                checked_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                query,
                item.get(
                    "shop",
                    ""
                ),
                item.get(
                    "title",
                    ""
                ),
                item.get(
                    "price_eur",
                    0
                ),
                item.get(
                    "link",
                    ""
                ),
                now,
            )
        )

    conn.commit()

    conn.close()


# =========================================================
# AUTOCOMPLETE SCORING
# =========================================================

def suggestion_score(
    item,
    query
):

    q = clean_text(
        query
    ).lower()

    title = clean_text(
        item.get(
            "title",
            ""
        )
    ).lower()

    subtitle = clean_text(
        item.get(
            "subtitle",
            ""
        )
    ).lower()

    score = 0

    if title.startswith(q):
        score += 100

    if any(
        word.startswith(q)
        for word in title.split()
    ):
        score += 60

    if q in title:
        score += 40

    if q in subtitle:
        score += 10

    score += max(
        0,
        20 -
        len(title) // 10
    )

    return score


# =========================================================
# API SUGGESTIONS
# =========================================================

@app.get("/api/suggestions")
def api_suggestions():

    q = clean_text(
        request.args.get(
            "q",
            ""
        )
    )

    if len(q) < 2:

        return jsonify({
            "query": q,
            "suggestions": []
        })

    q_lower = q.lower()

    candidates = []

    for item in SUGGESTION_CATALOG:

        title = clean_text(
            item.get(
                "title",
                ""
            )
        ).lower()

        subtitle = clean_text(
            item.get(
                "subtitle",
                ""
            )
        ).lower()

        if (
            q_lower in title
            or q_lower in subtitle
            or title.startswith(
                q_lower
            )
        ):

            candidates.append(
                item.copy()
            )

    parsed = normalize_query(
        q
    )

    normalized = parsed.get(
        "normalized",
        ""
    )

    if normalized:

        normalized_lower = (
            normalized.lower()
        )

        exists = any(
            clean_text(
                item.get(
                    "query",
                    ""
                )
            ).lower()
            == normalized_lower
            for item in candidates
        )

        if (
            not exists
            and normalized_lower != q_lower
        ):

            if parsed.get(
                "product_type"
            ):

                candidates.append({
                    "title": normalized,
                    "query": normalized,
                    "subtitle": (
                        "Automaticky "
                        "rozpoznaný produkt"
                    ),
                    "type": "product",
                    "type_label": "Produkt",
                })

            elif parsed.get(
                "pokemon"
            ):

                candidates.append({
                    "title": normalized,
                    "query": normalized,
                    "subtitle": (
                        "Automaticky "
                        "rozpoznaná karta"
                    ),
                    "type": "card",
                    "type_label": "Karta",
                })

    candidates.sort(
        key=lambda item:
        suggestion_score(
            item,
            q
        ),
        reverse=True
    )

    output = []

    seen = set()

    for item in candidates:

        key = clean_text(
            item.get(
                "query",
                item.get(
                    "title",
                    ""
                )
            )
        ).lower()

        if not key:
            continue

        if key in seen:
            continue

        seen.add(key)

        output.append(item)

        if len(output) >= 8:
            break

    return jsonify({
        "query": q,
        "normalized_query": normalized,
        "suggestions": output
    })


# =========================================================
# API PARSE
# =========================================================

@app.get("/api/parse")
def api_parse():

    q = request.args.get(
        "q",
        ""
    )

    parsed = normalize_query(
        q
    )

    parsed["type"] = classify_query(
        parsed
    )

    return jsonify(parsed)


# =========================================================
# API SEARCH
# =========================================================

@app.get("/api/search")
def api_search():

    original_query = clean_text(
        request.args.get(
            "q",
            ""
        )
    )

    if not original_query:

        return jsonify({
            "error":
            "Chýba vyhľadávanie."
        }), 400

    parsed = normalize_query(
        original_query
    )

    normalized_query = parsed.get(
        "normalized",
        original_query
    )

    results, debug = search_all(
        normalized_query,
        return_debug=True
    )

    save_history(
        original_query,
        results
    )

    info = {
        "title": normalized_query,
        "subtitle": "",
        "image": "",
    }

    if parsed.get(
        "set_name"
    ):

        info["subtitle"] = (
            "Set: " +
            parsed["set_name"]
        )

    elif parsed.get(
        "pokemon"
    ):

        info["subtitle"] = (
            "Pokémon: " +
            parsed["pokemon"]
        )

    return jsonify({
        "query": original_query,
        "normalized_query": normalized_query,
        "parsed": parsed,
        "results": results,
        "czk_per_eur": CZK_PER_EUR,
        "info": info,
        "debug": debug,
    })


# =========================================================
# API DEBUG SEARCH
# =========================================================

@app.get("/api/debug/search")
def api_debug_search():

    original_query = clean_text(
        request.args.get(
            "q",
            ""
        )
    )

    if not original_query:

        return jsonify({
            "error":
            "Chýba vyhľadávanie."
        }), 400

    parsed = normalize_query(
        original_query
    )

    normalized_query = parsed.get(
        "normalized",
        original_query
    )

    results, debug = search_all(
        normalized_query,
        return_debug=True
    )

    return jsonify({
        "query": original_query,
        "normalized_query": normalized_query,
        "parsed": parsed,
        "debug": debug,
        "results": results,
    })


# =========================================================
# VESelý DRAK DEBUG
# =========================================================

@app.get("/api/debug/vesely")
def api_debug_vesely():

    original_query = clean_text(
        request.args.get(
            "q",
            ""
        )
    )

    if not original_query:

        return jsonify({
            "error":
            "Chýba vyhľadávanie."
        }), 400

    parsed = normalize_query(
        original_query
    )

    normalized_query = parsed.get(
        "normalized",
        original_query
    )

    results, debug = (
        vesely_drak_search(
            normalized_query,
            return_debug=True
        )
    )

    return jsonify({
        "query": original_query,
        "normalized_query": normalized_query,
        "parsed": parsed,
        "debug": debug,
        "results": results,
    })


# =========================================================
# HEALTH
# =========================================================

@app.get("/health")
def health():

    index_path = find_index()

    return jsonify({
        "service": "CardRadar",
        "status": "ok",
        "version": VERSION,
        "base_dir": BASE_DIR,
        "index_exists": bool(
            index_path
        ),
        "index_path": index_path,
    })


# =========================================================
# HOME
# =========================================================

@app.get("/")
def home():

    index_path = find_index()

    if not index_path:

        return Response(
            """
            <h1>CardRadar</h1>
            <p>
                index.html nebol nájdený.
            </p>
            """,
            status=500,
            mimetype="text/html"
        )

    try:

        with open(
            index_path,
            "r",
            encoding="utf-8"
        ) as f:

            return Response(
                f.read(),
                mimetype="text/html"
            )

    except Exception as e:

        return Response(
            (
                "<h1>CardRadar</h1>"
                "<p>"
                "Chyba pri načítaní stránky."
                "</p>"
                f"<pre>{e}</pre>"
            ),
            status=500,
            mimetype="text/html"
        )


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":

    port = int(
        os.environ.get(
            "PORT",
            "10000"
        )
    )

    app.run(
        host="0.0.0.0",
        port=port
    )
