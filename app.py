import os
import re
import sqlite3
import unicodedata
from datetime import datetime
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, request, send_file

app = Flask(__name__)

VERSION = "5.6"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# =========================================================
# INDEX.HTML
# =========================================================

def find_index_file():
    candidates = [
        os.path.join(BASE_DIR, "index.html"),
        os.path.join(BASE_DIR, "Templates", "index.html"),
        os.path.join(BASE_DIR, "templates", "index.html"),
        os.path.join(BASE_DIR, "Index.html"),
        os.path.join(BASE_DIR, "Templates", "Index.html"),
        os.path.join(BASE_DIR, "templates", "Index.html"),
    ]

    for path in candidates:
        if os.path.isfile(path):
            return path

    for root, dirs, files in os.walk(BASE_DIR):
        dirs[:] = [
            d for d in dirs
            if d not in {
                ".git",
                "__pycache__",
                ".venv",
                "venv",
                "node_modules"
            }
        ]

        for filename in files:
            if filename.lower() == "index.html":
                return os.path.join(root, filename)

    return None


INDEX_FILE = find_index_file()

DB_FILE = os.path.join(
    BASE_DIR,
    "cardradar.db"
)

# =========================================================
# HTTP
# =========================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/18.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "sk-SK,sk;q=0.9,en;q=0.8,cs;q=0.7",
    "Accept": (
        "text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    ),
}

TIMEOUT = 15

CZK_PER_EUR = 24.4618


# =========================================================
# DATABASE
# =========================================================

def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query TEXT NOT NULL,
            shop TEXT NOT NULL,
            title TEXT NOT NULL,
            price_eur REAL NOT NULL,
            checked_at TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()


init_db()


# =========================================================
# NORMALIZÁCIA TEXTU
# =========================================================

def normalize(text):
    if not text:
        return ""

    text = str(text)

    text = unicodedata.normalize(
        "NFKD",
        text
    )

    text = "".join(
        c for c in text
        if not unicodedata.combining(c)
    )

    text = text.lower()

    text = text.replace("–", "-")
    text = text.replace("—", "-")
    text = text.replace("’", "'")

    text = re.sub(
        r"\s+",
        " ",
        text
    )

    return text.strip()


def words(text):
    return re.findall(
        r"[a-z0-9]+",
        normalize(text)
    )


# =========================================================
# SETY POKÉMON
# =========================================================

SET_ALIASES = {

    # Scarlet & Violet
    "sv1": "scarlet violet",
    "sv2": "paldea evolved",
    "sv3": "obsidian flames",
    "sv3.5": "151",
    "sv4": "paradox rift",
    "sv4.5": "paldean fates",
    "sv5": "temporal forces",
    "sv6": "twilight masquerade",
    "sv6.5": "shrouded fable",
    "sv7": "stellar crown",
    "sv8": "surging sparks",
    "sv8.5": "prismatic evolutions",
    "sv9": "journey together",
    "sv10": "destined rivals",
    "sv10.5": "black bolt white flare",

    # Mega Evolution
    "me1": "mega evolution",
    "me01": "mega evolution",
    "me2": "phantasmal flames",
    "me02": "phantasmal flames",

    # Older / common
    "151": "pokemon 151",
    "pokemon151": "pokemon 151",
    "sv 151": "pokemon 151",

    # Popular shorthand
    "surging": "surging sparks",
    "sparks": "surging sparks",
    "phantasmal": "phantasmal flames",
    "flames": "phantasmal flames",
    "prismatic": "prismatic evolutions",
}


# =========================================================
# PRODUKTOVÉ ALIASY
# =========================================================

PRODUCT_ALIASES = {

    # ETB
    "etb": "elite trainer box",
    "elite": "elite trainer box",
    "trainer box": "elite trainer box",
    "elite trainer": "elite trainer box",

    # Booster Box
    "bb": "booster box",
    "boosterbox": "booster box",
    "booster boxx": "booster box",

    # Booster Bundle
    "boosterbundle": "booster bundle",
    "bundle": "booster bundle",

    # UPC
    "upc": "ultra premium collection",
    "ultra premium": "ultra premium collection",

    # Collection
    "collectionbox": "collection box",

    # Blister
    "blisterpack": "blister pack",
}


