import os
import re
import sqlite3
import urllib.parse
import time
import copy
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, Response
# =========================================================
# CARD RADAR
# VERSION 5.16
# CARDYX FOCUS
# STABILITY + SPEED + REAL AUTOCOMPLETE + PRODUCT IMAGES
# =========================================================
VERSION = "5.16"
app = Flask(__name__)
BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)
CZK_PER_EUR = 24.4618
DB_PATH = os.path.join(
    BASE_DIR,
    "cardradar.db"
)
# =========================================================
# PERFORMANCE SETTINGS
# =========================================================
# Cache normálneho vyhľadávania
CACHE_TTL = 120
CACHE_MAX_ITEMS = 50
# Cache autocomplete
SUGGESTION_CACHE_TTL = 120
SUGGESTION_CACHE_MAX_ITEMS = 100
# Hlavné vyhľadávanie CardyX
SEARCH_TIMEOUT = 10
# =========================================================
# IMAGE SETTINGS
# =========================================================
IMAGE_CACHE_TTL = 3600
IMAGE_CACHE_MAX_ITEMS = 500
# Viac paralelných requestov pri normálnom searchi
IMAGE_FETCH_WORKERS = 8
# Kratší timeout, aby obrázky nezdržiavali search
IMAGE_FETCH_TIMEOUT = 3
# =========================================================
# HTTP HEADERS
# =========================================================
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
    "Accept-Language": (
        "cs-CZ,sk-SK;q=0.9,en;q=0.8"
    ),
    "Connection": "keep-alive",
}
# =========================================================
# THREAD LOCAL HTTP SESSION
# =========================================================
_http_local = threading.local()
def get_http_session():
    session = getattr(
        _http_local,
        "session",
        None
    )
    if session is None:
        session = requests.Session()
        session.headers.update(
            HEADERS
        )
        _http_local.session = session
    return session
# =========================================================
# SEARCH CACHE
# =========================================================
_search_cache = {}
_search_cache_lock = threading.Lock()
def cache_key(query):
    return normalize_spaces(
        query
    ).lower()
def cache_get(query):
    key = cache_key(
        query
    )
    now = time.monotonic()
    with _search_cache_lock:
        item = _search_cache.get(
            key
        )
        if not item:
            return None
        timestamp = item.get(
            "timestamp",
            0
        )
        if (
            now -
            timestamp
            >
            CACHE_TTL
        ):
            _search_cache.pop(
                key,
                None
            )
            return None
        return copy.deepcopy(
            item.get(
                "data"
            )
        )
def cache_set(
    query,
    data
):
    key = cache_key(
        query
    )
    with _search_cache_lock:
        if (
            key not in _search_cache
            and
            len(_search_cache)
            >= CACHE_MAX_ITEMS
        ):
            oldest_key = min(
                _search_cache,
                key=lambda k:
                _search_cache[k].get(
                    "timestamp",
                    0
                )
            )
            _search_cache.pop(
                oldest_key,
                None
            )
        _search_cache[key] = {
            "timestamp":
                time.monotonic(),
            "data":
                copy.deepcopy(
                    data
                ),
        }
def cache_clear():
    with _search_cache_lock:
        _search_cache.clear()
# =========================================================
# AUTOCOMPLETE CACHE
# =========================================================
_suggestion_cache = {}
_suggestion_cache_lock = threading.Lock()
def suggestion_cache_key(query):
    return normalize_spaces(
        query
    ).lower()
def suggestion_cache_get(query):
    key = suggestion_cache_key(
        query
    )
    now = time.monotonic()
    with _suggestion_cache_lock:
        item = _suggestion_cache.get(
            key
        )
        if not item:
            return None
        timestamp = item.get(
            "timestamp",
            0
        )
        if (
            now -
            timestamp
            >
            SUGGESTION_CACHE_TTL
        ):
            _suggestion_cache.pop(
                key,
                None
            )
            return None
        return copy.deepcopy(
            item.get(
                "data",
                []
            )
        )
def suggestion_cache_set(
    query,
    data
):
    key = suggestion_cache_key(
        query
    )
    with _suggestion_cache_lock:
        if (
            key not in _suggestion_cache
            and
            len(_suggestion_cache)
            >= SUGGESTION_CACHE_MAX_ITEMS
        ):
            oldest_key = min(
                _suggestion_cache,
                key=lambda k:
                _suggestion_cache[k].get(
                    "timestamp",
                    0
                )
            )
            _suggestion_cache.pop(
                oldest_key,
                None
            )
        _suggestion_cache[key] = {
            "timestamp":
                time.monotonic(),
            "data":
                copy.deepcopy(
                    data
                ),
        }
def suggestion_cache_clear():
    with _suggestion_cache_lock:
        _suggestion_cache.clear()
# =========================================================
# IMAGE CACHE
# =========================================================
_image_cache = {}
_image_cache_lock = threading.Lock()
def image_cache_get(
    product_url
):
    product_url = clean_text(
        product_url
    )
    if not product_url:
        return None
    now = time.monotonic()
    with _image_cache_lock:
        item = _image_cache.get(
            product_url
        )
        if item is None:
            return None
        timestamp = item.get(
            "timestamp",
            0
        )
        if (
            now -
            timestamp
            >
            IMAGE_CACHE_TTL
        ):
            _image_cache.pop(
                product_url,
                None
            )
            return None
        return item.get(
            "image",
            ""
        )
def image_cache_set(
    product_url,
    image_url
):
    product_url = clean_text(
        product_url
    )
    if not product_url:
        return
    image_url = clean_text(
        image_url
    )
    with _image_cache_lock:
        if (
            product_url not in _image_cache
            and
            len(_image_cache)
            >= IMAGE_CACHE_MAX_ITEMS
        ):
            oldest_key = min(
                _image_cache,
                key=lambda k:
                _image_cache[k].get(
                    "timestamp",
                    0
                )
            )
            _image_cache.pop(
                oldest_key,
                None
            )
        _image_cache[product_url] = {
            "timestamp":
                time.monotonic(),
            "image":
                image_url,
        }
