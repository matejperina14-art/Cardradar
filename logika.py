"""
CARD RADAR – logika.py
Čistá logika bez internetu a databázy:
  - rozpoznanie hľadania (Pokémon, set, číslo karty, typ produktu)
  - či názov produktu sedí na hľadanie
  - filter merchu a príslušenstva
  - jazyk, sklad, cena, počet boosterov, spájanie rovnakých produktov

KEĎ VYJDE NOVÝ SET:
  1. pridaj ho navrch do NOVE_SETY
  2. ak má skratku (napr. me06), pridaj ju do SET_ALIASES
"""

import re
import unicodedata
from functools import lru_cache

# 1 € = x Kč. Denne sa aktualizuje z Európskej centrálnej banky (obchody.py).
# Hodnota tu je len záloha pri štarte, kým sa nenačíta kurz z databázy / ECB.
KURZ = {"CZK": 24.4618}
KURZ_INFO = {"date": "", "source": "default"}   # dátum kurzu ECB (napr. 2026-10-06)


def czk_to_eur(czk):
    """Kč -> € podľa aktuálneho kurzu."""
    return czk / KURZ["CZK"] if czk else None


# =========================================================
# TEXT
# =========================================================

def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


@lru_cache(maxsize=50000)
def _fold(text):
    t = unicodedata.normalize("NFKD", text.lower())
    if t.isascii():
        return t
    return "".join(c for c in t if not unicodedata.combining(c))


def fold(text):
    """Malé písmená bez diakritiky (Pokémon -> pokemon). Výsledky si pamätá (rýchle)."""
    return _fold(str(text or ""))


def fold_words(text):
    return set(re.findall(r"[a-z0-9]+", fold(text)))


def has_word(text, word):
    if not text or not word:
        return False
    return re.search(r"\b" + re.escape(word) + r"\b", text, re.I) is not None


def _stem(w):
    """celebrations -> celebration (množné číslo v názvoch setov)"""
    return w[:-1] if len(w) > 4 and w.endswith("s") else w


# Slová, ktoré nič nehovoria o konkrétnom produkte
GENERIC_WORDS = {
    "pokemon", "tcg", "booster", "boosters", "box", "boxy", "display", "elite", "trainer",
    "etb", "bundle", "pack", "packs", "blister", "tin", "tins", "mini", "collection",
    "premium", "kolekcia", "kolekce", "set", "edicia", "edice", "the", "and", "of",
    "en", "eng", "english", "anglicky", "anglicka", "anglicke", "card", "cards", "karty",
    "game", "hra", "balicek", "balicky", "sealed",
}


# =========================================================
# SETY A POKÉMONI
# =========================================================

# Úvodná stránka – NAJNOVŠÍ HORE
NOVE_SETY = [
    {"name": "Delta Reign", "query": "delta reign"},
    {"name": "30th Celebration", "query": "30th celebration"},
    {"name": "Pitch Black", "query": "pitch black"},
    {"name": "Chaos Rising", "query": "chaos rising"},
    {"name": "Perfect Order", "query": "perfect order"},
    {"name": "Ascended Heroes", "query": "ascended heroes"},
    {"name": "Phantasmal Flames", "query": "phantasmal flames"},
    {"name": "Mega Evolution", "query": "mega evolution"},
    {"name": "Black Bolt", "query": "black bolt"},
    {"name": "White Flare", "query": "white flare"},
    {"name": "Destined Rivals", "query": "destined rivals"},
    {"name": "Journey Together", "query": "journey together"},
    {"name": "Prismatic Evolutions", "query": "prismatic evolutions"},
]

# skratka / iný názov -> oficiálny názov setu
SET_ALIASES = {
    "sv1": "scarlet violet base", "sv2": "paldea evolved", "sv3": "obsidian flames",
    "sv4": "paradox rift", "sv5": "temporal forces", "sv6": "twilight masquerade",
    "sv7": "stellar crown", "sv8": "surging sparks", "sv8a": "terastal festival",
    "sv9": "journey together", "sv9a": "destined rivals", "sv10": "destined rivals",
    "sv10.5": "black bolt white flare", "sv11": "black bolt white flare",
    "me01": "mega evolution", "me1": "mega evolution",
    "me02": "phantasmal flames", "me2": "phantasmal flames",
    "me2.5": "ascended heroes", "me 2.5": "ascended heroes",
    "me03": "perfect order", "me3": "perfect order",
    "me04": "chaos rising", "me4": "chaos rising",
    "me05": "pitch black", "me5": "pitch black",
    "151": "pokemon 151", "pokemon151": "pokemon 151", "pokemon 151": "pokemon 151",
    "prismatic": "prismatic evolutions", "prismatic evo": "prismatic evolutions",
    "surging": "surging sparks", "sparks": "surging sparks",
    "destined": "destined rivals", "journey": "journey together",
    "terastal": "terastal festival", "phantasmal": "phantasmal flames",
    "pitch": "pitch black", "chaos": "chaos rising",
    "mega brave": "mega evolution mega brave", "mega evolution": "mega evolution",
    "30th celebrations": "30th celebration",
    "30th anniversary celebration": "30th celebration",
    "30th anniversary celebrations": "30th celebration",
}

# Spoločné vydanie dvoch setov: stačí, ak názov produktu obsahuje jeden z nich
# (napr. „Black Bolt ETB“ sedí na hľadanie „sv10.5“).
SET_PARTS = {
    "black bolt white flare": ("black bolt", "white flare"),
}

KNOWN_SETS = sorted(
    set(SET_ALIASES.values()) | {
        "surging sparks", "pokemon 151", "prismatic evolutions", "terastal festival",
        "destined rivals", "journey together", "twilight masquerade", "stellar crown",
        "temporal forces", "obsidian flames", "mega evolution", "phantasmal flames",
        "ascended heroes", "perfect order", "black bolt", "white flare", "delta reign",
        "30th celebration", "paldean fates", "shrouded fable", "paldea evolved",
        "pitch black", "chaos rising",
    },
    key=len, reverse=True,
)

# Séria (nie set): „Mega Evolution – Pitch Black“ = set Pitch Black
SERIE = {"mega evolution"}

POKEMON_ALIASES = {
    "pikachu": "Pikachu", "pika": "Pikachu", "charizard": "Charizard", "char": "Charizard",
    "umbreon": "Umbreon", "eevee": "Eevee", "mew": "Mew", "mewtwo": "Mewtwo",
    "gengar": "Gengar", "lucario": "Lucario", "greninja": "Greninja", "rayquaza": "Rayquaza",
    "gardevoir": "Gardevoir", "dragonite": "Dragonite", "gyarados": "Gyarados",
    "blastoise": "Blastoise", "venusaur": "Venusaur", "lugia": "Lugia", "ho-oh": "Ho-Oh",
    "hooh": "Ho-Oh", "arceus": "Arceus", "dialga": "Dialga", "palkia": "Palkia",
    "zekrom": "Zekrom", "reshiram": "Reshiram", "celebi": "Celebi", "jolteon": "Jolteon",
    "vaporeon": "Vaporeon", "flareon": "Flareon", "espeon": "Espeon", "sylveon": "Sylveon",
    "leafeon": "Leafeon", "glaceon": "Glaceon",
}

# Poradie je dôležité: najprv dlhšie / presnejšie názvy, jednoslovné až na konci
PRODUCT_PATTERNS = [
    ("elite trainer box", r"\belite\s+trainer(?:\s+box)?\b"),
    ("elite trainer box", r"\betb\b"),
    ("booster box", r"\bbooster\s*(?:box|display)\b"),
    ("booster bundle", r"\bbooster\s*bundle\b"),
    ("booster bundle", r"\bbundle\b"),                     # „bundle“ = booster bundle
    ("collection box", r"\bcollection\s+box\b"),
    ("premium collection", r"\bpremium\s+collection\b"),
    ("blister", r"\bblister(?:\s+pack)?\b"),
    ("tin", r"\btins?\b"),
    ("booster box", r"\bdisplay\b"),                        # „display“ = booster box
    ("collection", r"\bcollection\b|\bkolekci\w*|\bkolekce\b"),
    ("booster", r"\bboosters?\b"),
]