# =========================================================
# POKÉMON NÁZVY
# =========================================================

POKEMON_ALIASES = {

    "charizard": "Charizard",
    "charmander": "Charmander",
    "charmeleon": "Charmeleon",

    "pikachu": "Pikachu",
    "raichu": "Raichu",

    "eevee": "Eevee",
    "umbreon": "Umbreon",
    "espeon": "Espeon",
    "sylveon": "Sylveon",
    "vaporeon": "Vaporeon",
    "jolteon": "Jolteon",
    "flareon": "Flareon",
    "glaceon": "Glaceon",
    "leafeon": "Leafeon",

    "mew": "Mew",
    "mewtwo": "Mewtwo",

    "gengar": "Gengar",

    "greninja": "Greninja",

    "lucario": "Lucario",

    "rayquaza": "Rayquaza",

    "lugia": "Lugia",

    "ho-oh": "Ho-Oh",
    "hooh": "Ho-Oh",

    "dialga": "Dialga",
    "palkia": "Palkia",
    "giratina": "Giratina",

    "arceus": "Arceus",

    "zekrom": "Zekrom",
    "reshiram": "Reshiram",

    "celebi": "Celebi",

    "snorlax": "Snorlax",

    "greninja": "Greninja",

    "blastoise": "Blastoise",
    "venusaur": "Venusaur",

    "alakazam": "Alakazam",

    "dragonite": "Dragonite",

    "magikarp": "Magikarp",

    "gyarados": "Gyarados",

    "tyranitar": "Tyranitar",

    "mimikyu": "Mimikyu",

    "gardevoir": "Gardevoir",

    "machamp": "Machamp",

    "garchomp": "Garchomp",

    "zoroark": "Zoroark",

    "ceruledge": "Ceruledge",

    "dragapult": "Dragapult",
}


# =========================================================
# NORMALIZOVANÝ DOTAZ
# =========================================================