def image_cache_clear():
    with _image_cache_lock:
        _image_cache.clear()
# =========================================================
# DATABASE
# =========================================================
def init_db():
    conn = sqlite3.connect(
        DB_PATH
    )
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
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None
# =========================================================
# TEXT HELPERS
# =========================================================
def clean_text(value):
    value = str(
        value or ""
    )
    value = value.replace(
        "\xa0",
        " "
    )
    value = re.sub(
        r"\s+",
        " ",
        value
    )
    return value.strip()
def words(text):
    text = clean_text(
        text
    ).lower()
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
def text_contains_word(
    text,
    word
):
    if not text or not word:
        return False
    return (
        re.search(
            r"\b" +
            re.escape(
                word
            ) +
            r"\b",
            text,
            re.IGNORECASE
        )
        is not None
    )
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
_suggestion_catalog_lock = threading.Lock()
def add_suggestions_from_results(
    results
):
    if not results:
        return
    with _suggestion_catalog_lock:
        existing = {}
        for item in SUGGESTION_CATALOG:
            key = clean_text(
                item.get(
                    "query",
                    item.get(
                        "title",
                        ""
                    )
                )
            ).lower()
            if key:
                existing[key] = item
        for result in results:
            title = clean_text(
                result.get(
                    "title",
                    ""
                )
            )
            if not title:
                continue
            if is_merch(title):
                continue
            suggestion = make_suggestion_from_title(
                title
            )
            if not suggestion:
                continue
            # Obrázok uložený priamo v autocomplete katalógu.
            suggestion["image"] = clean_text(
                result.get(
                    "image",
                    ""
                )
            )
            key = clean_text(
                suggestion.get(
                    "query",
                    ""
                )
            ).lower()
            if not key:
                continue
            existing[key] = suggestion
        new_catalog = list(
            existing.values()
        )
        new_catalog = (
            new_catalog[-300:]
        )
        SUGGESTION_CATALOG.clear()
        SUGGESTION_CATALOG.extend(
            new_catalog
        )
def make_suggestion_from_title(
    title
):
    title = clean_text(
        title
    )
    if not title:
        return None
    if is_merch(title):
        return None
    parsed = normalize_query(
        title
    )
    pokemon = clean_text(
        parsed.get(
            "pokemon",
            ""
        )
    )
    suffix = clean_text(
        parsed.get(
            "suffix",
            ""
        )
    )
    card_number = clean_text(
        parsed.get(
            "card_number",
            ""
        )
    )
    set_name = clean_text(
        parsed.get(
            "set_name",
            ""
        )
    )
    parts = []
    if pokemon:
        parts.append(
            pokemon
        )
    if suffix:
        parts.append(
            suffix
        )
    if card_number:
        parts.append(
            card_number
        )
    if set_name:
        parts.append(
            set_name
        )
    query = normalize_spaces(
        " ".join(parts)
    )
    if not query:
        query = title
    if len(query) > 120:
        query = title[:120].strip()
    suggestion_type = (
        "card"
        if not parsed.get(
            "product_type"
        )
        else "product"
    )
    type_label = (
        "Karta"
        if suggestion_type == "card"
        else "Produkt"
    )
    return {
        "title":
            title,
        "query":
            query,
        "subtitle":
            "Reálna karta z CardyX",
        "type":
            suggestion_type,
        "type_label":
            type_label,
        "image":
            "",
    }