def _compile(pairs):
    return [(re.compile(r"\b" + re.escape(a.lower()) + r"\b"), c)
            for a, c in sorted(pairs, key=lambda x: len(x[0]), reverse=True)]


_SETS_RE = _compile(SET_ALIASES.items())
_KNOWN_SETS_RE = _compile((s, s) for s in KNOWN_SETS)
_POKEMON_RE = _compile(POKEMON_ALIASES.items())
_PRODUCT_RE = [(c, re.compile(p)) for c, p in PRODUCT_PATTERNS]
_SUFFIX_RE = re.compile(r"\b(vmax|vstar|ex|gx|v)\b", re.I)
_CARDNUM_RE = re.compile(r"\b(\d{1,4})\s*/\s*(\d{1,4})\b")


def _extract(q, candidates):
    for pattern, canonical in candidates:
        if pattern.search(q):
            return canonical, pattern.sub(" ", q)
    return "", q


# =========================================================
# ROZPOZNANIE HĽADANIA
# =========================================================

def _normalize(query):
    original = clean_text(query)
    out = {"original": original, "normalized": "", "pokemon": "", "set_name": "",
           "product_name": "", "product_type": "", "card_number": "", "suffix": "",
           "set_code": "", "code_number": ""}
    if not original:
        return out
    q = original.lower()

    # kód karty „PBL 084“, „MEW 200“, „30C 128“ (ako ho píšu obchody, napr. Gengar.cz)
    m = _code_query_match(original)
    if m:
        code, num = m.group(1).lower(), int(m.group(2))
        out["set_code"], out["code_number"] = code, str(num)
        q = q[:m.start()] + " " + q[m.end():]

    m = _CARDNUM_RE.search(q)
    if m:
        out["card_number"] = f"{int(m.group(1))}/{int(m.group(2))}"   # 004/102 = 4/102
        q = _CARDNUM_RE.sub(" ", q, count=1)

    for canonical, pattern in _PRODUCT_RE:
        if pattern.search(q):
            out["product_type"] = out["product_name"] = canonical
            q = pattern.sub(" ", q)
            break

    # najprv celé názvy setov („surging sparks“), až potom skratky („sv8“)
    set_name, q = _extract(q, _KNOWN_SETS_RE)
    if not set_name:
        set_name, q = _extract(q, _SETS_RE)
    if not set_name and out["set_code"]:
        set_name = SET_CODES[out["set_code"]]
    for w in set_name.split():
        q = re.sub(r"\b" + re.escape(w) + r"\b", " ", q)
    out["set_name"] = set_name

    out["pokemon"], q = _extract(q, _POKEMON_RE)

    m = _SUFFIX_RE.search(q)
    if m:
        out["suffix"] = m.group(1).lower()
        q = _SUFFIX_RE.sub(" ", q)

    q = clean_text(re.sub(r"\bpok[eé]mon\b", " ", q, flags=re.I))

    code = f'{out["set_code"].upper()} {int(out["code_number"]):03d}' if out["set_code"] else ""
    if code:   # „pbl 084“ – set je daný kódom, do textu ho nepíšeme (ostane rozpoznateľný aj pri ďalšom čítaní)
        parts = [out["pokemon"], out["suffix"], q, code, out["product_type"]]
    elif out["product_type"] == "elite trainer box":
        parts = [set_name, out["pokemon"], out["suffix"], out["card_number"], "elite trainer box"]
    else:
        parts = [out["pokemon"], out["suffix"], q, out["card_number"], set_name, out["product_type"]]
    out["normalized"] = clean_text(" ".join(p for p in parts if p)) or original
    return out


@lru_cache(maxsize=5000)
def _normalize_cached(query):
    p = _normalize(query)
    if p["set_name"] in SERIE:
        rest = re.sub(r"\bmega\s+evolution\b", " ", query, flags=re.I)
        q = _normalize(rest)
        if q["set_name"] and q["set_name"] not in SERIE:
            q["original"] = p["original"]
            p = q
    return tuple(p.items())


def normalize_query(query):
    """Rozloží hľadanie na časti. Vracia nový dict (môžeš ho upravovať)."""
    return dict(_normalize_cached(clean_text(query)))


def classify_query(parsed):
    """'sealed' (ETB, boxy...) alebo 'card' (jednotlivé karty)"""
    if parsed.get("code_number"):
        return "card"
    if parsed.get("product_type"):
        return "sealed"
    if parsed.get("set_name") and not parsed.get("pokemon") and not parsed.get("card_number"):
        return "sealed"
    return "card"


# =========================================================
# ZHODA NÁZVU S HĽADANÍM
# =========================================================

# Kódy setov, ako ich obchody píšu pri kartách: „Blastoise ex (MEW 200) - NM“, „Scizor ex (30C UF 108)“.
# Platia LEN v zátvorke s číslom karty – „pre“ / „mew“ v bežnom texte sa tak nikdy nepomýli.
# Overené na gengar.cz (október 2026). Nový set = doplň jeho kód sem.
SET_CODES = {
    "svi": "scarlet violet base", "pal": "paldea evolved", "obf": "obsidian flames", "mew": "pokemon 151",
    "par": "paradox rift", "paf": "paldean fates", "tef": "temporal forces", "twm": "twilight masquerade",
    "sfa": "shrouded fable", "scr": "stellar crown", "ssp": "surging sparks", "pre": "prismatic evolutions",
    "jtg": "journey together", "dri": "destined rivals", "blk": "black bolt", "wht": "white flare",
    "meg": "mega evolution", "pfl": "phantasmal flames", "asc": "ascended heroes", "por": "perfect order",
    "cri": "chaos rising", "pbl": "pitch black", "30c": "30th celebration",
}
# kód v hľadaní: „pbl 84“, „PBL 084“, „30c 128“. „MEW“ je aj Pokémon – ako kód platí len veľkými
# písmenami alebo s trojmiestnym číslom („MEW 200“, „mew 007“), inak „mew 151“ = Pokémon Mew zo setu 151.
_CODE_Q_RE = re.compile(r"(?<![A-Za-z0-9])(" + "|".join(sorted(SET_CODES, key=len, reverse=True)) +
                        r")\s*-?\s*(\d{1,3})(?![\d/])", re.I)


def _code_query_match(text):
    for m in _CODE_Q_RE.finditer(text or ""):
        code = m.group(1).lower()
        if code in POKEMON_ALIASES and not (m.group(1).isupper() or len(m.group(2)) == 3 and m.group(2) != "151"):
            continue
        return m
    return None


def code_number_in(text, code, number, set_name=""):
    """Je v názve karta s týmto kódom a číslom? „(PBL 084)“ alebo „084/088 Pitch Black“."""
    n = int(number)
    for mm in re.finditer(r"\(\s*" + re.escape(code) + r"(?:\s+[a-z]{1,3})?\s+([a-z]{0,3})(\d{1,3})", fold(text)):
        if int(mm.group(2)) == n and not mm.group(1):
            return True
    return bool(set_name) and set_matches_text(text, set_name) and any(
        int(x.group(1)) == n for x in _CARDNUM_RE.finditer(text or ""))


# „(MEW 200)“, „(SIT TG01)“, „(30C UF 108)“, „(SWSH 016)“ – karta s kódom setu (veľké písmená)
CODE_CARD_RE = re.compile(r"\([A-Z0-9]{2,5}(?:\s+[A-Z]{1,3})?\s+[A-Z]{0,3}\d{1,3}[a-z]?\)")
_CODES_BY_SET = {}
for _code, _canonical in SET_CODES.items():
    _CODES_BY_SET.setdefault(_canonical, []).append(
        re.compile(r"\(\s*" + re.escape(_code) + r"(?:\s+[a-z]{1,3})?\s+[a-z]{0,3}\d{1,3}"))


def strip_codes(title):
    """Názov bez „(MEW 200)“ – aby kód setu MEW nebol Pokémon Mew."""
    return CODE_CARD_RE.sub(" ", title or "")


