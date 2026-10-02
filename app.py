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
# VERSION 6.1
# SMART SEARCH / NORMALIZATION
# =========================================================

VERSION = "6.1"

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CZK_PER_EUR = 24.4618

DB_PATH = os.path.join(BASE_DIR, "cardradar.db")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
        "AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "sk-SK,sk;q=0.9,cs;q=0.8,en;q=0.7",
}


# =========================================================
# DATABASE
# =========================================================

def db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():

    conn = db_connection()

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

    conn.execute("""
        CREATE TABLE IF NOT EXISTS discovered_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            shop TEXT NOT NULL,
            link TEXT,
            price_eur REAL,
            product_type TEXT,
            first_seen TEXT,
            last_seen TEXT,
            UNIQUE(title, shop, link)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS suggestion_cache (
            query TEXT PRIMARY KEY,
            suggestions TEXT,
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


def normalize_spaces(text):

    return re.sub(
        r"\s+",
        " ",
        clean_text(text)
    ).strip()


def words(text):

    text = clean_text(text).lower()

    return set(
        re.findall(
            r"[a-z0-9]+",
            text
        )
    )


def normalize_compare(text):

    text = clean_text(text).lower()

    replacements = {
        "á": "a",
        "ä": "a",
        "č": "c",
        "ď": "d",
        "é": "e",
        "ě": "e",
        "í": "i",
        "ĺ": "l",
        "ľ": "l",
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
        text = text.replace(old, new)

    text = re.sub(
        r"[^a-z0-9]+",
        " ",
        text
    )

    return normalize_spaces(text)


# =========================================================
# SEARCH ALIASES
# =========================================================

SET_ALIASES = {

    # Scarlet & Violet
    "sv1": "scarlet violet base",
    "sv2": "paldea evolved",
    "sv3": "obsidian flames",
    "sv4": "paradox rift",
    "sv5": "temporal forces",
    "sv6": "twilight masquerade",
    "sv7": "stellar crown",
    "sv8": "surging sparks",
    "sv8a": "terastal festival",
    "sv9": "journey together",
    "sv9a": "destined rivals",
    "sv10": "destined rivals",

    # Special
    "sv10.5": "destined rivals",
    "sv11": "black bolt white flare",

    # Pokémon 151
    "151": "pokemon 151",
    "pokemon151": "pokemon 151",
    "pokemon 151": "pokemon 151",

    # Prismatic Evolutions
    "prismatic": "prismatic evolutions",
    "prismatic evo": "prismatic evolutions",
    "prismatic evolutions": "prismatic evolutions",

    # Surging Sparks
    "surging": "surging sparks",
    "sparks": "surging sparks",

    # Journey Together
    "journey": "journey together",

    # Destined Rivals
    "destined": "destined rivals",

    # Terastal Festival
    "terastal": "terastal festival",
    "terastal festival": "terastal festival",

    # Phantasmal Flames
    "phantasmal": "phantasmal flames",
    "phantasmal flames": "phantasmal flames",
    "pfl": "phantasmal flames",

    # Paldean Fates
    "paf": "paldean fates",

    # Mega Evolution
    "mega brave": "mega evolution mega brave",
    "mega evolution": "mega evolution",
    "me01": "mega evolution",
    "me02": "phantasmal flames",
}


PRODUCT_ALIASES = {

    "etb": "elite trainer box",
    "elite trainer": "elite trainer box",
    "elite trainer box": "elite trainer box",

    "booster box": "booster box",
    "boosterbox": "booster box",
    "bb": "booster box",

    "booster bundle": "booster bundle",
    "boosterbundle": "booster bundle",
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


# =========================================================
# POKÉMON ALIASES
# =========================================================

POKEMON_ALIASES = {

    # Mega Charizard
    "mega charizard x ex": "Mega Charizard X ex",
    "mega charizard x": "Mega Charizard X",
    "mega charizard": "Mega Charizard",

    # Pikachu
    "pikachu vmax": "Pikachu VMAX",
    "pikachu vstar": "Pikachu VSTAR",
    "pikachu ex": "Pikachu ex",
    "pikachu v": "Pikachu V",
    "pikachu": "Pikachu",
    "pika": "Pikachu",

    # Charizard
    "charizard vmax": "Charizard VMAX",
    "charizard vstar": "Charizard VSTAR",
    "charizard ex": "Charizard ex",
    "charizard v": "Charizard V",
    "charizard": "Charizard",
    "char": "Charizard",

    # Eeveelutions
    "umbreon vmax": "Umbreon VMAX",
    "umbreon vstar": "Umbreon VSTAR",
    "umbreon ex": "Umbreon ex",
    "umbreon": "Umbreon",

    "eevee": "Eevee",

    "espeon": "Espeon",
    "sylveon": "Sylveon",
    "leafeon": "Leafeon",
    "glaceon": "Glaceon",
    "jolteon": "Jolteon",
    "vaporeon": "Vaporeon",
    "flareon": "Flareon",

    # Other popular Pokémon
    "mewtwo": "Mewtwo",
    "mew ex": "Mew ex",
    "mew": "Mew",

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
}


KNOWN_SETS = [

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
    "paradox rift",
    "paldea evolved",
    "scarlet violet base",
    "mega evolution",
    "phantasmal flames",
    "black bolt",
    "white flare",
    "paldean fates",
]


# =========================================================
# AUTOCOMPLETE CATALOG
# =========================================================

SUGGESTION_CATALOG = [

    # POKÉMON

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
        "title": "Mega Charizard X",
        "query": "Mega Charizard X",
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

    # SETS

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

    # PRODUCTS

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
            "suffix": "",
        }

    q = original.lower()

    # =====================================================
    # CARD NUMBER
    # =====================================================

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

        q = re.sub(
            r"\b\d{1,4}\s*/\s*\d{1,4}\b",
            " ",
            q
        )

    # =====================================================
    # PRODUCT TYPE
    # =====================================================

    product_type = ""

    product_patterns = [

        (
            "elite trainer box",
            r"\belite\s+trainer\s+box\b"
        ),

        (
            "elite trainer box",
            r"\betb\b"
        ),

        (
            "booster box",
            r"\bbooster\s*box\b"
        ),

        (
            "booster bundle",
            r"\bbooster\s*bundle\b"
        ),

        (
            "collection box",
            r"\bcollection\s+box\b"
        ),

        (
            "premium collection",
            r"\bpremium\s+collection\b"
        ),

        (
            "blister",
            r"\bblister(?:\s+pack)?\b"
        ),

        (
            "tin",
            r"\btins?\b"
        ),
    ]

    for canonical, pattern in product_patterns:

        if re.search(
            pattern,
            q,
            re.IGNORECASE
        ):

            product_type = canonical

            q = re.sub(
                pattern,
                " ",
                q,
                flags=re.IGNORECASE
            )

            break

    # =====================================================
    # SET
    # =====================================================

    set_name = ""

    aliases = sorted(
        SET_ALIASES.items(),
        key=lambda x: len(x[0]),
        reverse=True
    )

    for alias, canonical in aliases:

        pattern = (
            r"(?<![a-z0-9])"
            +
            re.escape(alias.lower())
            +
            r"(?![a-z0-9])"
        )

        if re.search(
            pattern,
            q
        ):

            set_name = canonical

            q = re.sub(
                pattern,
                " ",
                q
            )

            break

    if not set_name:

        for candidate in sorted(
            KNOWN_SETS,
            key=len,
            reverse=True
        ):

            pattern = (
                r"(?<![a-z0-9])"
                +
                re.escape(candidate.lower())
                +
                r"(?![a-z0-9])"
            )

            if re.search(
                pattern,
                q
            ):

                set_name = candidate

                q = re.sub(
                    pattern,
                    " ",
                    q
                )

                break

    # =====================================================
    # POKÉMON
    # =====================================================

    pokemon = ""

    for alias, canonical in sorted(
        POKEMON_ALIASES.items(),
        key=lambda x: len(x[0]),
        reverse=True
    ):

        pattern = (
            r"(?<![a-z0-9])"
            +
            re.escape(alias.lower())
            +
            r"(?![a-z0-9])"
        )

        if re.search(
            pattern,
            q
        ):

            pokemon = canonical

            q = re.sub(
                pattern,
                " ",
                q
            )

            break

    # =====================================================
    # CARD SUFFIX
    # =====================================================

    suffix = ""

    suffix_match = re.search(
        r"\b(vmax|vstar|ex|gx|v)\b",
        q,
        re.IGNORECASE
    )

    if suffix_match:

        suffix = suffix_match.group(1).lower()

        q = re.sub(
            r"\b(vmax|vstar|ex|gx|v)\b",
            " ",
            q,
            flags=re.IGNORECASE
        )

    # =====================================================
    # REMOVE GENERIC WORDS
    # =====================================================

    q = re.sub(
        r"\bpokemon\b",
        " ",
        q,
        flags=re.IGNORECASE
    )

    q = re.sub(
        r"\bcard\b",
        " ",
        q,
        flags=re.IGNORECASE
    )

    q = re.sub(
        r"\bkarta\b",
        " ",
        q,
        flags=re.IGNORECASE
    )

    q = re.sub(
        r"\bpokemon\s+card\b",
        " ",
        q,
        flags=re.IGNORECASE
    )

    q = re.sub(
        r"\bfull\s+art\b",
        " ",
        q,
        flags=re.IGNORECASE
    )

    q = normalize_spaces(q)

    # =====================================================
    # BUILD NORMALIZED QUERY
    # =====================================================

    parts = []

    if pokemon:
        parts.append(pokemon)

    if suffix:
        parts.append(suffix)

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

    # =====================================================
    # SPECIAL ORDER FOR SEALED PRODUCTS
    # =====================================================

    if product_type:

        normalized_parts = []

        if set_name:
            normalized_parts.append(
                set_name
            )

        if pokemon:
            normalized_parts.append(
                pokemon
            )

        if suffix:
            normalized_parts.append(
                suffix
            )

        if card_number:
            normalized_parts.append(
                card_number
            )

        normalized_parts.append(
            product_type
        )

        normalized = normalize_spaces(
            " ".join(normalized_parts)
        )

    return {

        "original":
            original,

        "normalized":
            normalized or original,

        "pokemon":
            pokemon,

        "set_name":
            set_name,

        "product_name":
            product_type,

        "product_type":
            product_type,

        "card_number":
            card_number,

        "suffix":
            suffix,
    }


# =========================================================
# QUERY CLASSIFICATION
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

    "coin",
    "mince",
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
# PRODUCT TYPE DETECTION
# =========================================================

def detect_product_type(title):

    t = normalize_compare(
        title
    )

    if (
        "elite trainer box" in t
        or re.search(
            r"\betb\b",
            t
        )
    ):
        return "elite trainer box"

    if "booster box" in t:
        return "booster box"

    if "booster bundle" in t:
        return "booster bundle"

    if "collection box" in t:
        return "collection box"

    if "premium collection" in t:
        return "premium collection"

    if "blister" in t:
        return "blister"

    if re.search(
        r"\btin\b",
        t
    ):
        return "tin"

    return "card"


# =========================================================
# CARD MATCH
# =========================================================

def card_matches_query(
    title,
    query
):

    title_clean = normalize_compare(
        title
    )

    query_clean = normalize_compare(
        query
    )

    parsed = normalize_query(
        query
    )

    pokemon = normalize_compare(
        parsed.get(
            "pokemon",
            ""
        )
    )

    suffix = normalize_compare(
        parsed.get(
            "suffix",
            ""
        )
    )

    card_number = normalize_compare(
        parsed.get(
            "card_number",
            ""
        )
    )

    set_name = normalize_compare(
        parsed.get(
            "set_name",
            ""
        )
    )

    # -----------------------------------------------------
    # Reject sealed products
    # -----------------------------------------------------

    if detect_product_type(title) != "card":
        return False

    # -----------------------------------------------------
    # Reject merch
    # -----------------------------------------------------

    if is_merch(title):
        return False

    # -----------------------------------------------------
    # Pokémon
    # -----------------------------------------------------

    if pokemon:

        pokemon_compare = normalize_compare(
            pokemon
        )

        if pokemon_compare not in title_clean:
            return False

    # -----------------------------------------------------
    # Suffix
    # -----------------------------------------------------

    if suffix:

        if suffix not in title_clean:
            return False

    # -----------------------------------------------------
    # Card number
    # -----------------------------------------------------

    if card_number:

        compact_title = title_clean.replace(
            " ",
            ""
        )

        compact_number = card_number.replace(
            " ",
            ""
        )

        if compact_number not in compact_title:
            return False

    # -----------------------------------------------------
    # Set
    # -----------------------------------------------------

    if set_name:

        set_words = words(
            set_name
        )

        if not set_words.issubset(
            words(title_clean)
        ):

            # Allow common abbreviation
            if (
                set_name == "phantasmal flames"
                and "pfl" in title_clean
            ):
                pass
            else:
                return False

    # -----------------------------------------------------
    # Generic query
    # -----------------------------------------------------

    if (
        not pokemon
        and not suffix
        and not card_number
        and not set_name
    ):

        query_words = [

            x

            for x in words(
                query_clean
            )

            if len(x) >= 3

        ]

        if query_words:

            if not all(
                word in title_clean
                for word in query_words
            ):
                return False

    return True


# =========================================================
# SEALED MATCH
# =========================================================

def sealed_matches_query(
    title,
    query
):

    title_clean = normalize_compare(
        title
    )

    parsed = normalize_query(
        query
    )

    set_name = normalize_compare(
        parsed.get(
            "set_name",
            ""
        )
    )

    product_type = normalize_compare(
        parsed.get(
            "product_type",
            ""
        )
    )

    pokemon = normalize_compare(
        parsed.get(
            "pokemon",
            ""
        )
    )

    if is_merch(title):
        return False

    # -----------------------------------------------------
    # Reject cases / displays
    # -----------------------------------------------------

    if re.search(
        r"\b(case|10x|12x|6x|display|carton)\b",
        title_clean
    ):
        return False

    # -----------------------------------------------------
    # Product type
    # -----------------------------------------------------

    if product_type:

        if product_type == "elite trainer box":

            if (
                "elite trainer box"
                not in title_clean
                and "etb"
                not in title_clean
            ):
                return False

        elif product_type not in title_clean:

            return False

    # -----------------------------------------------------
    # Set
    # -----------------------------------------------------

    if set_name:

        set_words = words(
            set_name
        )

        title_words = words(
            title_clean
        )

        if not set_words.issubset(
            title_words
        ):

            # Special abbreviation support
            if (
                set_name == "phantasmal flames"
                and "pfl" in title_clean
            ):
                pass
            else:
                return False

    # -----------------------------------------------------
    # Pokémon
    # -----------------------------------------------------

    if pokemon:

        if pokemon not in title_clean:
            return False

    return True


# =========================================================
# PRICE PARSER
# =========================================================

def parse_price(text):

    if not text:
        return None

    text = clean_text(
        text
    )

    # -----------------------------------------------------
    # EUR
    # -----------------------------------------------------

    eur_patterns = [

        r"(\d{1,6}(?:[.,]\d{1,2})?)\s*€",

        r"€\s*(\d{1,6}(?:[.,]\d{1,2})?)",

        r"(\d{1,6}(?:[.,]\d{1,2})?)\s*EUR\b",
    ]

    for pattern in eur_patterns:

        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if match:

            value = match.group(
                1
            )

            value = value.replace(
                " ",
                ""
            )

            if (
                "," in value
                and
                "." in value
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

                number = float(
                    value
                )

                if (
                    0 <
                    number <
                    100000
                ):

                    return number

            except Exception:
                pass

    # -----------------------------------------------------
    # CZK
    # -----------------------------------------------------

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

            value = match.group(
                1
            )

            value = value.replace(
                ",",
                "."
            )

            try:

                czk = float(
                    value
                )

                if (
                    0 <
                    czk <
                    10000000
                ):

                    return (
                        czk /
                        CZK_PER_EUR
                    )

            except Exception:
                pass

    return None


# =========================================================
# HTTP
# =========================================================

def get(
    url,
    timeout=15
):

    try:

        response = requests.get(
            url,
            headers=HEADERS,
            timeout=timeout
        )

        return response

    except Exception:

        return None


# =========================================================
# CARDYX PRODUCT DISCOVERY
# =========================================================

def cardyx_discover(
    query
):

    results = []

    try:

        url = (
            "https://www.cardyx.sk/search"
            "?q="
            +
            urllib.parse.quote(
                query
            )
        )

        response = get(
            url,
            timeout=12
        )

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

            href = a.get(
                "href"
            )

            if not href:
                continue

            if href.startswith("/"):

                href = (
                    "https://www.cardyx.sk"
                    +
                    href
                )

            if href in seen:
                continue

            seen.add(
                href
            )

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

            if (
                price is None
                and
                a.parent
            ):

                price = parse_price(
                    a.parent.get_text(
                        " ",
                        strip=True
                    )
                )

            product_type = detect_product_type(
                title
            )

            results.append({

                "title":
                    title,

                "shop":
                    "CardyX",

                "country":
                    "SK",

                "condition":
                    "Nové",

                "price_eur":
                    (
                        round(
                            price,
                            2
                        )
                        if price is not None
                        else None
                    ),

                "link":
                    href,

                "product_type":
                    product_type,
            })

            if len(results) >= 20:
                break

    except Exception:

        return results

    return results


# =========================================================
# SAVE DISCOVERED PRODUCTS
# =========================================================

def save_discovered_products(
    results
):

    if not results:
        return

    now = datetime.now(
        timezone.utc
    ).isoformat()

    conn = db_connection()

    for item in results:

        title = clean_text(
            item.get(
                "title",
                ""
            )
        )

        shop = clean_text(
            item.get(
                "shop",
                ""
            )
        )

        link = clean_text(
            item.get(
                "link",
                ""
            )
        )

        if (
            not title
            or
            not shop
        ):
            continue

        conn.execute(
            """
            INSERT INTO discovered_products
            (
                title,
                shop,
                link,
                price_eur,
                product_type,
                first_seen,
                last_seen
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)

            ON CONFLICT(
                title,
                shop,
                link
            )

            DO UPDATE SET

                price_eur =
                    excluded.price_eur,

                product_type =
                    excluded.product_type,

                last_seen =
                    excluded.last_seen
            """,
            (
                title,
                shop,
                link,
                item.get(
                    "price_eur"
                ),
                item.get(
                    "product_type",
                    detect_product_type(
                        title
                    )
                ),
                now,
                now,
            )
        )

    conn.commit()
    conn.close()


# =========================================================
# CARDYX SEARCH
# =========================================================

def cardyx_search(
    query
):

    discovered = cardyx_discover(
        query
    )

    save_discovered_products(
        discovered
    )

    results = []

    parsed = normalize_query(
        query
    )

    kind = classify_query(
        parsed
    )

    for item in discovered:

        title = item[
            "title"
        ]

        price = item.get(
            "price_eur"
        )

        if price is None:
            continue

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

            "title":
                title,

            "shop":
                item["shop"],

            "country":
                item["country"],

            "condition":
                item["condition"],

            "price_eur":
                round(
                    price,
                    2
                ),

            "link":
                item["link"],
        })

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
# GENERIC SHOPS
# =========================================================

GENERIC_SHOPS = [

    {
        "name":
            "Veselý Drak",

        "country":
            "CZ",

        "url":
            "https://www.vesely-drak.cz/",
    },

    {
        "name":
            "iHRYsko",

        "country":
            "SK",

        "url":
            "https://www.ihrysko.sk/",
    },

    {
        "name":
            "Černý Rytíř",

        "country":
            "CZ",

        "url":
            "https://www.cernyrytir.cz/",
    },
]


# =========================================================
# GENERIC SHOP SEARCH
# =========================================================

def generic_shop_search(
    shop,
    query
):

    results = []

    try:

        if shop["name"] == "Veselý Drak":

            url = (
                "https://www.vesely-drak.cz/"
                "?s="
                +
                urllib.parse.quote(
                    query
                )
            )

        elif shop["name"] == "iHRYsko":

            url = (
                "https://www.ihrysko.sk/"
                "?s="
                +
                urllib.parse.quote(
                    query
                )
            )

        elif shop["name"] == "Černý Rytíř":

            url = (
                "https://www.cernyrytir.cz/"
                "?q="
                +
                urllib.parse.quote(
                    query
                )
            )

        else:

            return results

        response = get(
            url,
            timeout=12
        )

        if not response:
            return results

        if response.status_code != 200:
            return results

        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )

        parsed = normalize_query(
            query
        )

        kind = classify_query(
            parsed
        )

        seen = set()

        containers = soup.select(
            """
            article,
            .product,
            .product-item,
            .product-box,
            .product-card,
            li.product,
            .item
            """
        )

        if not containers:

            containers = soup.find_all(
                "a",
                href=True
            )

        for container in containers:

            if container.name == "a":

                a = container

            else:

                a = container.find(
                    "a",
                    href=True
                )

            if not a:
                continue

            href = a.get(
                "href",
                ""
            )

            if not href:
                continue

            title_element = (

                container.find(
                    [
                        "h1",
                        "h2",
                        "h3",
                        "h4",
                        "h5",
                        "h6"
                    ]
                )

                if container.name != "a"

                else None
            )

            if title_element:

                title = clean_text(
                    title_element.get_text(
                        " ",
                        strip=True
                    )
                )

            else:

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

            title_key = normalize_compare(
                title
            )

            if title_key in seen:
                continue

            seen.add(
                title_key
            )

            block_text = clean_text(
                container.get_text(
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
                    +
                    href
                )

            product_type = detect_product_type(
                title
            )

            results.append({

                "title":
                    title,

                "shop":
                    shop["name"],

                "country":
                    shop["country"],

                "condition":
                    "Nové",

                "price_eur":
                    round(
                        price,
                        2
                    ),

                "link":
                    href,

                "product_type":
                    product_type,
            })

            if len(results) >= 15:
                break

    except Exception:

        return results

    save_discovered_products(
        results
    )

    return results


# =========================================================
# SEARCH ALL SHOPS
# =========================================================

def search_all(
    query
):

    results = []

    results.extend(
        cardyx_search(
            query
        )
    )

    with ThreadPoolExecutor(
        max_workers=3
    ) as executor:

        futures = [

            executor.submit(
                generic_shop_search,
                shop,
                query
            )

            for shop in GENERIC_SHOPS

        ]

        for future in as_completed(
            futures
        ):

            try:

                results.extend(
                    future.result()
                )

            except Exception:
                pass

    unique = {}

    for item in results:

        title = clean_text(
            item.get(
                "title",
                ""
            )
        )

        shop = clean_text(
            item.get(
                "shop",
                ""
            )
        )

        try:

            price = round(
                float(
                    item.get(
                        "price_eur",
                        0
                    )
                ),
                2
            )

        except Exception:

            price = 0

        key = (

            shop.lower(),

            title.lower(),

            price

        )

        unique[key] = item

    results = list(
        unique.values()
    )

    results.sort(
        key=lambda x:
        float(
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

    conn = db_connection()

    now = datetime.now(
        timezone.utc
    ).isoformat()

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
# DISCOVERED SUGGESTIONS
# =========================================================

def get_discovered_suggestions(
    query,
    limit=12
):

    q = normalize_compare(
        query
    )

    if not q:
        return []

    conn = db_connection()

    rows = conn.execute(
        """
        SELECT
            title,
            shop,
            link,
            price_eur,
            product_type,
            last_seen

        FROM discovered_products

        WHERE lower(title) LIKE ?

        ORDER BY last_seen DESC

        LIMIT 100
        """,
        (
            "%" +
            q +
            "%",
        )
    ).fetchall()

    conn.close()

    output = []

    seen = set()

    for row in rows:

        title = clean_text(
            row["title"]
        )

        key = normalize_compare(
            title
        )

        if (
            not key
            or
            key in seen
        ):
            continue

        seen.add(
            key
        )

        product_type = (

            row["product_type"]

            or

            detect_product_type(
                title
            )
        )

        if product_type == "card":

            type_label = "Karta"

            subtitle = (
                "Reálny produkt • "
                +
                str(
                    row["shop"]
                )
            )

        else:

            type_label = "Produkt"

            subtitle = (
                product_type.title()
                +
                " • "
                +
                str(
                    row["shop"]
                )
            )

        output.append({

            "title":
                title,

            "query":
                title,

            "subtitle":
                subtitle,

            "type":
                (
                    "card"
                    if product_type == "card"
                    else "product"
                ),

            "type_label":
                type_label,

            "shop":
                row["shop"],

            "price_eur":
                row["price_eur"],

            "link":
                row["link"],
        })

        if len(output) >= limit:
            break

    return output


# =========================================================
# SUGGESTION SCORING
# =========================================================

def suggestion_score(
    item,
    query
):

    q = normalize_compare(
        query
    )

    title = normalize_compare(
        item.get(
            "title",
            ""
        )
    )

    subtitle = normalize_compare(
        item.get(
            "subtitle",
            ""
        )
    )

    score = 0

    if title == q:
        score += 300

    if title.startswith(q):
        score += 150

    if any(
        word.startswith(q)
        for word in title.split()
    ):
        score += 90

    if q in title:
        score += 60

    if q in subtitle:
        score += 15

    if item.get("shop"):
        score += 25

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

            "query":
                q,

            "normalized_query":
                "",

            "suggestions":
                [],
        })

    candidates = []

    q_compare = normalize_compare(
        q
    )

    # -----------------------------------------------------
    # STATIC CATALOG
    # -----------------------------------------------------

    for item in SUGGESTION_CATALOG:

        title = normalize_compare(
            item.get(
                "title",
                ""
            )
        )

        subtitle = normalize_compare(
            item.get(
                "subtitle",
                ""
            )
        )

        if (

            q_compare in title

            or

            q_compare in subtitle

            or

            title.startswith(
                q_compare
            )

        ):

            candidates.append(
                item.copy()
            )

    # -----------------------------------------------------
    # REAL DISCOVERED PRODUCTS
    # -----------------------------------------------------

    candidates.extend(
        get_discovered_suggestions(
            q,
            limit=15
        )
    )

    # -----------------------------------------------------
    # LIVE CARDYX DISCOVERY
    # -----------------------------------------------------

    if len(q) >= 3:

        live_products = cardyx_discover(
            q
        )

        if live_products:

            save_discovered_products(
                live_products
            )

            for product in live_products:

                title = product.get(
                    "title",
                    ""
                )

                if not title:
                    continue

                product_type = (

                    product.get(
                        "product_type"
                    )

                    or

                    detect_product_type(
                        title
                    )
                )

                if product_type == "card":

                    type_label = "Karta"

                else:

                    type_label = "Produkt"

                candidates.append({

                    "title":
                        title,

                    "query":
                        title,

                    "subtitle":
                        (
                            "Karta"
                            if product_type == "card"
                            else product_type.title()
                        )
                        +
                        " • CardyX",

                    "type":
                        (
                            "card"
                            if product_type == "card"
                            else "product"
                        ),

                    "type_label":
                        type_label,

                    "shop":
                        "CardyX",

                    "price_eur":
                        product.get(
                            "price_eur"
                        ),

                    "link":
                        product.get(
                            "link",
                            ""
                        ),
                })

    # -----------------------------------------------------
    # NORMALIZED INTERPRETATION
    # -----------------------------------------------------

    parsed = normalize_query(
        q
    )

    normalized = parsed.get(
        "normalized",
        ""
    )

    if normalized:

        normalized_compare = normalize_compare(
            normalized
        )

        exists = any(

            normalize_compare(

                item.get(
                    "query",
                    item.get(
                        "title",
                        ""
                    )
                )

            )
            ==
            normalized_compare

            for item in candidates
        )

        if (

            not exists

            and

            normalized_compare
            !=
            q_compare

        ):

            if parsed.get(
                "product_type"
            ):

                candidates.append({

                    "title":
                        normalized,

                    "query":
                        normalized,

                    "subtitle":
                        "Automaticky rozpoznaný produkt",

                    "type":
                        "product",

                    "type_label":
                        "Produkt",
                })

            elif parsed.get(
                "pokemon"
            ):

                candidates.append({

                    "title":
                        normalized,

                    "query":
                        normalized,

                    "subtitle":
                        "Automaticky rozpoznaná karta",

                    "type":
                        "card",

                    "type_label":
                        "Karta",
                })

    # -----------------------------------------------------
    # SORT
    # -----------------------------------------------------

    candidates.sort(
        key=lambda item:
        suggestion_score(
            item,
            q
        ),
        reverse=True
    )

    # -----------------------------------------------------
    # DEDUPLICATE
    # -----------------------------------------------------

    output = []

    seen = set()

    for item in candidates:

        key = normalize_compare(
            item.get(
                "query",
                item.get(
                    "title",
                    ""
                )
            )
        )

        if not key:
            continue

        if key in seen:
            continue

        seen.add(
            key
        )

        output.append(
            item
        )

        if len(output) >= 10:
            break

    return jsonify({

        "query":
            q,

        "normalized_query":
            normalized,

        "suggestions":
            output,
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

    return jsonify(
        parsed
    )


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

    results = search_all(
        normalized_query
    )

    save_history(
        original_query,
        results
    )

    # =====================================================
    # PRICE SUMMARY
    # =====================================================

    prices = [

        float(
            item["price_eur"]
        )

        for item in results

        if item.get(
            "price_eur"
        ) is not None

    ]

    summary = {

        "count":
            len(results),

        "shops":
            len(
                set(
                    item.get(
                        "shop",
                        ""
                    )
                    for item in results
                )
            ),

        "lowest_eur":
            (
                round(
                    min(prices),
                    2
                )

                if prices

                else None
            ),

        "highest_eur":
            (
                round(
                    max(prices),
                    2
                )

                if prices

                else None
            ),

        "average_eur":
            (
                round(
                    sum(prices)
                    /
                    len(prices),
                    2
                )

                if prices

                else None
            ),
    }

    # =====================================================
    # INFO
    # =====================================================

    info = {

        "title":
            normalized_query,

        "subtitle":
            "",

        "image":
            "",
    }

    if parsed.get(
        "set_name"
    ):

        info["subtitle"] = (
            "Set: "
            +
            parsed[
                "set_name"
            ]
        )

    elif parsed.get(
        "pokemon"
    ):

        info["subtitle"] = (
            "Pokémon: "
            +
            parsed[
                "pokemon"
            ]
        )

    if parsed.get(
        "product_type"
    ):

        info["subtitle"] = (

            info["subtitle"]

            +
            (
                " • "
                if info["subtitle"]
                else ""
            )

            +
            parsed[
                "product_type"
            ]
        )

    return jsonify({

        "query":
            original_query,

        "normalized_query":
            normalized_query,

        "parsed":
            parsed,

        "results":
            results,

        "summary":
            summary,

        "czk_per_eur":
            CZK_PER_EUR,

        "info":
            info,
    })


# =========================================================
# API DISCOVERED PRODUCTS
# =========================================================

@app.get("/api/products")
def api_products():

    q = clean_text(
        request.args.get(
            "q",
            ""
        )
    )

    limit = request.args.get(
        "limit",
        "30"
    )

    try:

        limit = min(
            max(
                int(limit),
                1
            ),
            100
        )

    except Exception:

        limit = 30

    if q:

        suggestions = get_discovered_suggestions(
            q,
            limit
        )

        return jsonify({

            "query":
                q,

            "products":
                suggestions,
        })

    conn = db_connection()

    rows = conn.execute(
        """
        SELECT
            title,
            shop,
            link,
            price_eur,
            product_type,
            last_seen

        FROM discovered_products

        ORDER BY last_seen DESC

        LIMIT ?
        """,
        (
            limit,
        )
    ).fetchall()

    conn.close()

    products = []

    for row in rows:

        products.append({

            "title":
                row["title"],

            "shop":
                row["shop"],

            "link":
                row["link"],

            "price_eur":
                row["price_eur"],

            "product_type":
                row["product_type"],

            "last_seen":
                row["last_seen"],
        })

    return jsonify({

        "query":
            "",

        "products":
            products,
    })


# =========================================================
# HEALTH
# =========================================================

@app.get("/health")
def health():

    index_path = find_index()

    try:

        conn = db_connection()

        discovered_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM discovered_products
            """
        ).fetchone()[0]

        history_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM price_history
            """
        ).fetchone()[0]

        conn.close()

    except Exception:

        discovered_count = 0
        history_count = 0

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

        "discovered_products":
            discovered_count,

        "price_history":
            history_count,
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