# =========================================================
# NORMALIZATION
# =========================================================
def normalize_query(query):
    original = normalize_spaces(
        query
    )
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
            q,
            count=1
        )
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
            q
        ):
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
            re.escape(
                alias.lower()
            ) +
            r"\b"
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
        known_sets = sorted(
            set(
                list(
                    SET_ALIASES.values()
                )
                +
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
                re.escape(
                    candidate.lower()
                ) +
                r"\b"
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
    pokemon = ""
    for alias, canonical in sorted(
        POKEMON_ALIASES.items(),
        key=lambda x: len(x[0]),
        reverse=True
    ):
        pattern = (
            r"\b" +
            re.escape(
                alias.lower()
            ) +
            r"\b"
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
    suffix = ""
    suffix_match = re.search(
        r"\b(vmax|vstar|ex|gx|v)\b",
        q,
        re.IGNORECASE
    )
    if suffix_match:
        suffix = (
            suffix_match.group(1)
        ).lower()
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
    q = normalize_spaces(
        q
    )
    parts = []
    if pokemon:
        parts.append(
            pokemon
        )
    if suffix:
        parts.append(
            suffix
        )
    if q:
        parts.append(
            q
        )
    if card_number:
        parts.append(
            card_number
        )
    if set_name:
        parts.append(
            set_name
        )
    if product_type:
        parts.append(
            product_type
        )
    normalized = normalize_spaces(
        " ".join(parts)
    )
    if product_type == "elite trainer box":
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
            "elite trainer box"
        )
        normalized = normalize_spaces(
            " ".join(
                normalized_parts
            )
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
# CLASSIFY
# =========================================================
def classify_query(parsed):
    if parsed.get(
        "product_type"
    ):
        return "sealed"
    if (
        parsed.get("set_name")
        and not parsed.get("pokemon")
        and not parsed.get("card_number")
    ):
        return "sealed"
    return "card"
# =========================================================
# CARD NUMBER
# =========================================================
def normalize_card_number(value):
    value = clean_text(
        value
    )
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
# =========================================================
# SET MATCH
# =========================================================
def set_matches_text(
    searchable,
    set_name
):
    searchable = clean_text(
        searchable
    ).lower()
    set_name = clean_text(
        set_name
    ).lower()
    if not searchable or not set_name:
        return False
    set_words = words(
        set_name
    )
    searchable_words = words(
        searchable
    )
    if set_words.issubset(
        searchable_words
    ):
        return True
    for alias, canonical in SET_ALIASES.items():
        if canonical.lower() == set_name:
            if alias.lower() in searchable:
                return True
    return False
# =========================================================
# CARD MATCHING
# =========================================================
def card_matches_query(
    title,
    query,
    extra_text="",
    return_reason=False,
    parsed=None
):
    title_clean = clean_text(
        title
    )
    extra_clean = clean_text(
        extra_text
    )
    searchable = normalize_spaces(
        title_clean +
        " " +
        extra_clean
    )
    if parsed is None:
        parsed = normalize_query(
            query
        )
    pokemon = clean_text(
        parsed.get(
            "pokemon",
            ""
        )
    )
    set_name = clean_text(
        parsed.get(
            "set_name",
            ""
        )
    )
    card_number = clean_text(
        parsed.get(
            "card_number",
            ""
        )
    )
    suffix = clean_text(
        parsed.get(
            "suffix",
            ""
        )
    )
    if pokemon:
        if not text_contains_word(
            searchable,
            pokemon
        ):
            if return_reason:
                return (
                    False,
                    "pokemon_not_found"
                )
            return False
    if card_number:
        normalized_searchable = re.sub(
            r"\s+",
            "",
            searchable.lower()
        )
        normalized_card_number = (
            card_number
            .replace(" ", "")
            .lower()
        )
        if normalized_card_number not in (
            normalized_searchable
        ):
            if return_reason:
                return (
                    False,
                    "card_number_not_found"
                )
            return False
    if set_name:
        if not set_matches_text(
            searchable,
            set_name
        ):
            if return_reason:
                return (
                    False,
                    "set_not_found"
                )
            return False
    if suffix:
        if not text_contains_word(
            searchable,
            suffix
        ):
            if return_reason:
                return (
                    False,
                    "suffix_not_found"
                )
            return False
    if return_reason:
        return (
            True,
            "matched"
        )
    return True
# =========================================================
# SEALED MATCH
# =========================================================
def sealed_matches_query(
    title,
    query,
    extra_text="",
    return_reason=False,
    parsed=None
):
    searchable = normalize_spaces(
        clean_text(title) +
        " " +
        clean_text(extra_text)
    ).lower()
    if parsed is None:
        parsed = normalize_query(
            query
        )
    set_name = parsed.get(
        "set_name",
        ""
    )
    product_type = parsed.get(
        "product_type",
        ""
    )
    if set_name:
        if not set_matches_text(
            searchable,
            set_name
        ):
            if return_reason:
                return (
                    False,
                    "set_not_found"
                )
            return False
    if product_type:
        if product_type == (
            "elite trainer box"
        ):
            if not (
                "elite trainer box"
                in searchable
                or
                re.search(
                    r"\betb\b",
                    searchable
                )
            ):
                if return_reason:
                    return (
                        False,
                        "etb_not_found"
                    )
                return False
        elif product_type not in searchable:
            if return_reason:
                return (
                    False,
                    "product_type_not_found"
                )
            return False
    if (
        product_type ==
        "elite trainer box"
    ):
        if re.search(
            r"\b(case|10x|12x|6x)\b",
            searchable
        ):
            if return_reason:
                return (
                    False,
                    "bulk_product"
                )
            return False
    if return_reason:
        return (
            True,
            "matched"
        )
    return True
# =========================================================
# PRICE
# =========================================================
def parse_price(text):
    if not text:
        return None
    text = clean_text(
        text
    )
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
            value = (
                match.group(1)
                .replace(
                    " ",
                    ""
                )
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
                return float(
                    value
                )
            except Exception:
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
            value = (
                match.group(1)
                .replace(
                    ",",
                    "."
                )
            )
            try:
                czk = float(
                    value
                )
                return (
                    czk /
                    CZK_PER_EUR
                )
            except Exception:
                pass
    return None
# =========================================================
# MERCH FILTER
# =========================================================
MERCH_BLACKLIST = [
    "plush",
    "plyš",
    "peluche",
    "figúrka",
    "figurka",
    "figure",
    "figurine",
    "vinyl figure",
    "statue",
    "funko",
    "funko pop",
    "pop!",
    "pop vinyl",
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
    "slúchadlá",
    "sluchatka",
    "headphones",
    "earphones",
    "hračka",
    "hracka",
    "toy",
    "toys",
    "lampa",
    "lamp",
    "fľaša",
    "flasa",
    "bottle",
    "peňaženka",
    "penezenka",
    "wallet",
    "puzdro",
    "pouzdro",
    "phone case",
    "mobile case",
    "čepice",
    "cepice",
    "cap",
    "deka",
    "blanket",
    "polštář",
    "polstar",
    "vankúš",
    "vankus",
]
def is_merch(
    title,
    extra_text=""
):
    searchable = normalize_spaces(
        clean_text(title) +
        " " +
        clean_text(extra_text)
    ).lower()
    for item in MERCH_BLACKLIST:
        if item in searchable:
            return True
    return False
# =========================================================
# HTTP
# =========================================================
def fetch(
    url,
    timeout=SEARCH_TIMEOUT
):
    start = time.monotonic()
    debug = {
        "url":
            url,
        "http_status":
            None,
        "elapsed_ms":
            0,
        "status":
            "unknown",
        "error":
            "",
    }
    try:
        session = get_http_session()
        response = session.get(
            url,
            timeout=timeout,
            allow_redirects=True
        )
        debug[
            "http_status"
        ] = response.status_code
        debug[
            "elapsed_ms"
        ] = round(
            (
                time.monotonic()
                -
                start
            ) * 1000
        )
        if response.status_code != 200:
            debug[
                "status"
            ] = "http_error"
            debug[
                "error"
            ] = (
                f"HTTP "
                f"{response.status_code}"
            )
            return None, debug
        debug[
            "status"
        ] = "http_ok"
        return response, debug
    except requests.Timeout:
        debug[
            "elapsed_ms"
        ] = round(
            (
                time.monotonic()
                -
                start
            ) * 1000
        )
        debug[
            "status"
        ] = "timeout"
        debug[
            "error"
        ] = "Request timeout"
        return None, debug
    except requests.RequestException as e:
        debug[
            "elapsed_ms"
        ] = round(
            (
                time.monotonic()
                -
                start
            ) * 1000
        )
        debug[
            "status"
        ] = "request_error"
        debug[
            "error"
        ] = str(e)
        return None, debug
    except Exception as e:
        debug[
            "elapsed_ms"
        ] = round(
            (
                time.monotonic()
                -
                start
            ) * 1000
        )
        debug[
            "status"
        ] = "request_error"
        debug[
            "error"
        ] = str(e)
        return None, debug
# =========================================================
# URL
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
    if href.startswith(
        "#"
    ):
        return ""
    return urllib.parse.urljoin(
        base_url,
        href
    )
# =========================================================
# CARDYX TITLE EXTRACTION
# =========================================================
def cardyx_extract_title(
    anchor
):
    title = clean_text(
        anchor.get_text(
            " ",
            strip=True
        )
    )
    if title:
        return title
    for attr in [
        "title",
        "aria-label",
    ]:
        value = clean_text(
            anchor.get(
                attr,
                ""
            )
        )
        if value:
            return value
    image = anchor.find(
        "img"
    )
    if image:
        for attr in [
            "alt",
            "title",
        ]:
            value = clean_text(
                image.get(
                    attr,
                    ""
                )
            )
            if value:
                return value
    return ""
# =========================================================
# CARDYX IMAGE EXTRACTION
# =========================================================
def cardyx_extract_image(
    anchor,
    base_url="https://www.cardyx.sk/"
):
    image = anchor.find(
        "img"
    )
    if image is None:
        current = anchor
        for _ in range(3):
            current = current.parent
            if not current:
                break
            image = current.find(
                "img"
            )
            if image is not None:
                break
    if image is None:
        return ""
    attributes = [
        "src",
        "data-src",
        "data-lazy-src",
        "data-original",
        "data-image",
        "data-image-src",
        "data-original-src",
    ]
    for attr in attributes:
        value = clean_text(
            image.get(
                attr,
                ""
            )
        )
        if not value:
            continue
        if value.startswith(
            "data:image/"
        ):
            continue
        absolute = absolute_url(
            base_url,
            value
        )
        if absolute:
            return absolute
    for attr in [
        "srcset",
        "data-srcset",
    ]:
        srcset = clean_text(
            image.get(
                attr,
                ""
            )
        )
        if not srcset:
            continue
        candidates = []
        for part in srcset.split(","):
            part = clean_text(
                part
            )
            if not part:
                continue
            pieces = part.split()
            if not pieces:
                continue
            url = pieces[0]
            width = 0
            if len(pieces) > 1:
                match = re.search(
                    r"(\d+)w",
                    pieces[1]
                )
                if match:
                    try:
                        width = int(
                            match.group(1)
                        )
                    except Exception:
                        width = 0
            absolute = absolute_url(
                base_url,
                url
            )
            if absolute:
                candidates.append(
                    (
                        width,
                        absolute
                    )
                )
        if candidates:
            candidates.sort(
                key=lambda item:
                item[0]
            )
            return candidates[-1][1]
    return ""
# =========================================================
# CARDYX PRODUCT PAGE IMAGE EXTRACTION
# =========================================================
def cardyx_extract_product_page_image(
    soup,
    product_url
):
    if soup is None:
        return ""
    for selector in [
        'meta[property="og:image"]',
        'meta[property="og:image:url"]',
        'meta[name="og:image"]',
    ]:
        meta = soup.select_one(
            selector
        )
        if meta:
            value = clean_text(
                meta.get(
                    "content",
                    ""
                )
            )
            if value and not value.startswith(
                "data:image/"
            ):
                absolute = absolute_url(
                    product_url,
                    value
                )
                if absolute:
                    return absolute
    for selector in [
        'meta[name="twitter:image"]',
        'meta[property="twitter:image"]',
    ]:
        meta = soup.select_one(
            selector
        )
        if meta:
            value = clean_text(
                meta.get(
                    "content",
                    ""
                )
            )
            if value and not value.startswith(
                "data:image/"
            ):
                absolute = absolute_url(
                    product_url,
                    value
                )
                if absolute:
                    return absolute
    attributes = [
        "src",
        "data-src",
        "data-lazy-src",
        "data-original",
        "data-image",
        "data-image-src",
        "data-original-src",
    ]
    images = soup.find_all(
        "img"
    )
    prioritized_images = []
    other_images = []
    for image in images:
        alt = clean_text(
            image.get(
                "alt",
                ""
            )
        ).lower()
        image_class = clean_text(
            image.get(
                "class",
                ""
            )
        ).lower()
        image_id = clean_text(
            image.get(
                "id",
                ""
            )
        ).lower()
        marker = (
            alt +
            " " +
            image_class +
            " " +
            image_id
        )
        if any(
            token in marker
            for token in [
                "product",
                "produkt",
                "gallery",
                "main-image",
                "main image",
                "woocommerce",
            ]
        ):
            prioritized_images.append(
                image
            )
        else:
            other_images.append(
                image
            )
    ordered_images = (
        prioritized_images +
        other_images
    )
    for image in ordered_images:
        for attr in attributes:
            value = clean_text(
                image.get(
                    attr,
                    ""
                )
            )
            if not value:
                continue
            if value.startswith(
                "data:image/"
            ):
                continue
            absolute = absolute_url(
                product_url,
                value
            )
            if absolute:
                return absolute
    for image in ordered_images:
        for attr in [
            "srcset",
            "data-srcset",
        ]:
            srcset = clean_text(
                image.get(
                    attr,
                    ""
                )
            )
            if not srcset:
                continue
            candidates = []
            for part in srcset.split(","):
                part = clean_text(
                    part
                )
                if not part:
                    continue
                pieces = part.split()
                if not pieces:
                    continue
                url = pieces[0]
                width = 0
                if len(pieces) > 1:
                    match = re.search(
                        r"(\d+)w",
                        pieces[1]
                    )
                    if match:
                        try:
                            width = int(
                                match.group(1)
                            )
                        except Exception:
                            width = 0
                absolute = absolute_url(
                    product_url,
                    url
                )
                if absolute:
                    candidates.append(
                        (
                            width,
                            absolute
                        )
                    )
            if candidates:
                candidates.sort(
                    key=lambda item:
                    item[0]
                )
                return candidates[-1][1]
    return ""
# =========================================================
# CARDYX PRODUCT PAGE IMAGE FETCH
# =========================================================
def cardyx_fetch_product_image(
    product_url
):
    product_url = clean_text(
        product_url
    )
    if not product_url:
        return ""
    cached = image_cache_get(
        product_url
    )
    if cached is not None:
        return cached
    try:
        response, debug = fetch(
            product_url,
            timeout=IMAGE_FETCH_TIMEOUT
        )
        if not response:
            image_cache_set(
                product_url,
                ""
            )
            return ""
        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )
        image_url = (
            cardyx_extract_product_page_image(
                soup,
                product_url
            )
        )
        image_cache_set(
            product_url,
            image_url
        )
        return image_url
    except Exception:
        image_cache_set(
            product_url,
            ""
        )
        return ""
# =========================================================
# CARDYX ENRICH MISSING IMAGES
# =========================================================
def cardyx_enrich_missing_images(
    results,
    debug=None
):
    if not results:
        return results
    missing = []
    for item in results:
        image = clean_text(
            item.get(
                "image",
                ""
            )
        )
        link = clean_text(
            item.get(
                "link",
                ""
            )
        )
        if image:
            continue
        if not link:
            continue
        missing.append(
            item
        )
    if not missing:
        return results
    fetched = 0
    found = 0
    try:
        with ThreadPoolExecutor(
            max_workers=IMAGE_FETCH_WORKERS
        ) as executor:
            future_map = {
                executor.submit(
                    cardyx_fetch_product_image,
                    item.get(
                        "link",
                        ""
                    )
                ):
                    item
                for item in missing
            }
            for future in as_completed(
                future_map
            ):
                item = future_map[
                    future
                ]
                fetched += 1
                try:
                    image_url = future.result()
                except Exception:
                    image_url = ""
                if image_url:
                    item[
                        "image"
                    ] = image_url
                    found += 1
                else:
                    item[
                        "image"
                    ] = ""
    except Exception:
        pass
    if debug is not None:
        debug[
            "image_fallback_candidates"
        ] = len(
            missing
        )
        debug[
            "image_fallback_fetched"
        ] = fetched
        debug[
            "image_fallback_found"
        ] = found
    return results
# =========================================================
# CARDYX PRODUCT BLOCK
# =========================================================
def cardyx_find_product_block(
    anchor
):
    current = anchor
    best_text = ""
    for level in range(1, 7):
        parent = current.parent
        if not parent:
            break
        current = parent
        text = clean_text(
            current.get_text(
                " ",
                strip=True
            )
        )
        if not text:
            continue
        if (
            "€" in text
            or
            "Kč" in text
            or
            "CZK" in text
        ):
            if len(text) < 1800:
                best_text = text
                if level >= 2:
                    break
    if best_text:
        return best_text
    if anchor.parent:
        return clean_text(
            anchor.parent.get_text(
                " ",
                strip=True
            )
        )
    return ""
# =========================================================
# CARDYX SEARCH
# =========================================================
def cardyx_search(
    query,
    return_debug=False,
    enrich_images=True,
    cache_result=True
):
    start = time.monotonic()
    # =====================================================
    # CACHE
    # =====================================================
    cached = cache_get(
        query
    )
    if cached is not None:
        cached_results = cached.get(
            "results",
            []
        )
        cached_debug = cached.get(
            "debug",
            {}
        )
        cached_debug[
            "cache"
        ] = "hit"
        cached_debug[
            "elapsed_ms"
        ] = round(
            (
                time.monotonic()
                -
                start
            ) * 1000
        )
        add_suggestions_from_results(
            cached_results
        )
        if return_debug:
            return (
                cached_results,
                cached_debug
            )
        return cached_results
    results = []
    debug = {
        "shop":
            "CardyX",
        "query":
            query,
        "url":
            "",
        "status":
            "starting",
        "http_status":
            None,
        "results":
            0,
        "links_scanned":
            0,
        "unique_links":
            0,
        "price_found":
            0,
        "merch_filtered":
            0,
        "match_filtered":
            0,
        "accepted":
            0,
        "images_found":
            0,
        "images_missing":
            0,
        "image_fallback_candidates":
            0,
        "image_fallback_fetched":
            0,
        "image_fallback_found":
            0,
        "image_fallback_skipped":
            not enrich_images,
        "elapsed_ms":
            0,
        "cache":
            "miss",
        "error":
            "",
        "sample_decisions":
            [],
    }
    try:
        # =================================================
        # SEARCH URL
        # =================================================
        url = (
            "https://www.cardyx.sk/search"
            "?q=" +
            urllib.parse.quote(
                query
            )
        )
        debug[
            "url"
        ] = url
        response, http_debug = fetch(
            url,
            timeout=SEARCH_TIMEOUT
        )
        debug[
            "http_status"
        ] = http_debug.get(
            "http_status"
        )
        if not response:
            debug[
                "status"
            ] = http_debug.get(
                "status",
                "http_error"
            )
            debug[
                "error"
            ] = http_debug.get(
                "error",
                ""
            )
            debug[
                "elapsed_ms"
            ] = round(
                (
                    time.monotonic()
                    -
                    start
                ) * 1000
            )
            return (
                results,
                debug
            ) if return_debug else results
        # =================================================
        # HTML PARSE
        # =================================================
        soup = BeautifulSoup(
            response.text,
            "html.parser"
        )
        links = soup.select(
            'a[href*="/products/"]'
        )
        debug[
            "links_scanned"
        ] = len(
            links
        )
        seen = set()
        # =================================================
        # PARSE QUERY IBA RAZ
        # =================================================
        parsed = normalize_query(
            query
        )
        kind = classify_query(
            parsed
        )
        # =================================================
        # PROCESS PRODUCTS
        # =================================================
        for anchor in links:
            href = anchor.get(
                "href"
            )
            if not href:
                continue
            href = absolute_url(
                "https://www.cardyx.sk/",
                href
            )
            if not href:
                continue
            href_key = (
                href
                .lower()
                .rstrip("/")
            )
            if href_key in seen:
                continue
            seen.add(
                href_key
            )
            title = cardyx_extract_title(
                anchor
            )
            if not title:
                if len(
                    debug[
                        "sample_decisions"
                    ]
                ) < 20:
                    debug[
                        "sample_decisions"
                    ].append({
                        "title":
                            "",
                        "decision":
                            "filtered",
                        "reason":
                            "no_title",
                    })
                continue
            # =================================================
            # IMAGE
            # =================================================
            image_url = cardyx_extract_image(
                anchor
            )
            # =================================================
            # PRODUCT BLOCK
            # =================================================
            block_text = (
                cardyx_find_product_block(
                    anchor
                )
            )
            # =================================================
            # PRICE
            # =================================================
            price = parse_price(
                block_text
            )
            if price is None:
                if anchor.parent:
                    price = parse_price(
                        anchor.parent.get_text(
                            " ",
                            strip=True
                        )
                    )
            if price is None:
                if len(
                    debug[
                        "sample_decisions"
                    ]
                ) < 20:
                    debug[
                        "sample_decisions"
                    ].append({
                        "title":
                            title,
                        "decision":
                            "filtered",
                        "reason":
                            "no_price",
                    })
                continue
            debug[
                "price_found"
            ] += 1
            # =================================================
            # MERCH
            # =================================================
            if is_merch(
                title
            ):
                debug[
                    "merch_filtered"
                ] += 1
                if len(
                    debug[
                        "sample_decisions"
                    ]
                ) < 20:
                    debug[
                        "sample_decisions"
                    ].append({
                        "title":
                            title,
                        "decision":
                            "filtered",
                        "reason":
                            "merch",
                    })
                continue
            # =================================================
            # MATCH
            # =================================================
            if kind == "card":
                matched, reason = (
                    card_matches_query(
                        title,
                        query,
                        block_text,
                        return_reason=True,
                        parsed=parsed
                    )
                )
            else:
                matched, reason = (
                    sealed_matches_query(
                        title,
                        query,
                        block_text,
                        return_reason=True,
                        parsed=parsed
                    )
                )
            if not matched:
                debug[
                    "match_filtered"
                ] += 1
                if len(
                    debug[
                        "sample_decisions"
                    ]
                ) < 20:
                    debug[
                        "sample_decisions"
                    ].append({
                        "title":
                            title,
                        "decision":
                            "filtered",
                        "reason":
                            reason,
                    })
                continue
            # =================================================
            # IMAGE COUNTER
            # =================================================
            if image_url:
                debug[
                    "images_found"
                ] += 1
            else:
                debug[
                    "images_missing"
                ] += 1
            # =================================================
            # ACCEPT
            # =================================================
            result = {
                "title":
                    title,
                "shop":
                    "CardyX",
                "country":
                    "SK",
                "condition":
                    "Nové",
                "price_eur":
                    round(
                        price,
                        2
                    ),
                "link":
                    href,
                "image":
                    image_url,
            }
            results.append(
                result
            )
            debug[
                "accepted"
            ] += 1
            if len(
                debug[
                    "sample_decisions"
                ]
            ) < 20:
                debug[
                    "sample_decisions"
                ].append({
                    "title":
                        title,
                    "price_eur":
                        round(
                            price,
                            2
                        ),
                    "image":
                        bool(
                            image_url
                        ),
                    "decision":
                        "accepted",
                    "reason":
                        reason,
                })
        debug[
            "unique_links"
        ] = len(
            seen
        )
        # =====================================================
        # DEDUPLICATE
        # =====================================================
        unique = {}
        for item in results:
            title_key = clean_text(
                item.get(
                    "title",
                    ""
                )
            ).lower()
            link_key = clean_text(
                item.get(
                    "link",
                    ""
                )
            ).lower()
            key = (
                title_key,
                link_key,
            )
            unique[key] = item
        results = list(
            unique.values()
        )
        # =====================================================
        # FALLBACK IMAGE FETCH
        # =====================================================
        # Toto sa spustí iba pri normálnom vyhľadávaní.
        #
        # AUTOCOMPLETE:
        # enrich_images=False
        #
        # NORMÁLNY SEARCH:
        # enrich_images=True
        if enrich_images:
            cardyx_enrich_missing_images(
                results,
                debug
            )
        # =====================================================
        # UPDATE IMAGE COUNTERS
        # =====================================================
        final_images_found = 0
        final_images_missing = 0
        for item in results:
            if clean_text(
                item.get(
                    "image",
                    ""
                )
            ):
                final_images_found += 1
            else:
                final_images_missing += 1
        debug[
            "images_found"
        ] = final_images_found
        debug[
            "images_missing"
        ] = final_images_missing
        # =====================================================
        # SORT BY PRICE
        # =====================================================
        results.sort(
            key=lambda item:
            float(
                item.get(
                    "price_eur",
                    999999
                )
            )
        )
        debug[
            "results"
        ] = len(
            results
        )
        debug[
            "status"
        ] = (
            "ok"
            if results
            else
            "no_results"
        )
    except Exception as e:
        debug[
            "status"
        ] = "parser_error"
        debug[
            "error"
        ] = str(e)
    debug[
        "elapsed_ms"
    ] = round(
        (
            time.monotonic()
            -
            start
        ) * 1000
    )
    # =====================================================
    # CACHE RESULT
    # =====================================================
    # AUTOCOMPLETE search bez image fallbacku nesmie
    # prepísať normálny search cache výsledkami bez obrázkov.
    if cache_result:
        cache_set(
            query,
            {
                "results":
                    results,
                "debug":
                    debug,
            }
        )
    # =====================================================
    # UPDATE REAL AUTOCOMPLETE
    # =====================================================
    add_suggestions_from_results(
        results
    )
    if return_debug:
        return (
            results,
            debug
        )
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
            results, debug = (
                cardyx_search(
                    query,
                    return_debug=True,
                    enrich_images=True,
                    cache_result=True
                )
            )
            debug[
                "elapsed_ms"
            ] = round(
                (
                    time.monotonic()
                    -
                    start
                ) * 1000
            )
            return (
                results,
                debug
            )
        return [], {
            "shop":
                name,
            "query":
                query,
            "status":
                "not_implemented",
            "results":
                0,
            "error":
                "",
            "elapsed_ms":
                round(
                    (
                        time.monotonic()
                        -
                        start
                    ) * 1000
                ),
        }
    except Exception as e:
        return [], {
            "shop":
                name,
            "query":
                query,
            "status":
                "runner_error",
            "results":
                0,
            "error":
                str(e),
            "elapsed_ms":
                round(
                    (
                        time.monotonic()
                        -
                        start
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
    # =====================================================
    # IBA CARDYX
    # =====================================================
    shop_results, debug = run_shop(
        "CardyX",
        query
    )
    results.extend(
        shop_results
    )
    diagnostics.append(
        debug
    )
    # =====================================================
    # DEDUPLICATE
    # =====================================================
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
            clean_text(
                item.get(
                    "link",
                    ""
                )
            ).lower(),
        )
        unique[key] = item
    results = list(
        unique.values()
    )
    # =====================================================
    # PRICE SORT
    # =====================================================
    results.sort(
        key=lambda item:
        float(
            item.get(
                "price_eur",
                999999
            )
        )
    )
    if return_debug:
        return (
            results,
            {
                "query":
                    query,
                "shops":
                    diagnostics,
                "total_results":
                    len(
                        results
                    ),
            }
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
    conn = None
    try:
        conn = sqlite3.connect(
            DB_PATH,
            timeout=5
        )
        now = datetime.utcnow().isoformat()
        rows = []
        for item in results:
            rows.append(
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
        conn.executemany(
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
            rows
        )
        conn.commit()
    except Exception:
        pass
    finally:
        if conn:
            conn.close()
# =========================================================
# AUTOCOMPLETE SCORE
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
# REAL CARDYX SUGGESTIONS
# =========================================================
def load_real_cardyx_suggestions(
    query
):
    cached = suggestion_cache_get(
        query
    )
    if cached is not None:
        return cached
    parsed = normalize_query(
        query
    )
    search_query = parsed.get(
        "normalized",
        ""
    )
    if not search_query:
        return []
    # =====================================================
    # KRITICKÉ ZRÝCHLENIE
    # =====================================================
    #
    # Autocomplete nepotrebuje otvárať každú produktovú
    # stránku len kvôli chýbajúcemu obrázku.
    #
    # Zoberieme iba obrázok, ktorý CardyX poskytol
    # priamo v search výsledku.
    #
    # cache_result=False zároveň zabráni tomu, aby
    # autocomplete výsledky bez fallback obrázkov
    # prepísali normálny search cache.
    # =====================================================
    results = cardyx_search(
        search_query,
        enrich_images=False,
        cache_result=False
    )
    suggestions = []
    for result in results:
        title = clean_text(
            result.get(
                "title",
                ""
            )
        )
        if not title:
            continue
        if is_merch(title):
            continue
        suggestion = make_suggestion_from_title(
            title
        )
        if suggestion:
            # Obrázok zo search výsledku CardyX.
            # Žiadny ďalší request.
            suggestion[
                "image"
            ] = clean_text(
                result.get(
                    "image",
                    ""
                )
            )
            suggestions.append(
                suggestion
            )
    unique = {}
    for item in suggestions:
        key = clean_text(
            item.get(
                "query",
                item.get(
                    "title",
                    ""
                )
            )
        ).lower()
        if key:
            unique[key] = item
    suggestions = list(
        unique.values()
    )
    suggestions.sort(
        key=lambda item:
        suggestion_score(
            item,
            query
        ),
        reverse=True
    )
    suggestions = suggestions[:20]
    suggestion_cache_set(
        query,
        suggestions
    )
    return suggestions
# =========================================================
# API SUGGESTIONS
# =========================================================
@app.get(
    "/api/suggestions"
)
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
            "suggestions":
                [],
        })
    q_lower = q.lower()
    candidates = []
    # =====================================================
    # 1. REAL CARDYX RESULTS
    # =====================================================
    try:
        real_suggestions = (
            load_real_cardyx_suggestions(
                q
            )
        )
        candidates.extend(
            real_suggestions
        )
    except Exception:
        pass
    # =====================================================
    # 2. EXISTUJÚCI KATALÓG
    # =====================================================
    with _suggestion_catalog_lock:
        catalog_snapshot = [
            item.copy()
            for item in SUGGESTION_CATALOG
        ]
    for item in catalog_snapshot:
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
            or
            q_lower in subtitle
            or
            title.startswith(
                q_lower
            )
        ):
            candidates.append(
                item
            )
    # =====================================================
    # 3. AUTOMATICKÉ ROZPOZNANIE
    # =====================================================
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
            ==
            normalized_lower
            for item in candidates
        )
        if (
            not exists
            and
            normalized_lower
            != q_lower
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
                    "image":
                        "",
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
                        "Pokémon",
                    "type":
                        "card",
                    "type_label":
                        "Karta",
                    "image":
                        "",
                })
    # =====================================================
    # SORT
    # =====================================================
    candidates.sort(
        key=lambda item:
        suggestion_score(
            item,
            q
        ),
        reverse=True
    )
    # =====================================================
    # DEDUPLICATE
    # =====================================================
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
        seen.add(
            key
        )
        output.append(
            item
        )
        if len(output) >= 8:
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
@app.get(
    "/api/parse"
)
def api_parse():
    q = request.args.get(
        "q",
        ""
    )
    parsed = normalize_query(
        q
    )
    parsed[
        "type"
    ] = classify_query(
        parsed
    )
    return jsonify(
        parsed
    )
# =========================================================
# API SEARCH
# =========================================================
@app.get(
    "/api/search"
)
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
                "Chýba vyhľadávanie.",
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
        info[
            "subtitle"
        ] = (
            "Set: " +
            parsed[
                "set_name"
            ]
        )
    elif parsed.get(
        "pokemon"
    ):
        info[
            "subtitle"
        ] = (
            "Pokémon: " +
            parsed[
                "pokemon"
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
        "czk_per_eur":
            CZK_PER_EUR,
        "info":
            info,
        "debug":
            debug,
    })
# =========================================================
# API DEBUG SEARCH
# =========================================================
@app.get(
    "/api/debug/search"
)
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
                "Chýba vyhľadávanie.",
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
        "query":
            original_query,
        "normalized_query":
            normalized_query,
        "parsed":
            parsed,
        "debug":
            debug,
        "results":
            results,
    })