# oficiálny názov setu (bez diakritiky) -> regexy jeho skratiek; pripravené raz pri štarte
_ALIASES_BY_SET = {}
for _alias, _canonical in SET_ALIASES.items():
    _ALIASES_BY_SET.setdefault(fold(_canonical), []).append(
        re.compile(r"(?<![a-z0-9/])" + re.escape(fold(_alias)) + r"(?![a-z0-9/])"))


@lru_cache(maxsize=50000)
def set_matches_text(text, set_name):
    """Je v texte daný set? Bez diakritiky, skratky len ako celé slová, aj množné číslo."""
    s, n = fold(clean_text(text)), fold(clean_text(set_name))
    if not s or not n:
        return False
    parts = SET_PARTS.get(n)
    if parts and any(set_matches_text(text, p) for p in parts):
        return True
    nw, sw = fold_words(n), fold_words(s)
    if nw and nw <= sw:
        return True
    if any(rx.search(s) for rx in _ALIASES_BY_SET.get(n, ())):
        return True
    if any(rx.search(s) for rx in _CODES_BY_SET.get(n, ())):
        return True
    stem_n = {_stem(w) for w in nw}
    return bool(stem_n) and stem_n <= {_stem(w) for w in sw}


def quick_anchors(parsed, loose_set=False):
    """Rýchly predfilter pre katalógy: skupiny reťazcov (bez diakritiky). Názov produktu musí
    obsahovať aspoň jeden reťazec z KAŽDEJ skupiny, inak sa drahé porovnanie ani nespúšťa.
    Je to len hrubé sito – presné pravidlá (card/sealed_matches_query) idú potom."""
    groups = []
    pokemon = parsed.get("pokemon")
    if pokemon:
        groups.append((fold(pokemon).replace("-", ""), fold(pokemon)))
    set_name = fold(parsed.get("set_name") or "")
    if set_name and not (loose_set and parsed.get("card_number")):   # loose_set: set nemusí byť v názve
        names = [set_name] + [fold(p) for p in SET_PARTS.get(set_name, ())]
        alts = {n.split()[0] for n in names if n.split()}
        alts |= {fold(a) for a, c in SET_ALIASES.items() if fold(c) in names}
        alts |= {"(" + k for k, c in SET_CODES.items() if c in names}
        groups.append(tuple(alts))
    number = parsed.get("card_number")
    if number and "/" in number:
        groups.append((number.split("/")[1],))
    if parsed.get("code_number"):
        groups.append((parsed["code_number"],))
    return groups


def anchors_hit(folded_title, groups):
    return all(any(a in folded_title for a in g) for g in groups)


def _all_words(parsed):
    """Všetky slová hľadania (aj všeobecné), keď nič konkrétnejšie nie je."""
    return {w for w in fold_words(parsed.get("original", "")) - {"pokemon", "tcg", "the", "and", "of", "en"}
            if len(w) >= 3 or w.isdigit()}


def _wanted_words(parsed):
    return {w for w in fold_words(parsed.get("original", "")) - GENERIC_WORDS
            if len(w) >= 3 or w.isdigit()}


# „10x ETB“, „case“ = veľkoobchodné balenie. ALE „6x booster“, „36x booster“ je len popis obsahu
# (bundle / box) – predtým sa takéto produkty vyhadzovali z výsledkov.
_CONTENT_AFTER = r"(?!\s*-?\s*(?:booster\w*|bal[íi][čc]\w*|packs?\b|boost\w*|karet|kariet|cards?\b))"
_BULK_RE = re.compile(r"\bcase\b|(?<![\w/.,])\d{1,2}\s*x(?![a-z0-9])(?!\s*\d)" + _CONTENT_AFTER +
                      r"|\bx\s*\d{1,2}\b" + _CONTENT_AFTER, re.I)
_WANT_BULK_RE = re.compile(r"\bcase\b|(?:bundle|blister|etb|tin|trainer\s+box)\s+display|\b\d{1,2}\s*x\b", re.I)


def card_number_in(text, number):
    """Je v texte presne toto číslo karty? 4/102 = 004/102, ale 4/102 != 104/102."""
    try:
        a, b = (int(x) for x in number.split("/"))
    except (ValueError, AttributeError):
        return False
    return any(int(m.group(1)) == a and int(m.group(2)) == b for m in _CARDNUM_RE.finditer(text or ""))


def card_matches_query(title, extra_text, parsed, loose_set=False):
    """Hľadanie karty. loose_set = obchod nepíše set do názvu, stačí číslo karty."""
    title = clean_text(title)
    searchable = clean_text(title + " " + (extra_text or ""))
    pokemon, set_name = parsed.get("pokemon"), parsed.get("set_name")
    number, suffix = parsed.get("card_number"), parsed.get("suffix")

    if pokemon and not has_word(strip_codes(title), pokemon):
        return False, "pokemon_not_in_title"
    if number and not card_number_in(searchable, number):
        return False, "card_number_not_found"
    if parsed.get("code_number") and not code_number_in(searchable, parsed.get("set_code"),
                                                        parsed["code_number"], set_name):
        return False, "card_code_not_found"
    if set_name and not (loose_set and number) and not set_matches_text(searchable, set_name):
        return False, "set_not_found"
    if suffix and not has_word(strip_codes(title), suffix):
        return False, "suffix_not_in_title"
    # „rare candy“: bez Pokémona, setu a čísla musia byť hľadané slová v názve
    if not (pokemon or set_name or number):
        want = _wanted_words(parsed) or _all_words(parsed)
        if not want or want - fold_words(title):
            return False, "words_not_in_title"
    return True, "matched"


# Ako obchody píšu typ produktu v názve
_PTYPE_TITLE_RE = {
    "elite trainer box": re.compile(r"elite\s+trainer(?:\s+box)?|\betb\b", re.I),
    "booster box": re.compile(r"booster\s*(?:box|display)|\bdisplay\b|boosterbox|(?-i:\bBB\b)", re.I),   # BB = Posbírej to
    "booster bundle": re.compile(r"\bbundle\b", re.I),   # niektoré obchody píšu len „Bundle“
    "collection box": re.compile(r"collection\s+box|kolekci\w*\s+box", re.I),
    "premium collection": re.compile(r"premium\s+collection|pr[ée]miov\w*\s+kolekci", re.I),
    "blister": re.compile(r"blister", re.I),
    "tin": re.compile(r"\btins?\b|plechovk\w*", re.I),
    "collection": re.compile(r"collection|kolekci\w*|kolekce", re.I),
    "booster": re.compile(r"booster|bal[íi][čc]ek|bal[íi][čc]ky", re.I),   # „151 Balíček“
}
# „Display“ booster bundlov / blistrov / ETB nie je booster box, a pod.
_PTYPE_NOT_RE = {
    "booster box": re.compile(r"bundle|blister|\btins?\b|elite\s+trainer|\betb\b|sleeved|"
                              r"build\s*(?:&|and)?\s*battle|collection", re.I),
    "booster bundle": re.compile(r"elite\s+trainer|\betb\b|booster\s*box", re.I),
    "booster": re.compile(r"\d+\s*(?:karet|kariet|kart|cards)\b", re.I),   # „Balíček pro sběratele - 100 karet“
}
_MYSTERY_RE = re.compile(r"mystery|blind\s*box|tajn[ýyá]\w*|p[řr]ekvapen\w*", re.I)


