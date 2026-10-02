import os
import re
import sqlite3
import urllib.parse
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, Response


# =========================================================
# CARD RADAR
# Version 5.7 + Veselý Drak
# =========================================================

VERSION = "5.7"

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CZK_PER_EUR = 24.4618

DB_PATH = os.path.join(BASE_DIR, "cardradar.db")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
        "AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
    )
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

SUGGESTION_CATALOG = [

    {
        "title": "Pikachu",
        "query": "Pikachu",
        "subtitle": "Pokémon",
        "type": "pokemon",
        "type_label": "Pokémon",
    },

    {
        "title": "Pikachu ex",
        "query": "Pikachu ex",
        "subtitle": "Pokémon karta",
        "type": "card",
        "type_label": "Karta",
    },

    {
        "title": "Pikachu V",
        "query": "Pikachu V",
        "subtitle": "Pokémon karta",
        "type": "card",
        "type_label": "Karta",
    },

    {
        "title": "Pikachu VMAX",
        "query": "Pikachu VMAX",
        "subtitle": "Pokémon karta",
        "type": "card",
        "type_label": "Karta",
    },

    {
        "title": "Charizard",
        "query": "Charizard",
        "subtitle": "Pokémon",
        "type": "pokemon",
        "type_label": "Pokémon",
    },

    {
        "title": "Charizard ex",
        "query": "Charizard ex",
        "subtitle": "Pokémon karta",
        "type": "card",
        "type_label": "Karta",
    },

    {
        "title": "Charizard V",
        "query": "Charizard V",
        "subtitle": "Pokémon karta",
        "type": "card",
        "type_label": "Karta",
    },

    {
        "title": "Charizard VMAX",
        "query": "Charizard VMAX",
        "subtitle": "Pokémon karta",
        "type": "card",
        "type_label": "Karta",
    },

    {
        "title": "Mega Charizard X ex",
        "query": "Mega Charizard X ex",
        "subtitle": "Pokémon karta",
        "type": "card",
        "type_label": "Karta",
    },

    {
        "title": "Umbreon",
        "query": "Umbreon",
        "subtitle": "Pokémon",
        "type": "pokemon",
        "type_label": "Pokémon",
    },

    {
        "title": "Umbreon VMAX",
        "query": "Umbreon VMAX",
        "subtitle": "Pokémon karta",
        "type": "card",
        "type_label": "Karta",
    },

    {
        "title": "Mew",
        "query": "Mew",
        "subtitle": "Pokémon",
        "type": "pokemon",
        "type_label": "Pokémon",
    },

    {
        "title": "Mew ex",
        "query": "Mew ex",
        "subtitle": "Pokémon karta",
        "type": "card",
        "type_label": "Karta",
    },

    {
        "title": "Mewtwo",
        "query": "Mewtwo",
        "subtitle": "Pokémon",
        "type": "pokemon",
        "type_label": "Pokémon",
    },

    {
        "title": "Gengar",
        "query": "Gengar",
        "subtitle": "Pokémon",
        "type": "pokemon",
        "type_label": "Pokémon",
    },

    {
        "title": "Eevee",
        "query": "Eevee",
        "subtitle": "Pokémon",
        "type": "pokemon",
        "type_label": "Pokémon",
    },

    {
        "title": "Greninja",
        "query": "Greninja",
        "subtitle": "Pokémon",
        "type": "pokemon",
        "type_label": "Pokémon",
    },

    {
        "title": "Rayquaza",
        "query": "Rayquaza",
        "subtitle": "Pokémon",
        "type": "pokemon",
        "type_label": "Pokémon",
    },

    {
        "title": "Gardevoir",
        "query": "Gardevoir",
        "subtitle": "Pokémon",
        "type": "pokemon",
        "type_label": "Pokémon",
    },

    {
        "title": "Dragonite",
        "query": "Dragonite",
        "subtitle": "Pokémon",
        "type": "pokemon",
        "type_label": "Pokémon",
    },

    {
        "title": "Surging Sparks",
        "query": "Surging Sparks",
        "subtitle": "Pokémon set • SV8",
        "type": "set",
        "type_label": "Set",
    },

    {
        "title": "Surging Sparks Elite Trainer Box",
        "query": "Surging Sparks ETB",
        "subtitle": "Elite Trainer Box",
        "type": "product",
        "type_label": "Produkt",
    },

    {
        "title": "Pokémon 151",
        "query": "Pokémon 151",
        "subtitle": "Pokémon set",
        "type": "set",
        "type_label": "Set",
    },

    {
        "title": "Pokémon 151 Elite Trainer Box",
        "query": "Pokémon 151 ETB",
        "subtitle": "Elite Trainer Box",
        "type": "product",
        "type_label": "Produkt",
    },

    {
        "title": "Prismatic Evolutions",
        "query": "Prismatic Evolutions",
        "subtitle": "Pokémon set",
        "type": "set",
        "type_label": "Set",
    },

    {
        "title": "Prismatic Evolutions Elite Trainer Box",
        "query": "Prismatic Evolutions ETB",
        "subtitle": "Elite Trainer Box",
        "type": "product",
        "type_label": "Produkt",
    },

    {
        "title": "Terastal Festival",
        "query": "Terastal Festival",
        "subtitle": "Pokémon set • SV8a",
        "type": "set",
        "type_label": "Set",
    },

    {
        "title": "Terastal Festival Elite Trainer Box",
        "query": "Terastal Festival ETB",
        "subtitle": "Elite Trainer Box",
        "type": "product",
        "type_label": "Produkt",
    },

    {
        "title": "Destined Rivals",
        "query": "Destined Rivals",
        "subtitle": "Pokémon set",
        "type": "set",
        "type_label": "Set",
    },

    {
        "title": "Journey Together",
        "query": "Journey Together",
        "subtitle": "Pokémon set",
        "type": "set",
        "type_label": "Set",
    },

    {
        "title": "Twilight Masquerade",
        "query": "Twilight Masquerade",
        "subtitle": "Pokémon set",
        "type": "set",
        "type_label": "Set",
    },

    {
        "title": "Stellar Crown",
        "query": "Stellar Crown",
        "subtitle": "Pokémon set",
        "type": "set",
        "type_label": "Set",
    },

    {
        "title": "Temporal Forces",
        "query": "Temporal Forces",
        "subtitle": "Pokémon set",
        "type": "set",
        "type_label": "Set",
    },

    {
        "title": "Obsidian Flames",
        "query": "Obsidian Flames",
        "subtitle": "Pokémon set",
        "type": "set",
        "type_label": "Set",
    },

    {
        "title": "Mega Evolution",
        "query": "Mega Evolution",
        "subtitle": "Pokémon set",
        "type": "set",
        "type_label": "Set",
    },

    {
        "title": "Phantasmal Flames",
        "query": "Phantasmal Flames",
        "subtitle": "Pokémon set",
        "type": "set",
        "type_label": "Set",
    },

    {
        "title": "Elite Trainer Box",
        "query": "Elite Trainer Box",
        "subtitle": "Pokémon produkt",
        "type": "product",
        "type_label": "Produkt",
    },

    {
        "title": "Booster Box",
        "query": "Booster Box",
        "subtitle": "Pokémon produkt",
        "type": "product",
        "type_label": "Produkt",
    },

    {
        "title": "Booster Bundle",
        "query": "Booster Bundle",
        "subtitle": "Pokémon produkt",
        "type": "product",
        "type_label": "Produkt",
    },

    {
        "title": "Collection Box",
        "query": "Collection Box",
        "subtitle": "Pokémon produkt",
        "type": "product",
        "type_label": "Produkt",
    },

    {
        "title": "Premium Collection",
        "query": "Premium Collection",
        "subtitle": "Pokémon produkt",
        "type": "product",
        "type_label": "Produkt",
    },

    {
        "title": "Pokémon Tin",
        "query": "Pokémon Tin",
        "subtitle": "Pokémon produkt",
        "type": "product",
        "type_label": "Produkt",
    },

]


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

    # -----------------------------------------------------
    # CARD NUMBER
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # PRODUCT TYPE
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # SET
    # -----------------------------------------------------

    set_name = ""

    for alias, canonical in sorted(
        SET_ALIASES.items(),
        key=lambda x: len(x[0]),
        reverse=True
    ):

        pattern = r"\b" + re.escape(alias.lower()) + r"\b"

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

            pattern = r"\b" + re.escape(
                candidate.lower()
            ) + r"\b"

            if re.search(pattern, q):

                set_name = candidate

                q = re.sub(
                    pattern,
                    " ",
                    q
                )

                break

    # -----------------------------------------------------
    # POKEMON
    # -----------------------------------------------------

    pokemon = ""

    for alias, canonical in sorted(
        POKEMON_ALIASES.items(),
        key=lambda x: len(x[0]),
        reverse=True
    ):

        pattern = r"\b" + re.escape(alias.lower()) + r"\b"

        if re.search(pattern, q):

            pokemon = canonical

            q = re.sub(
                pattern,
                " ",
                q
            )

            break

    # -----------------------------------------------------
    # EX / V / VMAX / GX / VSTAR
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # CLEAN REMAINING TEXT
    # -----------------------------------------------------

    q = re.sub(
        r"\bpokemon\b",
        " ",
        q,
        flags=re.IGNORECASE
    )

    q = normalize_spaces(q)

    # -----------------------------------------------------
    # BUILD NORMALIZED QUERY
    # -----------------------------------------------------

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

    # -----------------------------------------------------
    # SPECIAL ETB / SET HANDLING
    # -----------------------------------------------------

    if product_type == "elite trainer box":

        normalized_parts = []

        if set_name:
            normalized_parts.append(set_name)

        if pokemon:
            normalized_parts.append(pokemon)

        if suffix:
            normalized_parts.append(suffix.lower())

        if card_number:
            normalized_parts.append(card_number)

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

    if parsed.get("set_name") and not parsed.get("pokemon"):
        return "sealed"

    return "card"