def normalize_query(query):

    original = str(query or "").strip()

    if not original:
        return {
            "original": "",
            "normalized": "",
            "display": "",
            "type": "card",
            "set": "",
            "product": "",
            "card_number": None,
        }

    q = normalize(original)

    # -----------------------------------------------------
    # ODSTRÁNENIE NADBYTOČNÝCH ZNAKOV
    # -----------------------------------------------------

    q = re.sub(
        r"[\(\)\[\]\{\},;]+",
        " ",
        q
    )

    q = re.sub(
        r"\s+",
        " ",
        q
    ).strip()

    # -----------------------------------------------------
    # NORMALIZÁCIA ČÍSLA KARTY
    # -----------------------------------------------------

    card_number = None

    m = re.search(
        r"\b(\d{1,3})\s*/\s*(\d{1,3})\b",
        q
    )

    if m:
        a = int(m.group(1))
        b = int(m.group(2))

        card_number = f"{a:03d}/{b:03d}"

        q = re.sub(
            r"\b\d{1,3}\s*/\s*\d{1,3}\b",
            "",
            q
        )

    # -----------------------------------------------------
    # DETEKCIA TYPU PRODUKTU
    # -----------------------------------------------------

    product_type = "card"

    if re.search(
        r"\b(etb|elite trainer box|elite trainer|trainer box)\b",
        q
    ):
        product_type = "etb"

    elif re.search(
        r"\b(booster box|boosterbox|bb)\b",
        q
    ):
        product_type = "booster_box"

    elif re.search(
        r"\b(booster bundle|bundle)\b",
        q
    ):
        product_type = "booster_bundle"

    elif re.search(
        r"\b(upc|ultra premium collection)\b",
        q
    ):
        product_type = "upc"

    elif re.search(
        r"\b(blister|blister pack)\b",
        q
    ):
        product_type = "blister"

    elif re.search(
        r"\b(tin)\b",
        q
    ):
        product_type = "tin"

    elif re.search(
        r"\b(collection box|collection)\b",
        q
    ):
        product_type = "collection"

    # -----------------------------------------------------
    # SET ALIASY
    # -----------------------------------------------------

    set_name = ""

    # Najprv dlhšie aliasy
    aliases = sorted(
        SET_ALIASES.items(),
        key=lambda x: len(x[0]),
        reverse=True
    )

    for alias, replacement in aliases:

        pattern = (
            r"(?<![a-z0-9])"
            + re.escape(alias)
            + r"(?![a-z0-9])"
        )

        if re.search(pattern, q):

            set_name = replacement

            q = re.sub(
                pattern,
                " ",
                q
            )

            break

    # -----------------------------------------------------
    # PRODUKTOVÉ ALIASY
    # -----------------------------------------------------

    product_name = ""

    aliases = sorted(
        PRODUCT_ALIASES.items(),
        key=lambda x: len(x[0]),
        reverse=True
    )

    for alias, replacement in aliases:

        pattern = (
            r"(?<![a-z0-9])"
            + re.escape(alias)
            + r"(?![a-z0-9])"
        )

        if re.search(pattern, q):

            product_name = replacement

            q = re.sub(
                pattern,
                " ",
                q
            )

            break

    # -----------------------------------------------------
    # EX / VMAX / VSTAR / GX
    # -----------------------------------------------------

    special_tokens = []

    if re.search(
        r"(?<![a-z0-9])ex(?![a-z0-9])",
        q
    ):
        special_tokens.append("ex")

    if re.search(
        r"(?<![a-z0-9])vmax(?![a-z0-9])",
        q
    ):
        special_tokens.append("VMAX")

    if re.search(
        r"(?<![a-z0-9])vstar(?![a-z0-9])",
        q
    ):
        special_tokens.append("VSTAR")

    if re.search(
        r"(?<![a-z0-9])gx(?![a-z0-9])",
        q
    ):
        special_tokens.append("GX")

    # -----------------------------------------------------
    # ODSTRÁNENIE ŠPECIÁLNYCH TOKENOV Z HLAVNÉHO TEXTU
    # -----------------------------------------------------

    q = re.sub(
        r"(?<![a-z0-9])(?:ex|vmax|vstar|gx)(?![a-z0-9])",
        " ",
        q
    )

    # -----------------------------------------------------
    # POKÉMON ALIASY
    # -----------------------------------------------------

    pokemon_name = ""

    for alias, proper in sorted(
        POKEMON_ALIASES.items(),
        key=lambda x: len(x[0]),
        reverse=True
    ):

        pattern = (
            r"(?<![a-z0-9])"
            + re.escape(alias)
            + r"(?![a-z0-9])"
        )

        if re.search(pattern, q):

            pokemon_name = proper

            q = re.sub(
                pattern,
                " ",
                q
            )

            break

    # -----------------------------------------------------
    # ZOSTÁVAJÚCI TEXT
    # -----------------------------------------------------

    q = re.sub(
        r"\s+",
        " ",
        q
    ).strip()

    # -----------------------------------------------------
    # AUTOMATICKÉ URČENIE SETU Z NÁZVU
    # -----------------------------------------------------

    if not set_name:

        normalized_full = normalize(original)

        if "surging sparks" in normalized_full:
            set_name = "surging sparks"

        elif "phantasmal flames" in normalized_full:
            set_name = "phantasmal flames"

        elif "prismatic evolutions" in normalized_full:
            set_name = "prismatic evolutions"

        elif "paldean fates" in normalized_full:
            set_name = "paldean fates"

        elif "pokemon 151" in normalized_full:
            set_name = "pokemon 151"

    # -----------------------------------------------------
    # ZOSTAVENIE NORMALIZOVANÉHO DOTAZU
    # -----------------------------------------------------

    parts = []

    if pokemon_name:
        parts.append(
            pokemon_name
        )

    if q:
        parts.append(
            q
        )

    if special_tokens:
        parts.extend(
            special_tokens
        )

    if card_number:
        parts.append(
            card_number
        )

    if set_name:
        parts.append(
            set_name
        )

    normalized_query = " ".join(
        parts
    ).strip()

    # -----------------------------------------------------
    # DISPLAY
    # -----------------------------------------------------

    display_parts = []

    if pokemon_name:
        display_parts.append(
            pokemon_name
        )

    if q:
        display_parts.append(
            q
        )

    if special_tokens:
        display_parts.extend(
            special_tokens
        )

    if card_number:
        display_parts.append(
            card_number
        )

    if product_name:
        display_parts.append(
            product_name
        )

    if set_name:
        display_parts.append(
            set_name
        )

    display = " ".join(
        display_parts
    ).strip()

    # -----------------------------------------------------
    # ETB ŠPECIÁLNE ZOBRAZENIE
    # -----------------------------------------------------

    if product_type == "etb":

        base = []

        if pokemon_name:
            base.append(
                pokemon_name
            )

        if q:
            base.append(
                q
            )

        if set_name:
            base.append(
                set_name
            )

        display = " ".join(
            base
        ).strip()

        if display:
            display += " Elite Trainer Box"

    elif product_type == "booster_box":

        base = []

        if pokemon_name:
            base.append(
                pokemon_name
            )

        if q:
            base.append(
                q
            )

        if set_name:
            base.append(
                set_name
            )

        display = " ".join(
            base
        ).strip()

        if display:
            display += " Booster Box"

    elif product_type == "booster_bundle":

        base = []

        if pokemon_name:
            base.append(
                pokemon_name
            )

        if q:
            base.append(
                q
            )

        if set_name:
            base.append(
                set_name
            )

        display = " ".join(
            base
        ).strip()

        if display:
            display += " Booster Bundle"

    # -----------------------------------------------------
    # FALLBACK
    # -----------------------------------------------------

    if not display:
        display = original

    if not normalized_query:
        normalized_query = normalize(
            display
        )

    return {
        "original": original,
        "normalized": normalized_query,
        "display": display,
        "type": product_type,
        "set": set_name,
        "product": product_name,
        "card_number": card_number,
    }