def sealed_matches_query(title, extra_text, parsed):
    """Hľadanie ETB, boxov, bundlov..."""
    title = clean_text(title)
    extra_text = extra_text or ""
    set_name, ptype = parsed.get("set_name"), parsed.get("product_type")
    original = parsed.get("original", "") or ""

    if set_name and not set_matches_text(title, set_name):
        return False, "set_not_in_title"
    # typ produktu len z NÁZVU (text dlaždice môže obsahovať iné produkty, menu...)
    if ptype:
        rx = _PTYPE_TITLE_RE.get(ptype)
        if rx is not None:
            if not rx.search(title):
                return False, "product_type_not_found"
        elif ptype not in title.lower():
            return False, "product_type_not_found"
        bad = _PTYPE_NOT_RE.get(ptype)
        if bad is not None and bad.search(title):
            return False, "other_product_type"
    # mystery box / balíček má neznámy obsah – medzi ETB / boxy / boostery nepatrí (len pri hľadaní „mystery“)
    if ptype and _MYSTERY_RE.search(title) and not _MYSTERY_RE.search(original):
        return False, "mystery"
    if ptype == "elite trainer box" and re.search(r"\b(case|10x|12x|6x)\b", title, re.I):
        return False, "bulk_product"
    # set, ktorý nepoznáme: ostatné hľadané slová musia byť v názve
    if not set_name and _wanted_words(parsed) - fold_words(title + " " + extra_text):
        return False, "words_not_found"
    if parsed.get("pokemon") and not has_word(strip_codes(title), parsed["pokemon"]):
        return False, "pokemon_not_in_title"
    if not _WANT_BULK_RE.search(original):
        if _BULK_RE.search(title):
            return False, "bulk_product"
        if ptype == "booster bundle" and re.search(r"\bdisplay\b", title, re.I):
            return False, "bulk_product"
    return True, "matched"


# =========================================================
# CENA
# =========================================================

_NUM = (r"(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?(?!\d)"   # 1.099,00
        r"|\d{1,3}(?:,\d{3})+\.\d{1,2}(?!\d)"            # 1,099.00
        r"|\d{1,3}(?:[ ]\d{3})+(?:[.,]\d{1,2})?"         # 1 099,00
        r"|\d{1,8}(?:[.,]\d{1,2})?)")
_CZK = r"(?:,-|,–|\.-|-)?\s*(?:Kč|Kc|CZK)(?![a-z])"          # 1 299,- Kč
_EX_VAT = re.compile(r"(?:€\s*" + _NUM + r"|" + _NUM + r"\s*(?:€|" + _CZK + r"))\s*(?:bez\s+DPH|excl\.?\s*VAT)", re.I)
# Sumy, ktoré nie sú cenou produktu: „Ušetríte 10 €“, „doprava od 3,90 €“, „(0,15 € / ks)“
_NOISE_RE = re.compile(
    r"(?:u[šs]etr[íi]te|u[šs]et[řr][íi]te|[úu]spora|you\s+save|\bsave\b|doprava(?:\s+zdarma)?(?:\s+od)?"
    r"|po[šs]tovn[ée](?:\s+od)?|zdarma\s+od|nad)\s*:?\s*-?\s*(?:€\s*" + _NUM + r"|" + _NUM + r"\s*(?:€|" + _CZK + r"))"
    r"|" + _NUM + r"\s*(?:€|" + _CZK + r")\s*/\s*(?:ks|kus|pack|booster|bal\w*)", re.I)
_EUR_RES = [re.compile(r"€\s*" + _NUM), re.compile(_NUM + r"\s*€")]
_CZK_RES = [re.compile(_NUM + r"\s*" + _CZK, re.I), re.compile(r"CZK\s*" + _NUM, re.I)]


def to_float(value):
    value = str(value).replace(" ", "")
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", value):   # 1.099 = tisíc
        value = value.replace(".", "")
    if "," in value and "." in value:
        if value.rfind(",") > value.rfind("."):
            value = value.replace(".", "").replace(",", ".")
        else:
            value = value.replace(",", "")
    else:
        value = value.replace(",", ".")
    try:
        return float(value)
    except ValueError:
        return None


# Čísla, ktoré nie sú cena a mohli by sa „zlepiť“ s cenou vedľa nich:
# „PSA 10 536 Kč“ -> 10 536 Kč, „Mew ex 88 530 Kč“ -> 88 530 Kč, „Kód: 12345“...
_NOT_PRICE_RE = re.compile(
    r"\b(?:psa|cgc|bgs|sgc|tag|ace|grade[d]?)\s*\d{1,2}(?:[.,]5)?\b"
    r"|\b\d{1,4}\s*/\s*\d{1,4}\b|#\s?\d+"
    r"|\b(?:k[óo]d|code|ean|sku|katalogov\w*\s+[čc][íi]slo|[čc]\.)\s*:?\s*[\w-]*\d[\w-]*"
    r"|\b\d+\s*(?:ks|kus\w*|pcs|x)\b|\(\s*\d+\s*\)", re.I)


def _strip_title(text, title):
    """Z textu dlaždice odstráni názov produktu (čísla v názve nie sú cena)."""
    title = clean_text(title)
    if title and len(title) >= 4:
        text = re.sub(re.escape(title), " ", text, flags=re.I)
    return text


def parse_price_raw(text, title=""):
    """(suma, 'EUR' | 'CZK') alebo (None, ''). Ceny 'bez DPH' a úspory sa ignorujú.
    title = názov produktu; odstráni sa z textu, aby sa číslo z názvu nezlepilo s cenou."""
    text = _strip_title(clean_text(text), title)
    text = _NOISE_RE.sub(" ", _EX_VAT.sub(" ", text))
    text = _NOT_PRICE_RE.sub(" | ", text)   # oddeľovač, nie medzera – nič sa nespojí
    if not text.strip(" |"):
        return None, ""
    # Kde je € – pred číslom („€210“, Beardex) alebo za ním („49,90 €“)? Rozhoduje zápis v tejto
    # dlaždici: „135 €210“ = 210 € (€ je prilepené k 210), „49,90 € 59,90 €“ = 49,90 €.
    prefix = len(re.findall(r"€\d", text))
    suffix = len(re.findall(r"\d\s?€(?!\s?\d)", text))
    eur_res = _EUR_RES if prefix > suffix else _EUR_RES[::-1] if suffix > prefix else None
    if eur_res:
        for rx in eur_res:
            m = rx.search(text)
            if m:
                v = to_float(m.group(1))
                return (v, "EUR") if v else (None, "")
    # nerozhodné: prvá cena v texte. „0 Kč“ (Gengar: nevydaný produkt) = bez ceny, nehľadá sa ďalšie číslo
    for group in ((_EUR_RES, "EUR"), (_CZK_RES, "CZK")):
        best = None
        for rx in group[0]:
            m = rx.search(text)
            if m and (best is None or m.start() < best.start()):
                best = m
        if best is not None:
            v = to_float(best.group(1))
            return (v, group[1]) if v else (None, "")
    return None, ""


# Rozumné hranice ceny v € – čo je mimo, je takmer isto zle prečítané
_GRADED_RE = re.compile(r"\b(?:psa|cgc|bgs|sgc|graded|ohodnocen\w*|gradovan\w*)\b", re.I)
_SEALED_TYPE_RE = re.compile(r"booster|bundle|elite\s+trainer|\betb\b|collection|kolekci|\btins?\b|"
                             r"blister|display|deck|\bbox\b|chest|bal[íi][čc]|(?-i:\bBB\b)", re.I)


def price_plausible(title, eur):
    """False = cena je nezmyselná pre tento typ produktu (chyba čítania)."""
    if not eur or eur < 0.1:
        return False
    if is_combo(title) or _BULK_RE.search(title or ""):
        return eur <= 20000
    if _SEALED_TYPE_RE.search(title or ""):
        return eur <= 6000
    if _GRADED_RE.search(title or ""):
        return eur <= 15000
    return eur <= 3000   # jednotlivá karta bez gradingu


def parse_price(text, title=""):
    """Cena v EUR (Kč sa prepočíta aktuálnym kurzom)."""
    value, cur = parse_price_raw(text, title)
    if value is None:
        return None
    return czk_to_eur(value) if cur == "CZK" else value


# =========================================================
# JAZYK PRODUKTU
# =========================================================