# =========================================================
# CARD NUMBER
# =========================================================

def card_number_from_query(query):

    match = re.search(
        r"\b(\d{1,4})\s*/\s*(\d{1,4})\b",
        query
    )

    if not match:
        return ""

    return (
        f"{match.group(1)}/"
        f"{match.group(2)}"
    )


# =========================================================
# CARD MATCH
# =========================================================

def card_matches_query(title, query):

    title_clean = clean_text(title).lower()

    query_clean = clean_text(query).lower()

    parsed = normalize_query(query_clean)

    important = []

    if parsed.get("pokemon"):
        important.append(
            parsed["pokemon"].lower()
        )

    if parsed.get("card_number"):
        important.append(
            parsed["card_number"].lower()
        )

    if "ex" in query_clean.split():
        important.append("ex")

    if "vmax" in query_clean.split():
        important.append("vmax")

    if "vstar" in query_clean.split():
        important.append("vstar")

    if "gx" in query_clean.split():
        important.append("gx")

    if not important:

        return True

    for item in important:

        if item not in title_clean:
            return False

    return True


# =========================================================
# SEALED MATCH
# =========================================================

def sealed_matches_query(title, query):

    title_clean = clean_text(title).lower()

    parsed = normalize_query(query)

    set_name = parsed.get("set_name", "").lower()

    product_type = parsed.get(
        "product_type",
        ""
    ).lower()

    if set_name:

        set_words = words(set_name)

        if not set_words.issubset(
            words(title_clean)
        ):

            return False

    if product_type:

        if product_type == "elite trainer box":

            if (
                "elite trainer box" not in title_clean
                and "etb" not in title_clean
            ):

                return False

        elif product_type not in title_clean:

            return False

    if (
        product_type == "elite trainer box"
        and re.search(
            r"\b(case|10x|12x|6x)\b",
            title_clean
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

    # EUR
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

            if "," in value and "." in value:

                if value.rfind(",") > value.rfind("."):

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

    # CZK
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

                return czk / CZK_PER_EUR

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

    t = clean_text(title).lower()

    return any(
        word in t
        for word in MERCH_BLACKLIST
    )


# =========================================================
# HTTP
# =========================================================

def get(url, timeout=15):

    try:

        return requests.get(
            url,
            headers=HEADERS,
            timeout=timeout
        )

    except Exception:

        return None


# =========================================================
# CARDYX
# =========================================================

def cardyx_search(query):

    results = []

    try:

        url = (
            "https://www.cardyx.sk/search"
            "?q=" +
            urllib.parse.quote(query)
        )

        response = get(url)

        if not response:
            return results

        if response.status_code != 200:
            return results

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        links = soup.select(
            'a[href*="/products/"]'
        )

        seen = set()

        for a in links:

            href = a.get("href")

            if not href:
                continue

            if href.startswith("/"):
                href = (
                    "https://www.cardyx.sk"
                    + href
                )

            if href in seen:
                continue

            seen.add(href)

            title = clean_text(
                a.get_text(" ", strip=True)
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

                price = parse_price(
                    a.parent.get_text(
                        " ",
                        strip=True
                    )
                    if a.parent
                    else ""
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
                    query
                ):
                    continue

            else:

                if not sealed_matches_query(
                    title,
                    query
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

    except Exception:

        return results

    unique = {}

    for item in results:

        key = (
            item["title"].lower(),
            item["price_eur"]
        )

        unique[key] = item

    return list(
        unique.values()
    )


# =========================================================
# GENERIC SHOP SEARCH
# =========================================================

GENERIC_SHOPS = [

    {
        "name": "Veselý Drak",
        "country": "CZ",
        "url": "https://www.vesely-drak.cz/",
    },

    {
        "name": "iHRYsko",
        "country": "SK",
        "url": "https://www.ihrysko.sk/",
    },

    {
        "name": "Černý Rytíř",
        "country": "CZ",
        "url": "https://www.cernyrytir.cz/",
    },

]


def generic_shop_search(shop, query):

    results = []

    try:

        if shop["name"] == "Veselý Drak":

            url = (
                "https://www.vesely-drak.cz/"
                "?s=" +
                urllib.parse.quote(query)
            )

        elif shop["name"] == "iHRYsko":

            url = (
                "https://www.ihrysko.sk/"
                "?s=" +
                urllib.parse.quote(query)
            )

        elif shop["name"] == "Černý Rytíř":

            url = (
                "https://www.cernyrytir.cz/"
                "?q=" +
                urllib.parse.quote(query)
            )

        else:

            return results

        response = get(url)

        if not response:
            return results

        if response.status_code != 200:
            return results

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        product_links = soup.find_all(
            "a",
            href=True
        )

        seen = set()

        for a in product_links:

            href = a.get("href")

            if not href:
                continue

            title = clean_text(
                a.get_text(
                    " ",
                    strip=True
                )
            )

            if not title:
                continue

            if len(title) < 4:
                continue

            if href in seen:
                continue

            seen.add(href)

            parent = a.parent

            block_text = ""

            if parent:

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

                price = parse_price(
                    title
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
                    query
                ):
                    continue

            else:

                if not sealed_matches_query(
                    title,
                    query
                ):
                    continue

            if href.startswith("/"):

                href = (
                    shop["url"].rstrip("/")
                    + href
                )

            results.append({
                "title": title,
                "shop": shop["name"],
                "country": shop["country"],
                "condition": "Nové",
                "price_eur": round(
                    price,
                    2
                ),
                "link": href,
            })

            if len(results) >= 10:
                break

    except Exception:

        return results

    return results


# =========================================================
# SEARCH ALL SHOPS
# =========================================================

def search_all(query):

    results = []

    # =====================================================
    # 1. CARDYX
    # =====================================================

    results.extend(
        cardyx_search(query)
    )

    # =====================================================
    # 2. VESELÝ DRAK
    # =====================================================

    vesel_drako = {
        "name": "Veselý Drak",
        "country": "CZ",
        "url": "https://www.vesely-drak.cz/",
    }

    results.extend(
        generic_shop_search(
            vesel_drako,
            query
        )
    )

    # =====================================================
    # REMOVE DUPLICATES
    # =====================================================

    unique = {}

    for item in results:

        key = (
            clean_text(
                item.get("shop", "")
            ).lower(),

            clean_text(
                item.get("title", "")
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

    # =====================================================
    # SORT BY PRICE
    # =====================================================

    results.sort(
        key=lambda x: float(
            x.get(
                "price_eur",
                999999
            )
        )
    )

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
        20 - len(title) // 10
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
            or title.startswith(q_lower)
        ):

            candidates.append(
                item.copy()
            )

    parsed = normalize_query(q)

    normalized = parsed.get(
        "normalized",
        ""
    )

    if normalized:

        normalized_lower = normalized.lower()

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

        if not exists and normalized_lower != q_lower:

            if parsed.get("product_type"):

                candidates.append({
                    "title": normalized,
                    "query": normalized,
                    "subtitle": "Automaticky rozpoznaný produkt",
                    "type": "product",
                    "type_label": "Produkt",
                })

            elif parsed.get("pokemon"):

                candidates.append({
                    "title": normalized,
                    "query": normalized,
                    "subtitle": "Automaticky rozpoznaná karta",
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

    parsed = normalize_query(q)

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
            "error": "Chýba vyhľadávanie."
        }), 400

    parsed = normalize_query(
        original_query
    )

    normalized_query = parsed.get(
        "normalized",
        original_query
    )

    results = search_all(
        normalized_query
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

    if parsed.get("set_name"):

        info["subtitle"] = (
            "Set: " +
            parsed["set_name"]
        )

    elif parsed.get("pokemon"):

        info["subtitle"] = (
            "Pokémon: " +
            parsed["pokemon"]
        )

    return jsonify({

        "query": original_query,

        "normalized_query":
            normalized_query,

        "parsed":
            parsed,

        "results":
            results,

        "czk_per_eur":
            CZK_PER_EUR,

        "info":
            info,

    })


# =========================================================
# HEALTH
# =========================================================

@app.get("/health")
def health():

    index_path = find_index()

    return jsonify({

        "service":
            "CardRadar",

        "status":
            "ok",

        "version":
            VERSION,

        "base_dir":
            BASE_DIR,

        "index_exists":
            bool(index_path),

        "index_path":
            index_path,

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
            <p>index.html nebol nájdený.</p>
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
                "<p>Chyba pri načítaní stránky.</p>"
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