# =========================================================
# QUERY TYPE
# =========================================================

def classify_query(query):

    parsed = normalize_query(
        query
    )

    if parsed["type"] != "card":
        return "sealed"

    return "card"


# =========================================================
# CARD NUMBER
# =========================================================

def card_number_from_query(query):

    parsed = normalize_query(
        query
    )

    return parsed.get(
        "card_number"
    )


# =========================================================
# MERCHANDISE
# =========================================================

MERCH_WORDS = {
    "plysak",
    "plysovy",
    "hracka",
    "hracky",
    "figurka",
    "funko",
    "toy",
    "plush",
    "plushie",
    "album",
    "obal",
    "obaly",
    "sleeves",
    "sleeve",
    "binder",
    "dekoracia",
    "dekoracie",
    "tricko",
    "mikina",
    "taska",
    "batoh",
    "hrncek",
    "pohar",
    "poster",
    "poduska",
    "vankus",
    "keychain",
    "privesok",
    "stavebnica",
    "lego",
}


def candidate_is_merch(title):

    t = normalize(
        title
    )

    for bad in MERCH_WORDS:

        if re.search(
            r"\b"
            + re.escape(bad)
            + r"\b",
            t
        ):
            return True

    return False


# =========================================================
# CARD MATCHING
# =========================================================