# celé slová (bez ohľadu na veľkosť) a skratky (len VEĽKÝMI, aby „de“ nebola nemčina)
_LANG_DEFS = [
    ("JP", r"japon\w*|japan\w*|japonsk\w*", r"JP|JPN|JAP"),
    ("KR", r"k[óo]rej\w*|korean\w*", r"KR|KOR"),
    ("TW", r"traditional\s+chinese|t-?chinese|tradičn\w*\s+[čc][íi]n\w*|taiwan\w*", r"TW|T-?CN"),
    ("CN", r"[čc][ií]nsk\w*|[čc][ií]n[šs]t\w*|chinese|simplified\s+chinese|s-?chinese", r"CN|CHN|S-?CN"),
    ("ID", r"indon[ée]z\w*|indonesian\w*", r"IDN|INDO"),
    ("TH", r"thajsk\w*|thai", r"TH|THA"),
    ("DE", r"nem[ec]ck\w*|n[ěe]meck\w*|german\w*|deutsch\w*", r"DE|GER|DEU"),
    ("FR", r"franc[úu]zsk\w*|francouzsk\w*|french|fran[çc]ais\w*", r"FR|FRA"),
    ("IT", r"talian\w*|italsk\w*|italian\w*|italiano", r"IT|ITA"),
    ("ES", r"[šs]paniel\w*|[šs]pan[ěe]l\w*|spanish|espa[ñn]ol\w*", r"ES|ESP|SPA"),
    ("PT", r"portugal\w*|portugues\w*", r"PT|POR"),
    ("NL", r"holandsk\w*|nizozemsk\w*|dutch|nederlands\w*", r"NL|NLD"),
    ("PL", r"po[ľl]sk\w*|polish|polski", r"PL|POL"),
    ("EN", r"anglick\w*|english|angli[čc]tin\w*", r"EN|ENG|UK"),
]
_LANG_PATTERNS = [
    (code, re.compile(r"(?<!\w)(?:" + w + r")(?!\w)", re.I),
     re.compile(r"(?<![A-Za-z0-9])(?:" + c + r")(?![A-Za-z0-9])"))
    for code, w, c in _LANG_DEFS
]
ASIAN_LANGS = {"JP", "KR", "CN", "TW", "ID", "TH"}
FOREIGN_QUERY_RE = re.compile(r"japon|japan|jpn|k[óo]rej|korean|[čc][ií]nsk|chinese|indon|thai|thajsk", re.I)


@lru_cache(maxsize=20000)
def detect_language(title):
    """'JP', 'EN', 'DE'... alebo '' ak nie je uvedený."""
    title = title or ""
    for code, words_re, codes_re in _LANG_PATTERNS:
        if words_re.search(title) or codes_re.search(title):
            return code
    return ""


def query_language(q):
    lang = detect_language(q)
    return lang if lang and lang != "EN" else ""


# =========================================================
# FILTER MERCHU
# Slová sa hľadajú ako začiatok slova („plyš“ chytí plyšák, plyšová...).
#  1. príslušenstvo        – vždy preč
#  2. MERCH_HARD (oblečenie, hrnčeky, plyšáky...) – vždy preč
#  3. MERCH_SOFT (figúrky, odznaky...) – preč, iba ak to nie je TCG kolekcia
# =========================================================

ACCESSORY_PATTERNS = [
    r"sleeves?", r"obal\w*", r"album\w*", r"binder\w*", r"toploader\w*",
    r"playmat\w*", r"podlo[žz]k\w*", r"deck\s*box\w*", r"deckbox\w*",
    r"puzdr\w*", r"pouzdr\w*", r"stojan\w*", r"portfoli\w*", r"one\s*touch",
    r"card\s+holder\w*", r"magnetic\s+holder\w*", r"penny\s+sleeves?", r"r[áa]m[čc]ek\w*",
    r"akryl\w*", r"acrylic", r"ochrann\w*\s+box\w*", r"protector\w*",
    r"magnetick\w*\s+box\w*", r"box\s+na\s+ulo[žz]\w*",
    # kocky, žetóny, držiaky, krabičky a všetko „na karty“
    r"kock[ayu]\w*", r"kocky", r"kostk\w*", r"dice", r"d\d{1,2}",
    r"dr[žz]i?[áa]k\w*", r"dr[žz]iak\w*", r"holder\w*", r"stands?",
    r"token\w*", r"[žz]et[óo]n\w*", r"damage\s+counter\w*", r"counters", r"marker\w*", r"ukazovate[ľl]\w*",
    r"krabi[čc]k\w*\s+na\s+\w+", r"(?:na|pro|for)\s+(?:karty|kartičky|kartičk\w*|cards?)",
    r"storage\w*", r"organiz\w*", r"divider\w*", r"rozde[ľl]ova[čc]\w*", r"p[řr]ed[ěe]l\w*",
    r"card\s*saver\w*", r"semi\s*rigid\w*", r"graded\s+(?:card\s+)?(?:case|slab)\s+(?:holder|protector)",
    r"slab\s+(?:case|holder|stand)\w*", r"pr[áa]zdn\w*\s+slab\w*",
    # prázdne krabice, kódy, nepravé karty – nie sú to produkty s kartami
    r"pr[áa]zdn\w*", r"empty", r"bez\s+(?:booster\w*|bal[íi][čc]\w*|kar[ite]\w*|obsahu)",
    r"(?:only\s+)?box\s+only", r"len\s+(?:krabic\w*|box)", r"jen\s+(?:krabic\w*|box)",
    r"code\s*cards?", r"online\s+(?:code|k[óo]d\w*)", r"ptcgl\s+code\w*",
    r"proxy\w*", r"replik\w*", r"fake", r"custom\s+cards?", r"fan\s*-?made",
    # súčiastky z balení predávané samostatne: „ETB - Plastová Mince“ (29 Kč), „sada energií“
    r"(?:plastov|kovov|metal|acryl|akryl)\w*\s+(?:minc\w*|coin\w*)", r"minc[ea]", r"mincí", r"coin\b(?!\s*(?:set|collection|box|tin|gift))",
    r"sada\s+energi\w*", r"energy\s+(?:set|pack)\b", r"bal[íi][čc]ek\s+energi\w*",
    # hry, súťaže, losovania, live otváranie – cena nie je cena produktu (napr. „ETB – hra“ za 60 €)
    r"zahra[ťt]\w*", r"zahraj\w*", r"pr[íi][ďd]\s+si", r"hra[ťt]", r"hra\s+o", r"hra\s+na", r"\(hra\)", r"[-–—]\s*hra", r"(?:pok[eé]mon\s+)?minihr\w*", r"s[úu]ťa[žz]\w*", r"sout[ěe][žz]\w*",
    r"losovan\w*", r"losov[áa]n\w*", r"tombol\w*", r"raffle\w*", r"giveaway\w*", r"lottery", r"loter\w*",
    r"(?:box|pack|live)\s+break\w*", r"live\s+(?:opening|otv\w*|stream\w*)", r"otv[áa]ran\w*",
    r"vstupn[ée]\w*", r"turnaj\w*", r"tournament\w*", r"ticket\w*", r"l[íi]stok\w*",
]