# =========================================================
# CACHE DEBUG / CLEAR
# =========================================================
@app.get(
    "/api/debug/cache"
)
def api_debug_cache():
    with _search_cache_lock:
        now = time.monotonic()
        active = 0
        expired = 0
        for item in _search_cache.values():
            age = (
                now -
                item.get(
                    "timestamp",
                    0
                )
            )
            if age <= CACHE_TTL:
                active += 1
            else:
                expired += 1
    with _suggestion_cache_lock:
        suggestion_items = len(
            _suggestion_cache
        )
    with _suggestion_catalog_lock:
        catalog_items = len(
            SUGGESTION_CATALOG
        )
    with _image_cache_lock:
        image_items = len(
            _image_cache
        )
        image_active = 0
        image_expired = 0
        for item in _image_cache.values():
            age = (
                now -
                item.get(
                    "timestamp",
                    0
                )
            )
            if age <= IMAGE_CACHE_TTL:
                image_active += 1
            else:
                image_expired += 1
    return jsonify({
        "status":
            "ok",
        "cache_ttl_seconds":
            CACHE_TTL,
        "cache_max_items":
            CACHE_MAX_ITEMS,
        "cache_items":
            len(
                _search_cache
            ),
        "active":
            active,
        "expired":
            expired,
        "suggestion_cache_ttl_seconds":
            SUGGESTION_CACHE_TTL,
        "suggestion_cache_items":
            suggestion_items,
        "suggestion_catalog_items":
            catalog_items,
        "image_cache_ttl_seconds":
            IMAGE_CACHE_TTL,
        "image_cache_max_items":
            IMAGE_CACHE_MAX_ITEMS,
        "image_cache_items":
            image_items,
        "image_cache_active":
            image_active,
        "image_cache_expired":
            image_expired,
    })