def card_matches_query(title, query):

    if not title:
        return False

    title_n = normalize(
        title
    )

    parsed = normalize_query(
        query
    )

    search_n = normalize(
        parsed["normalized"]
    )

    if candidate_is_merch(
        title
    ):
        return False

    qwords = words(
        search_n
    )

    # EX
    if "ex" in qwords:

        if not re.search(
            r"(?<![a-z0-9])ex(?![a-z0-9])",
            title_n
        ):
            return False

    # VMAX
    if "vmax" in qwords:

        if "vmax" not in title_n:
            return False

    # VSTAR
    if "vstar" in qwords:

        if "vstar" not in title_n:
            return False

    # GX
    if "gx" in qwords:

        if not re.search(
            r"(?<![a-z0-9])gx(?![a-z0-9])",
            title_n
        ):
            return False

    # Číslo karty
    number = parsed.get(
        "card_number"
    )

    if number:

        a, b = number.split("/")

        possible = {
            f"{int(a)}/{int(b)}",
            f"{a}/{b}",
            f"{a.zfill(2)}/{b.zfill(2)}",
            f"{a.zfill(3)}/{b.zfill(3)}",
        }

        if not any(
            p in title_n
            for p in possible
        ):
            return False

    ignore = {
        "pokemon",
        "tcg",
        "card",
        "cards",
        "karte",
        "karta",
        "ex",
        "v",
        "vmax",
        "vstar",
        "gx",
        "nm",
        "near",
        "mint",
        "lp",
        "mp",
        "played",
        "english",
        "en",
    }

    important = [
        w
        for w in qwords
        if w not in ignore
        and not w.isdigit()
    ]

    for word in important:

        if len(word) <= 1:
            continue

        if word not in title_n:
            return False

    return True


# =========================================================
# SEALED MATCHING
# =========================================================

SEALED_WORDS = {
    "etb",
    "elite",
    "trainer",
    "box",
    "booster",
    "bundle",
    "tin",
    "collection",
    "premium",
    "upc",
    "display",
    "pack",
    "blister",
}


def sealed_matches_query(title, query):

    if not title:
        return False

    title_n = normalize(
        title
    )

    parsed = normalize_query(
        query
    )

    if candidate_is_merch(
        title
    ):
        return False

    qwords = words(
        parsed["normalized"]
    )

    product_type = parsed[
        "type"
    ]

    # ETB
    if product_type == "etb":

        if not (
            "etb" in title_n
            or "elite trainer box" in title_n
            or "elite trainer" in title_n
        ):
            return False

    # BOOSTER BOX
    if product_type == "booster_box":

        if "booster box" not in title_n:
            return False

    # BOOSTER BUNDLE
    if product_type == "booster_bundle":

        if "booster bundle" not in title_n:
            return False

    # UPC
    if product_type == "upc":

        if not (
            "ultra premium collection" in title_n
            or "upc" in title_n
        ):
            return False

    # BLISTER
    if product_type == "blister":

        if "blister" not in title_n:
            return False

    # TIN
    if product_type == "tin":

        if not re.search(
            r"\btin\b",
            title_n
        ):
            return False

    # COLLECTION
    if product_type == "collection":

        if "collection" not in title_n:
            return False

    # DÔLEŽITÉ SLOVÁ
    important = []

    for w in qwords:

        if w in SEALED_WORDS:
            continue

        if w in {
            "pokemon",
            "tcg"
        }:
            continue

        important.append(
            w
        )

    for word in important:

        if len(word) <= 1:
            continue

        if word not in title_n:
            return False

    return True


# =========================================================
# PRICE
# =========================================================

def parse_number(value):

    if value is None:
        return None

    value = str(
        value
    ).strip()

    value = value.replace(
        "\xa0",
        " "
    )

    value = value.replace(
        "€",
        ""
    )

    value = value.replace(
        "EUR",
        ""
    )

    value = value.replace(
        "Kč",
        ""
    )

    value = value.replace(
        "CZK",
        ""
    )

    value = value.strip()

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

    elif "," in value:

        value = value.replace(
            ".",
            ""
        )

        value = value.replace(
            ",",
            "."
        )

    else:

        if value.count(".") > 1:

            parts = value.split(".")

            value = (
                "".join(
                    parts[:-1]
                )
                + "."
                + parts[-1]
            )

    m = re.search(
        r"\d+(?:\.\d+)?",
        value
    )

    if not m:
        return None

    try:
        return float(
            m.group(0)
        )

    except Exception:
        return None


def czk_to_eur(value):

    if value is None:
        return None

    return float(
        value
    ) / CZK_PER_EUR


# =========================================================
# HTTP GET
# =========================================================

def get(url):

    try:

        response = requests.get(
            url,
            headers=HEADERS,
            timeout=TIMEOUT
        )

        if response.status_code != 200:
            return None

        return response

    except Exception:

        return None