MERCH_HARD_PATTERNS = [
    # oblečenie
    r"tri[čc]k\w*", r"trik[oa]", r"trik[aů]", r"t-?shirt\w*", r"\w*shirt\w*", r"tee",
    r"mikin\w*", r"hoodie\w*", r"hoody", r"sweat\w*", r"pono[žz]k\w*", r"socks?",
    r"[čc]iap\w*", r"[čc]epi[cč]\w*", r"k?[šs]iltovk\w*", r"caps?", r"beanie\w*", r"hats?",
    r"py[žz]am\w*", r"pyjam\w*", r"pajam\w*", r"kost[ýy]m\w*", r"costume\w*",
    r"rukavic\w*", r"[šs]atk\w*", r"[šs][áa]l", r"[šs][áa]ly", r"scarf\w*",
    r"tepl[áa]k\w*", r"leg[íi]n\w*", r"[šs]ortk\w*", r"bund[ay]", r"jacket\w*",
    r"papu[čc]\w*", r"slippers?", r"oble[čc]en\w*", r"textil\w*", r"bunda",
    # plyšáky, hračky
    r"ply[šs]\w*", r"plush\w*", r"peluche\w*", r"hra[čc]k\w*", r"toys?", r"lego",
    r"mega\s+construx", r"stavebnic\w*", r"puzzle\w*", r"pokladni[čc]k\w*",
    r"gashapon\w*", r"tamagotchi", r"funko\w*", r"pop!", r"vinyl\w*",
    # kuchyňa, domácnosť
    r"hrn[čc]\w*", r"hrn[íi][čc]\w*", r"hrnk\w*", r"hrnek", r"termo\w*", r"mugs?",
    r"[šs][áa]lk\w*", r"[šs][áa]lek", r"poh[áa]r\w*", r"cups?", r"tumbler\w*",
    r"f[ľl]a[šs]\w*", r"lahv\w*", r"lahev", r"bottle\w*", r"lamp", r"lamp[ayu]", r"lampi[čc]k\w*",
    r"svietidl\w*", r"sv[ií]tidl\w*", r"deka", r"deky", r"blanket\w*", r"vank[úu][šs]\w*",
    r"pol[šs]t[áa][řr]\w*", r"uter[áa]k\w*", r"ru[čc]n[íi]k\w*", r"osu[šs]k\w*", r"towel\w*",
    r"oblie[čc]k\w*", r"povle[čc]\w*", r"tanier\w*", r"tal[íi][řr]\w*", r"misk[ayu]",
    r"lunch\s*box\w*", r"desiatov\w*", r"svačin\w*", r"box\s+na\s+jedlo",
    # škola, doplnky, elektronika
    r"batoh\w*", r"backpack\w*", r"ruksak\w*", r"ta[šs]k\w*", r"bags?", r"kabelk\w*",
    r"pera[čc]n[íi]k\w*", r"penál\w*", r"z[áa]pisn[íi]k\w*", r"zo[šs]it\w*", r"se[šs]it\w*",
    r"fixk\w*", r"pastel\w*", r"pero", r"pera",
    r"k[ľl][úu][čc]enk\w*", r"kl[íi][čc]enk\w*", r"keychain\w*", r"keyring\w*",
    r"pr[íi]ves\w*", r"n[áa]ram\w*", r"n[áa]hrdeln[íi]k\w*", r"[šs]perk\w*",
    r"pe[ňn]a[žz]enk\w*", r"wallet\w*", r"phone\s+case", r"mobile\s+case", r"kryt\s+na",
    r"hodink\w*", r"hodiny", r"watch", r"sl[úu]chadl\w*", r"sluch[áa]tk\w*",
    r"headphones?", r"earphones?", r"reproduktor\w*", r"powerbank\w*",
    r"plag[áa]t\w*", r"poster\w*", r"sticker\w*", r"n[áa]lepk\w*", r"samolep\w*", r"tetov\w*",
    r"knih\w*", r"kniha", r"books?", r"komiks\w*", r"manga", r"omal\w*", r"encyklop\w*",
    r"nintendo", r"videohr\w*", r"switch",
    # jedlo
    r"[čc]okol[áa]d\w*", r"cukrovink\w*", r"bonbon\w*", r"candy", r"l[íi]zank\w*",
    r"[žz]uva[čc]k\w*", r"ramune", r"limon[áa]d\w*",
]

MERCH_SOFT_PATTERNS = [
    r"krabi[čc]k\w*", r"coin\w*", r"minc\w*", r"card\s+box\w*",
    r"fig[úu]r\w*", r"figur\w*", r"figure\w*", r"statue\w*", r"so[šs]k\w*",
    r"odznak\w*", r"badge\w*", r"pins?", r"bro[žz]\w*", r"mystery", r"blind\s*box\w*",
]


def _words_re(patterns):
    return re.compile(r"(?<!\w)(?:" + "|".join(patterns) + r")(?!\w)", re.I)


ACCESSORY_RE = _words_re(ACCESSORY_PATTERNS)
MERCH_HARD_RE = _words_re(MERCH_HARD_PATTERNS)
MERCH_SOFT_RE = _words_re(MERCH_SOFT_PATTERNS)

# Hľadanie PRÍSLUŠENSTVA („pikachu sleeves“, „binder“, „toploader“): vtedy sa príslušenstvo
# nevyhadzuje, ale naopak hľadá. Bez takého slova v hľadaní ostáva filter ako predtým.
ACCESSORY_QUERY_RE = _words_re([
    r"sleeves?", r"obal\w*", r"album\w*", r"binder\w*", r"toploader\w*", r"playmat\w*",
    r"podlo[žz]k\w*", r"deck\s*box\w*", r"deckbox\w*", r"portfoli\w*", r"one\s*touch",
    r"penny\s+sleeves?", r"card\s*saver\w*", r"semi\s*rigid\w*", r"puzdr\w*", r"pouzdr\w*",
])
_ACC_STEM = {"obaly": "obal", "obalu": "obal", "albumy": "album", "albumu": "album"}


def is_accessory_query(q):
    return bool(ACCESSORY_QUERY_RE.search(q or ""))


@lru_cache(maxsize=20000)
def is_accessory(title):
    """Príslušenstvo na karty (sleeves, album...), nie oblečenie / hračky / prázdne krabice."""
    t = clean_text(title)
    return bool(ACCESSORY_QUERY_RE.search(t)) and not MERCH_HARD_RE.search(t) \
        and not re.search(r"pr[áa]zdn|empty|proxy|fake|replik", t, re.I)


def accessory_matches_query(title, parsed):
    """Všetky hľadané slová (aj „sleeves“) musia byť v názve; jednotné / množné číslo je jedno."""
    stem = lambda w: _ACC_STEM.get(w, _stem(w))
    want = {stem(w) for w in _all_words(parsed)}
    have = {stem(w) for w in fold_words(title)}
    if not want or want - have:
        return False, "words_not_in_title"
    return True, "matched"

# Znaky TCG produktu (karta / sealed)
TCG_MARKER_RE = re.compile(
    r"booster|elite\s+trainer|\betb\b|collection|kolekci|blister|\btins?\b|\btcg\b"
    r"|battle\s+deck|theme\s+deck|build\s*(?:&|and)?\s*battle|display|battle\s+academy|league\s+battle\s+deck"
    r"|\b\d{1,3}\s*/\s*\d{1,3}\b|\bcards\b|miscellaneous"
    r"|(?-i:\([A-Z0-9]{2,5}(?:\s+[A-Z]{1,3})?\s+[A-Z]{0,3}\d{1,3}[a-z]?\))", re.I)   # „(MEW 200)“

# Znaky jednotlivej karty
CARD_MARKER_RE = re.compile(
    r"\b\d{1,3}\s*/\s*\d{1,3}\b|#\s?\d{1,3}\b|\b(?:sv|swsh|sm|xy|me|bw|svp|sve)\s?-?\d"
    r"|\b(?:ex|gx|v|vmax|vstar|lv\.?\s?x|break|prime|legend|tag\s+team)\b"
    r"|holo|reverse|full\s*art|rare|promo|illustration|secret|trainer\s+gallery|alt\w*\s+art"
    r"|\bsir\b|\bir\b|\bsr\b|\bur\b|\bar\b|\bchr\b|\bshiny\b|\bkart[ay]\b|\bcard\b"
    r"|\bpsa\b|\bcgc\b|\bbgs\b|graded|\bnm\b|near\s+mint|mint", re.I)

# Skutočné TCG produkty, ktoré obsahujú „merch“ slovo (Rare Candy, Poster Collection...)
_TCG_SAFE_RE = re.compile(
    r"rare\s+candy|puzzle\s+of\s+time|poster\s+collection|binder\s+collection"
    r"|sticker\s+collection|collector'?s?\s+chest|nintendo\s+(?:black\s+star\s+)?promos?"
    r"|trick\s+or\s+trade|grey\s+felt\s+hat", re.I)
_SWITCH_RE = re.compile(r"(?<!\w)(?:energy\s+)?switch(?:\s+cart)?(?!\w)", re.I)
_CONSOLE_RE = re.compile(r"nintendo\s+switch|konzol\w*|console|oled|joy-?con|videohr\w*|video\s*game", re.I)