@app.get(
    "/api/debug/cache/clear"
)
def api_debug_cache_clear():
    cache_clear()
    suggestion_cache_clear()
    image_cache_clear()
    with _suggestion_catalog_lock:
        SUGGESTION_CATALOG.clear()
    return jsonify({
        "status":
            "ok",
        "message":
            "Cache, image cache a autocomplete katalóg boli vymazané.",
    })
# =========================================================
# HEALTH
# =========================================================
@app.get(
    "/health"
)
def health():
    index_path = find_index()
    with _suggestion_catalog_lock:
        suggestion_count = len(
            SUGGESTION_CATALOG
        )
    with _image_cache_lock:
        image_cache_count = len(
            _image_cache
        )
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
            bool(
                index_path
            ),
        "index_path":
            index_path,
        "active_shops":
            [
                "CardyX"
            ],
        "cache_ttl":
            CACHE_TTL,
        "cache_max_items":
            CACHE_MAX_ITEMS,
        "suggestion_cache_ttl":
            SUGGESTION_CACHE_TTL,
        "suggestion_catalog_items":
            suggestion_count,
        "image_cache_ttl":
            IMAGE_CACHE_TTL,
        "image_cache_items":
            image_cache_count,
        "image_fetch_workers":
            IMAGE_FETCH_WORKERS,
        "image_fetch_timeout":
            IMAGE_FETCH_TIMEOUT,
        "search_timeout":
            SEARCH_TIMEOUT,
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