# =========================================================
# CARDYX
# =========================================================

def extract_cardyx_price_from_product(url):

    r = get(
        url
    )

    if not r:
        return None

    soup = BeautifulSoup(
        r.text,
        "html.parser"
    )

    for script in soup.select(
        'script[type="application/ld+json"]'
    ):

        raw = script.get_text(
            strip=True
        )

        if not raw:
            continue

        m = re.search(
            r'"price"\s*:\s*"?(\\?[\d.,]+)',
            raw
        )

        if m:

            value = parse_number(
                m.group(1)
            )

            if (
                value is not None
                and 0.5 <= value <= 100000
            ):
                return value

    meta = soup.select_one(
        'meta[property="product:price:amount"]'
    )

    if meta:

        value = parse_number(
            meta.get(
                "content"
            )
        )

        if (
            value is not None
            and 0.5 <= value <= 100000
        ):
            return value

    selectors = [
        ".price-item--sale",
        ".price-item--regular",
        ".price__regular",
        ".price",
    ]

    for selector in selectors:

        for node in soup.select(
            selector
        ):

            value = parse_number(
                node.get_text(
                    " ",
                    strip=True
                )
            )

            if (
                value is not None
                and 0.5 <= value <= 100000
            ):
                return value

    return None


def search_cardyx_cards(query):

    results = []

    parsed = normalize_query(
        query
    )

    search_query = parsed[
        "normalized"
    ]

    url = (
        "https://www.cardyx.sk/search"
        "?type=product&q="
        + quote(
            search_query
        )
    )

    r = get(
        url
    )

    if not r:
        return results

    soup = BeautifulSoup(
        r.text,
        "html.parser"
    )

    links = soup.select(
        "a[href*='/products/']"
    )

    seen = set()

    for a in links:

        href = a.get(
            "href"
        )

        if not href:
            continue

        product_url = urljoin(
            "https://www.cardyx.sk",
            href
        )

        if product_url in seen:
            continue

        seen.add(
            product_url
        )

        title = a.get_text(
            " ",
            strip=True
        )

        if not title:
            continue

        parent = a.parent

        if parent:

            parent_text = parent.get_text(
                " ",
                strip=True
            )

            if len(parent_text) > len(title):
                title = parent_text

        if not card_matches_query(
            title,
            query
        ):
            continue

        price = extract_cardyx_price_from_product(
            product_url
        )

        if price is None:
            continue

        results.append({
            "shop": "CardyX",
            "country": "SK",
            "language": "EN",
            "condition": "NM",
            "in_stock": True,
            "title": title,
            "price_eur": round(
                price,
                2
            ),
            "link": product_url,
        })

    return results


def search_cardyx_sealed(query):

    results = []

    parsed = normalize_query(
        query
    )

    search_query = parsed[
        "normalized"
    ]

    url = (
        "https://www.cardyx.sk/search"
        "?type=product&q="
        + quote(
            search_query
        )
    )

    r = get(
        url
    )

    if not r:
        return results

    soup = BeautifulSoup(
        r.text,
        "html.parser"
    )

    links = soup.select(
        "a[href*='/products/']"
    )

    seen = set()

    for a in links:

        href = a.get(
            "href"
        )

        if not href:
            continue

        product_url = urljoin(
            "https://www.cardyx.sk",
            href
        )

        if product_url in seen:
            continue

        seen.add(
            product_url
        )

        title = a.get_text(
            " ",
            strip=True
        )

        if not title:
            continue

        parent = a.parent

        if parent:

            parent_text = parent.get_text(
                " ",
                strip=True
            )

            if len(parent_text) > len(title):
                title = parent_text

        if not sealed_matches_query(
            title,
            query
        ):
            continue

        price = extract_cardyx_price_from_product(
            product_url
        )

        if price is None:
            continue

        results.append({
            "shop": "CardyX",
            "country": "SK",
            "language": "EN",
            "condition": "Sealed",
            "in_stock": True,
            "title": title,
            "price_eur": round(
                price,
                2
            ),
            "link": product_url,
        })

    return results