TCG_SET_NAMES = set(KNOWN_SETS) | {
    "scarlet violet", "scarlet & violet", "crown zenith", "silver tempest", "lost origin",
    "pokemon go", "pokémon go", "astral radiance", "brilliant stars", "fusion strike",
    "celebrations", "evolving skies", "chilling reign", "battle styles", "shining fates",
    "vivid voltage", "champion's path", "champions path", "darkness ablaze", "rebel clash",
    "sword shield", "sword & shield", "cosmic eclipse", "hidden fates", "unified minds",
    "unbroken bonds", "team up", "lost thunder", "dragon majesty", "celestial storm",
    "forbidden light", "ultra prism", "crimson invasion", "shining legends", "burning shadows",
    "guardians rising", "sun moon", "sun & moon", "evolutions", "steam siege", "fates collide",
    "generations", "breakpoint", "breakthrough", "ancient origins", "roaring skies",
    "primal clash", "phantom forces", "furious fists", "flashfire", "base set", "jungle",
    "fossil", "team rocket", "neo genesis", "gym heroes", "151", "shiny treasure",
    "vstar universe", "terastal", "night wanderer", "stellar miracle", "battle partners",
    "heat wave arena", "glory of team rocket",
}
_TCG_SET_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(s) for s in sorted(TCG_SET_NAMES, key=len, reverse=True)) + r")\b", re.I)


def _merch_clean(text):
    if not text:
        return ""
    console = _CONSOLE_RE.search(text)
    text = _TCG_SAFE_RE.sub(" ", text)
    # karta Switch / Energy Switch (nie herná konzola)
    if not console and _SWITCH_RE.search(text) and CARD_MARKER_RE.search(text):
        text = _SWITCH_RE.sub(" ", text)
    return text


@lru_cache(maxsize=20000)
def merch_reason(title, extra_text=""):
    """'' = karta / TCG produkt; inak dôvod vyradenia."""
    if CODE_CARD_RE.search(title or ""):   # „Backtrack Badge (PBL 074) - NM“ je karta, nie odznak
        m = re.search(r"proxy\w*|replik\w*|fake|custom|fan\s*-?made", title, re.I)
        return ("accessory:" + m.group(0).lower()) if m else ""
    text = clean_text(_merch_clean(title) + " " + _merch_clean(extra_text))
    m = ACCESSORY_RE.search(text)
    if m:
        return "accessory:" + m.group(0).lower()
    m = MERCH_HARD_RE.search(text)
    if m:
        return "merch:" + m.group(0).lower()
    m = MERCH_SOFT_RE.search(text)
    if m and not TCG_MARKER_RE.search(text):
        return "merch:" + m.group(0).lower()
    return ""


def is_merch(title, extra_text=""):
    return bool(merch_reason(title, extra_text))


@lru_cache(maxsize=20000)
def looks_like_tcg(title):
    """Názov vyzerá ako karta alebo TCG produkt."""
    text = clean_text(title)
    return bool(TCG_MARKER_RE.search(text) or CARD_MARKER_RE.search(text) or _TCG_SET_RE.search(text))


def is_tcg_product(title):
    return looks_like_tcg(title) and not is_merch(title)


def is_listed_product(title):
    """Čo si katalóg uloží: karty / sealed produkty a aj príslušenstvo (sleeves, albumy...).
    Do bežných výsledkov sa príslušenstvo dostane len pri hľadaní príslušenstva."""
    return is_tcg_product(title) or is_accessory(title)


# =========================================================
# SKLAD
# =========================================================

STOCK_OUT_RE = re.compile(
    r"vypredan\w*|vyprod[aá]n\w*|nie\s+je\s+skladom|nie\s+je\s+na\s+sklade"
    r"|nedostupn\w*|nen[íi]\s+skladem|nen[íi]\s+dostupn\w*|sold\s*out"
    r"|out\s+of\s+stock|ausverkauft", re.I)
STOCK_PRE_RE = re.compile(r"predobjedn\w*|p[řr]edobjedn\w*|pre-?order\w*|vorbestell\w*", re.I)
STOCK_ORDER_RE = re.compile(r"na\s+objedn[áa]vku|do\s+\d+\s+dn[íi]|na\s+dotaz|u\s+dodavatele", re.I)
STOCK_IN_RE = re.compile(
    r"skladom|skladem|na\s+sklad[eě]|in\s+stock|dostupn[ée]|k\s+odberu"
    r"|k\s+dispozici|ihne[dď]|expedujeme|odes[ií]l[aá]me", re.I)
COMING_RE = re.compile(r"o[čc]ak[áa]vame|o[čc]ek[áa]v[áa]me|pripravujeme|coming\s+soon", re.I)


def detect_stock(text):
    """'in' | 'out' | 'preorder' | 'order' | '' (nevieme)"""
    text = clean_text(text)
    if not text:
        return ""
    if STOCK_OUT_RE.search(text):
        return "out"
    if STOCK_PRE_RE.search(text):
        return "preorder"
    if STOCK_ORDER_RE.search(text):
        return "order"
    if STOCK_IN_RE.search(text):
        return "in"
    return ""


# =========================================================
# SPÁJANIE ROVNAKÝCH PRODUKTOV Z RÔZNYCH OBCHODOV
# =========================================================

GROUP_TYPES = [
    ("etb", re.compile(r"elite\s+trainer(?:\s+box)?|\betb\b", re.I)),
    ("booster box", re.compile(r"booster\s*(?:box|display)|boosterbox|(?-i:\bBB\b)", re.I)),
    ("booster bundle", re.compile(r"booster\s*bundle", re.I)),
    ("sleeved booster", re.compile(r"sleeved\s+booster", re.I)),
    ("3-pack blister", re.compile(r"3\s*-?\s*pack|three\s+pack|3\s*booster\s+blister", re.I)),
    ("checklane blister", re.compile(r"checklane|1\s*-?\s*pack\s+blister|single\s+blister", re.I)),
    ("blister", re.compile(r"blister", re.I)),
    ("mini tin", re.compile(r"mini\s+tin", re.I)),
    ("tin", re.compile(r"\btins?\b", re.I)),
    ("build battle", re.compile(r"build\s*(?:&|and)?\s*battle", re.I)),
    ("booster pack", re.compile(r"booster\s+pack|\bbooster\b|bal[íi][čc]ek", re.I)),
    ("collection", re.compile(r"collection|kolekci", re.I)),
]
_VARIANT_RES = [
    ("pc", re.compile(r"pok[eé]mon\s+center", re.I)),
    ("half", re.compile(r"\bhalf\b|polovi[čc]n", re.I)),
    ("rev", re.compile(r"reverse", re.I)),
    ("psa", re.compile(r"\b(?:psa|cgc|bgs|graded)\b", re.I)),
    ("used", re.compile(r"(?:[-–—|,(\[]\s*)(?:exc|excellent|lp|pl|mp|hp|played|poor|dmg|damaged)\s*[)\]]?\s*$", re.I)),
    # poškodené balenie sa nespája s novým (iná cena, iný produkt)
    ("dmg", re.compile(r"po[šs]kod\w*|po[šs]koz\w*|damaged|dent\w*|bez\s+f[óo]li\w*", re.I)),
]
_COMBO_TYPES = [
    re.compile(r"elite\s+trainer(?:\s+box)?|\betb\b", re.I),
    re.compile(r"booster\s*(?:box|display)", re.I),
    re.compile(r"booster\s*bundle", re.I),
    re.compile(r"blister", re.I),
    re.compile(r"\btins?\b", re.I),
    re.compile(r"collection|kolekci", re.I),
]
# Pri blistroch a tinoch rozhoduje aj Pokémon/motív – rôzne motívy sa nespájajú
_DETAIL_TYPES = {"3-pack blister", "checklane blister", "blister", "mini tin", "tin", "build battle"}
_DETAIL_SKIP = GENERIC_WORDS | {
    "checklane", "premium", "pack", "blister", "mini", "tin", "tins", "scarlet", "violet", "sword",
    "shield", "mega", "evolution", "series", "edition", "with", "build", "battle", "kit", "stadium",
    "japonsky", "japanese", "korejsky", "korean", "cinsky", "chinese", "nemecky", "german"}