# =========================================================
# OSTATNÉ OBCHODY
# =========================================================

SHOPS = [

    {
        "name": "Veselý Drak",
        "country": "CZ",
        "base": "https://www.vesely-drak.cz",
        "search": (
            "https://www.vesely-drak.cz/"
            "vyhledavani/?q={q}"
        ),
    },

    {
        "name": "iHRYsko",
        "country": "SK",
        "base": "https://www.ihrysko.sk",
        "search": (
            "https://www.ihrysko.sk/"
            "vysledky-vyhladavania/?q={q}"
        ),
    },

    {
        "name": "Černý Rytíř",
        "country": "CZ",
        "base": "https://www.cernyrytir.cz",
        "search": (
            "https://www.cernyrytir.cz/"
            "index.php3?akce=3&stranka=1&search={q}"
        ),
    },
]


def find_price_near_link(
    anchor,
    country
):

    nodes = []

    parent = anchor

    for _ in range(5):

        if parent is None:
            break

        nodes.append(
            parent
        )

        parent = parent.parent

    for node in nodes:

        text = node.get_text(
            " ",
            strip=True
        )

        m = re.search(
            r"(\d[\d\s.,]*)\s*(?:€|EUR)",
            text,
            re.I
        )

        if m:

            value = parse_number(
                m.group(1)
            )

            if (
                value is not None
                and 0.5 <= value <= 100000
            ):
                return value

        if country == "CZ":

            m = re.search(
                r"(\d[\d\s.,]*)\s*(?:Kč|CZK)",
                text,
                re.I
            )

            if m:

                value = parse_number(
                    m.group(1)
                )

                if (
                    value is not None
                    and 1 <= value <= 1000000
                ):
                    return czk_to_eur(
                        value
                    )

    return None


def generic_search(
    shop,
    query,
    mode
):

    results = []

    parsed = normalize_query(
        query
    )

    search_query = parsed[
        "normalized"
    ]

    url = shop[
        "search"
    ].format(
        q=quote(
            search_query
        )
    )

    r = get(
        url
    )

    if not r:
        return results

    soup = BeautifulSoup(
        r.text,
        "html.parser"
    )

    links = soup.find_all(
        "a",
        href=True
    )

    seen = set()

    for a in links:

        href = a.get(
            "href"
        )

        if not href:
            continue

        title = a.get_text(
            " ",
            strip=True
        )

        if not title:
            continue

        if len(title) < 3:
            continue

        if len(title) > 300:
            continue

        if mode == "card":

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

        link = urljoin(
            shop["base"],
            href
        )

        if link in seen:
            continue

        seen.add(
            link
        )

        price = find_price_near_link(
            a,
            shop["country"]
        )

        if price is None:
            continue

        results.append({
            "shop": shop["name"],
            "country": shop["country"],
            "language": "EN",
            "condition": (
                "Sealed"
                if mode == "sealed"
                else "NM"
            ),
            "in_stock": True,
            "title": title,
            "price_eur": round(
                price,
                2
            ),
            "link": link,
        })

        if len(results) >= 10:
            break

    return results


# =========================================================
# HISTORY
# =========================================================