@lru_cache(maxsize=20000)
def is_combo(title):
    """Viac produktov v jednom balení („Bundle + ETB“, „2x ETB“, „sada“...)."""
    t = clean_text(title)
    if sum(1 for rx in _COMBO_TYPES if rx.search(t)) >= 2:
        return True
    return bool(re.search(
        r"(?<![\w/.,])(?:[2-9]|1[0-9])\s*(?:x|ks|kusy|pcs)(?![a-z])" + _CONTENT_AFTER +
        r"|\bx\s*(?:[2-9]|1[0-9])\b" + _CONTENT_AFTER +
        r"|\b(?:bundle\s+deal|komplet\w*|set\s+of|sada)\b", t, re.I))


@lru_cache(maxsize=20000)
def group_key(title, lang=""):
    """Kľúč na spojenie rovnakého produktu z rôznych obchodov.
    None = nevieme s istotou, zobrazí sa samostatne."""
    t = clean_text(title)
    if not t or is_combo(t):
        return None
    if not is_tcg_product(t) and is_accessory(t):   # „ETB Sleeves Pitch Black“ sa nesmie spojiť s ETB
        return None
    p = normalize_query(strip_codes(t))
    lang = lang or "EN"
    variants = ",".join(v for v, rx in _VARIANT_RES if rx.search(t))
    ptype = next((name for name, rx in GROUP_TYPES if rx.search(t)), "")
    pokemon = (p.get("pokemon") or "").lower()
    set_name = p.get("set_name") or ""
    number = p.get("card_number") or ""

    if ptype and set_name and ptype != "collection":
        key = f"s|{set_name}|{ptype}|{pokemon}|{variants}|{lang}"
        if ptype in _DETAIL_TYPES:
            drop = _DETAIL_SKIP | fold_words(set_name)
            detail = sorted(w for w in fold_words(t)
                            if w not in drop and len(w) >= 3 and not w.isdigit()
                            and not re.fullmatch(r"(?:sv|me|swsh|sm|xy)\d+\w*", w))
            key += "|" + "-".join(detail)
        return key
    if number and pokemon:
        return f"c|{pokemon}|{number}|{variants}|{lang}"
    if pokemon and set_name and p.get("suffix") and not ptype:
        return f"c|{pokemon}|{p['suffix']}|{set_name}|{variants}|{lang}"
    return None


# =========================================================
# POČET BOOSTEROV (cena za booster)
# =========================================================

_PACKS_EXPLICIT_RE = re.compile(
    r"(?<![\w/.,#-])(\d{1,2})\s*(?:-|x)?\s*(?:booster\w*|bal[íi][čc]\w*|packs?\b|packungen|boost\w*)", re.I)
_PACKS_PAREN_RE = re.compile(r"booster\s*(?:box|display)\D{0,10}\((\d{1,2})\)", re.I)


@lru_cache(maxsize=20000)
def estimate_packs(title, lang=""):
    """Odhad počtu boosterov v produkte; None = nevieme."""
    t = clean_text(title).lower()
    if not t or is_combo(t):
        return None
    if not is_tcg_product(title) and is_accessory(title):   # „ETB Sleeves“ nemá boostery
        return None
    m = _PACKS_EXPLICIT_RE.search(t) or _PACKS_PAREN_RE.search(t)
    if m and 1 <= int(m.group(1)) <= 36:
        return int(m.group(1))
    if lang in ASIAN_LANGS:
        return None   # ázijské boxy majú rôzny počet (10, 20, 30...)
    if re.search(r"booster\s*(?:box|display)|boosterbox", t) or re.search(r"\bBB\b", title or ""):
        return 18 if re.search(r"\bhalf\b|polovičn", t) else 36
    if re.search(r"elite\s+trainer(?:\s+box)?|\betb\b", t):
        return 11 if re.search(r"pok[eé]mon center", t) else 9
    if re.search(r"booster\s*bundle", t):
        return 6
    if (re.search(r"sleeved\s+booster|booster\s+pack|\bbooster\b$", t)
            and not re.search(r"collection|box|tin|blister|bundle|display", t)):
        return 1
    return None


# =========================================================
# STAV KARTY (použité karty: Gengar „- NM“, „- EXC“, „- LP“, „- PL“...)
# Stav sa hľadá len na konci názvu alebo v zátvorke, aby „EX“ (Charizard EX) nebolo stavom.
# =========================================================

_CONDITIONS = [   # (vzor, kód, text na webe, skupina: "nm" = ako nová, "used" = použitá)
    (r"nm\s*/\s*m|near\s*mint|nm|mint|m", "NM", "NM – ako nová", "nm"),
    (r"exc|excellent|ex\+|výborn[ýá]|vyborn[ya]", "EXC", "EXC – mierne použitá", "used"),
    (r"lp|light(?:ly)?\s*played|slightly\s*played|sp", "LP", "LP – mierne použitá", "used"),
    (r"pl|played|mp|moderately\s*played|gd|good|použit[áa]|pouzit[aá]|hran[áa]", "PL", "PL – použitá", "used"),
    (r"hp|heavily\s*played|poor|dmg|damaged|poškoden[áa]|po[šs]kozen[áa]", "HP", "HP – silno použitá", "used"),
]
_COND_RES = [(re.compile(r"(?:[-–—|,]\s*|\(\s*|\[\s*)(?:" + rx + r")\s*[)\]]?\s*$", re.I), code, label, grp)
             for rx, code, label, grp in _CONDITIONS]


@lru_cache(maxsize=20000)
def card_condition(title):
    """(kód, text, skupina) stavu karty z konca názvu, alebo ("", "", "") – stav neuvedený (nová)."""
    t = clean_text(title)
    if _GRADED_RE.search(t):   # PSA / CGC – stav určuje známka, nie NM/LP
        return "", "", ""
    for rx, code, label, grp in _COND_RES:
        if rx.search(t):
            return code, label, grp
    return "", "", ""


def make_result(shop, title, price, link, image="", stock="", price_czk=None):
    """Jedna ponuka vo výsledkoch hľadania (rovnaký tvar pre všetky typy obchodov).
    price_czk = pôvodná cena v Kč (CZ obchody) – € sa z nej vždy počíta aktuálnym kurzom."""
    lang = detect_language(title)
    packs = estimate_packs(title, lang)
    cond_code, cond_label, cond_group = card_condition(title)
    r = {
        "title": title, "shop": shop["name"], "country": shop["country"],
        "condition": cond_label or "Nové", "cond": cond_code, "cond_group": cond_group or "new",
        "language": lang, "price_eur": round(price, 2),
        "price_czk": round(price_czk) if price_czk else None,
        "link": link, "image": image or "", "stock": stock or "",
        "packs": packs, "price_per_pack": None,
        "group": group_key(title, lang),
        "kind": product_kind(title),          # 'card' | 'sealed' | 'accessory' (záložky na webe)
        "shipping_eur": None, "total_eur": None,   # doplní obchody.add_shipping, ak obchod má poštovné
    }
    return reprice(r)


def product_kind(title):
    """'sealed' = balík (ETB, box, bundle, blister, tin, kolekcia...), 'accessory' = sleeves, album..., inak 'card'."""
    if not is_tcg_product(title) and is_accessory(title):
        return "accessory"
    return "sealed" if _SEALED_TYPE_RE.search(title or "") else "card"


def shipping_eur(shop, price_eur):
    """Poštovné v € podľa SHOPS[...]["shipping"]; None = nevieme.
    Tvar: {"price": 3.9, "free_from": 60, "currency": "EUR"}  (currency "CZK" pre české obchody)"""
    cfg = (shop or {}).get("shipping")
    if not cfg or cfg.get("price") is None or not price_eur:
        return None
    rate = KURZ["CZK"] if str(cfg.get("currency", "EUR")).upper() == "CZK" else 1.0
    free_from = cfg.get("free_from")
    if free_from is not None and price_eur >= free_from / rate:
        return 0.0
    return round(cfg["price"] / rate, 2)


def reprice(r):
    """Prepočíta € z Kč aktuálnym kurzom (aj pri výsledkoch z cache a katalógu)."""
    if r.get("price_czk"):
        r["price_eur"] = round(czk_to_eur(r["price_czk"]), 2)
    packs = r.get("packs")
    r["price_per_pack"] = round(r["price_eur"] / packs, 2) if packs and packs > 1 and r.get("price_eur") else None
    return r