def save_history(
    query,
    result
):

    try:

        conn = db()

        conn.execute(
            """
            INSERT INTO history
            (
                query,
                shop,
                title,
                price_eur,
                checked_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                query,
                result["shop"],
                result["title"],
                result["price_eur"],
                datetime.utcnow().isoformat(),
            )
        )

        conn.commit()
        conn.close()

    except Exception:
        pass


def get_history(query):

    try:

        conn = db()

        rows = conn.execute(
            """
            SELECT
                checked_at,
                price_eur
            FROM history
            WHERE query = ?
            ORDER BY checked_at DESC
            LIMIT 10
            """,
            (
                query,
            )
        ).fetchall()

        conn.close()

        return [
            {
                "checked_at": row["checked_at"],
                "price_eur": row["price_eur"],
            }
            for row in rows
        ]

    except Exception:
        return []


# =========================================================
# SORT
# =========================================================

def sort_results(results):

    unique = {}

    for result in results:

        key = (
            normalize(
                result["title"]
            ),
            result["shop"],
            result["link"],
        )

        unique[key] = result

    results = list(
        unique.values()
    )

    results.sort(
        key=lambda x:
        x["price_eur"]
    )

    return results


# =========================================================
# API SEARCH
# =========================================================

@app.route("/api/search")
def api_search():

    original_query = request.args.get(
        "q",
        ""
    ).strip()

    if not original_query:

        return jsonify({
            "error": "Chýba vyhľadávanie."
        }), 400

    parsed = normalize_query(
        original_query
    )

    mode = classify_query(
        original_query
    )

    results = []

    # -----------------------------------------------------
    # CARDYX
    # -----------------------------------------------------

    if mode == "card":

        results.extend(
            search_cardyx_cards(
                original_query
            )
        )

    else:

        results.extend(
            search_cardyx_sealed(
                original_query
            )
        )

    # -----------------------------------------------------
    # OSTATNÉ OBCHODY
    # -----------------------------------------------------

    for shop in SHOPS:

        try:

            results.extend(
                generic_search(
                    shop,
                    original_query,
                    mode
                )
            )

        except Exception as e:

            print(
                f"{shop['name']} error:",
                e
            )

    # -----------------------------------------------------
    # FINÁLNY FILTER
    # -----------------------------------------------------

    clean = []

    for result in results:

        title = result.get(
            "title",
            ""
        )

        price = result.get(
            "price_eur"
        )

        if not title:
            continue

        if price is None:
            continue

        if price <= 0:
            continue

        if candidate_is_merch(
            title
        ):
            continue

        if mode == "card":

            if not card_matches_query(
                title,
                original_query
            ):
                continue

        else:

            if not sealed_matches_query(
                title,
                original_query
            ):
                continue

        clean.append(
            result
        )

    results = sort_results(
        clean
    )

    # -----------------------------------------------------
    # HISTORY
    # -----------------------------------------------------

    for result in results[:10]:

        save_history(
            original_query,
            result
        )

    # -----------------------------------------------------
    # INFO
    # -----------------------------------------------------

    info = {}

    if results:

        info = {
            "title": results[0]["title"],
            "subtitle": (
                f"{results[0]['shop']} • "
                f"{results[0]['condition']}"
            ),
        }

    return jsonify({
        "query": original_query,
        "normalized_query": parsed["normalized"],
        "display_query": parsed["display"],
        "type": mode,
        "product_type": parsed["type"],
        "set": parsed["set"],
        "product": parsed["product"],
        "card_number": parsed["card_number"],
        "version": VERSION,
        "czk_per_eur": CZK_PER_EUR,
        "results": results,
        "history": get_history(
            original_query
        ),
        "info": info,
    })


# =========================================================
# QUERY PREVIEW
# =========================================================

@app.route("/api/parse")
def api_parse():

    query = request.args.get(
        "q",
        ""
    ).strip()

    if not query:

        return jsonify({
            "error": "Chýba vyhľadávanie."
        }), 400

    parsed = normalize_query(
        query
    )

    return jsonify({
        "version": VERSION,
        "original": parsed["original"],
        "normalized": parsed["normalized"],
        "display": parsed["display"],
        "type": parsed["type"],
        "set": parsed["set"],
        "product": parsed["product"],
        "card_number": parsed["card_number"],
    })


# =========================================================
# HEALTH
# =========================================================

@app.route("/health")
def health():

    current_index = find_index_file()

    return jsonify({
        "service": "CardRadar",
        "status": "ok",
        "version": VERSION,
        "index_exists": bool(
            current_index
        ),
        "index_path": current_index,
        "base_dir": BASE_DIR,
    })


# =========================================================
# FRONTEND
# =========================================================

@app.route("/")
def home():

    current_index = find_index_file()

    if not current_index:

        return (
            "<h1>CardRadar</h1>"
            "<p>Chýba index.html.</p>"
            "<p>Render ho nenašiel v projekte.</p>"
        ), 500

    return send_file(
        current_index
    )


# =========================================================
# START
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
