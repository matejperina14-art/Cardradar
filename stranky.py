"""
CARD RADAR – stranky.py
  - dáta pre úvodnú stránku (/api/home): zľavy, najlacnejšie ETB/boxy/bundle, top karty, nové sety
  - podmienky, ochrana údajov, pre obchody, robots.txt
  - potvrdzovacie stránky strážcu
  - admin: /admin/test (kontrola hľadania), /admin/obchody (pridanie obchodu), /admin/katalog

Texty stránok upravuješ priamo v TERMS_HTML, PRIVACY_HTML, SHOPS_HTML nižšie.
"""

import hmac
import html
import json
import os
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta

from flask import Response, jsonify, redirect, request

import logika as L
import obchody as O

ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
OPERATOR_NAME = os.environ.get("OPERATOR_NAME", "prevádzkovateľ CardRadar")
# identifikácia podľa zákona o elektronickom obchode: sídlo / miesto podnikania, IČO, zápis v registri
# napr. „Hlavná 1, 949 01 Nitra, IČO: 12345678, zapísaný v živnostenskom registri OÚ Nitra č. 430-12345“
OPERATOR_DETAILS = os.environ.get("OPERATOR_DETAILS", "")
CONTACT_EMAIL = os.environ.get("CONTACT_EMAIL", "") or os.environ.get("SMTP_FROM", "")
ALLOW_INDEXING = os.environ.get("ALLOW_INDEXING", "0") == "1"

# Admin prihlásenie: po prvom /admin/...?key=HESLO sa uloží cookie (app.py, admin_login).
# V cookie nie je samotné heslo, len podpis odvodený z neho – zmenou ADMIN_KEY sa všetci odhlásia.
ADMIN_COOKIE = "cr_admin"


def admin_cookie_value():
    if not ADMIN_KEY:
        return ""
    return hmac.new(ADMIN_KEY.encode(), b"cardradar-admin-v1", "sha256").hexdigest()


def is_admin():
    if not ADMIN_KEY:
        return False
    cookie = request.cookies.get(ADMIN_COOKIE, "")
    if cookie and hmac.compare_digest(cookie.encode(), admin_cookie_value().encode()):
        return True
    key = request.values.get("key", "")
    return bool(key) and hmac.compare_digest(key.encode(), ADMIN_KEY.encode())


# =========================================================
# ÚVODNÁ STRÁNKA
# =========================================================

_ETB_RE = re.compile(r"elite\s+trainer(?:\s+box)?|\betb\b", re.I)
_BOX_RE = re.compile(r"booster\s*(?:box|display)|boosterbox|(?-i:\bBB\b)", re.I)
_BUNDLE_RE = re.compile(r"\bbundle\b", re.I)
_SEALED_RE = re.compile(r"booster|bundle|elite\s+trainer|\betb\b|collection|kolekci|\btins?\b|blister|"
                        r"display|deck|premium|build\s*(?:&|and)?\s*battle|\bbox\b|chest|bal[íi][čc]|"
                        r"starter|academy|\bcase\b|mystery|plechovk|\bsada\b", re.I)

_home = {"t": 0.0, "data": None}
_items = {"t": 0.0, "all": None}
_home_lock = threading.Lock()


def _latest_rows():
    now = datetime.now(timezone.utc)
    since3 = (now - timedelta(days=3)).strftime("%Y-%m-%d")
    since30 = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    conn = O.db()
    try:
        return conn.execute("""
            SELECT p.link, p.title, p.shop, p.price_eur, p.stock, p.image,
                   (SELECT MAX(q.price_eur) FROM price_daily q
                     WHERE q.link = p.link AND q.day >= ? AND q.day < p.day),
                   (SELECT MAX(q.image) FROM price_daily q WHERE q.link = p.link)
            FROM price_daily p
            JOIN (SELECT link, MAX(day) AS d FROM price_daily WHERE day >= ? GROUP BY link) r
              ON p.link = r.link AND p.day = r.d
            WHERE p.price_eur > 0 AND (p.stock IS NULL OR p.stock != 'out')
        """, (since30, since3)).fetchall()
    finally:
        conn.close()


def _uniq(items, limit, key=lambda x: x["price_eur"], reverse=False):
    out, seen = [], set()
    for it in sorted(items, key=key, reverse=reverse):
        k = it["group"] or it["link"]
        if k not in seen:
            seen.add(k)
            out.append(it)
            if len(out) >= limit:
                break
    return out


def build_home():
    deals, etb, boxes, bundles, cards, all_items = [], [], [], [], [], []
    for link, title, shop, price, stock, image, old_max, any_image in _latest_rows():
        if not L.is_tcg_product(title) or not L.price_plausible(title, price):
            continue
        lang = L.detect_language(title)
        if lang in L.ASIAN_LANGS:
            continue
        it = {"link": link, "title": title, "shop": shop, "price_eur": round(price, 2),
              "stock": stock or "", "image": image or any_image or "", "language": lang,
              "group": L.group_key(title, lang),
              "query": (O.make_suggestion(title) or {}).get("query") or title}
        all_items.append(it)
        if old_max and old_max - price >= 1 and price <= old_max * 0.97:
            deals.append(dict(it, old_price_eur=round(old_max, 2),
                              drop_pct=round((old_max - price) / old_max * 100)))
        if L.is_combo(title) or L._BULK_RE.search(title):
            continue
        if _ETB_RE.search(title):
            etb.append(it)
        elif _BOX_RE.search(title):
            boxes.append(it)
        elif _BUNDLE_RE.search(title) and not re.search(r"display", title, re.I):
            bundles.append(it)
        elif not _SEALED_RE.search(title) and price >= 10 and L.CARD_MARKER_RE.search(title):
            cards.append(it)   # len skutočné karty (číslo, rarita, PSA...), nie iné produkty

    sets = []
    for s in L.NOVE_SETY:
        s = dict(s)
        mine = [i for i in all_items if L.set_matches_text(i["title"], s["query"])]
        s_etb = [i for i in mine if _ETB_RE.search(i["title"])]
        s_box = [i for i in mine if _BOX_RE.search(i["title"])]
        pic = next((i["image"] for i in s_etb + s_box + mine if i["image"].startswith("https://")), "")
        if pic:
            s["image"] = pic
        if s_etb:
            s["etb_from"] = min(i["price_eur"] for i in s_etb)
        if s_box:
            s["box_from"] = min(i["price_eur"] for i in s_box)
        s["offers"] = len(mine)
        sets.append(s)

    _items["all"], _items["t"] = all_items, time.monotonic()   # pre stránky setov
    return {
        "deals": _uniq(deals, 12, key=lambda d: d["drop_pct"], reverse=True),
        "cheap_etb": _uniq(etb, 8),
        "cheap_box": _uniq(boxes, 12),
        "cheap_bundle": _uniq(bundles, 12),
        "top_cards": _uniq(cards, 12, reverse=True),
        "popular": O.popular_queries(),
        "new_sets": sets,
        "kurz_czk": {"rate": L.KURZ["CZK"], "date": L.KURZ_INFO.get("date", "")},
    }


def _store_home(data):
    """Hotová úvodná stránka aj do databázy – ostatné procesy servera ju majú hneď, aj po reštarte."""
    _home["data"], _home["t"] = data, time.monotonic()
    try:
        O.meta_set("home_cache", json.dumps({"t": time.time(), "data": data}, ensure_ascii=False))
    except Exception:
        pass


def _rebuild_home():
    try:
        _store_home(build_home())
    except Exception as e:
        print(f"[CardRadar] Úvodná stránka: {e}", flush=True)
        _home["t"] = time.monotonic()   # neskúšať hneď znova
    finally:
        _home["building"] = False


def _rebuild_home_bg():
    with _home_lock:
        if _home.get("building"):
            return
        _home["building"] = True
    threading.Thread(target=_rebuild_home, daemon=True, name="home-rebuild").start()


def home_data():
    """Úvodná stránka okamžite: z pamäte, inak z databázy. Staršia ako 10 min sa obnoví na pozadí –
    návštevník nikdy nečaká na prepočet (predtým to trvalo niekoľko sekúnd)."""
    if _home["data"] is None:
        try:
            saved = json.loads(O.meta_get("home_cache") or "null")
        except Exception:
            saved = None
        if saved and saved.get("data"):
            _home["data"] = saved["data"]
            _home["t"] = time.monotonic() - max(0, time.time() - saved.get("t", 0))
    if _home["data"] is None:   # úplne prvý štart bez dát: raz počkáme
        with _home_lock:
            if _home["data"] is None:
                try:
                    _store_home(build_home())
                except Exception as e:
                    print(f"[CardRadar] Úvodná stránka: {e}", flush=True)
                    _home["data"], _home["t"] = {"new_sets": L.NOVE_SETY}, time.monotonic()
    elif time.monotonic() - _home["t"] > 600:
        _rebuild_home_bg()
    return _home["data"]


def home_cached():
    """Úvodná stránka, len ak je už pripravená (nikdy nečaká) – vkladá sa priamo do index.html."""
    return _home["data"] if _home["data"] is not None else None


def _home_warmer():
    """Pripraví úvodnú stránku vopred, aby ju nikto nemusel čakať."""
    time.sleep(20)
    while True:
        try:
            _store_home(build_home())
        except Exception:
            pass
        time.sleep(480)


def start_background():
    # príprava úvodnej stránky je náročná – stačí v jednom procese, ostatné ju spravia až na požiadanie
    if O.only_one_process("home"):
        threading.Thread(target=_home_warmer, daemon=True, name="home").start()
    if O.only_one_process("report"):
        threading.Thread(target=_report_loop, daemon=True, name="report").start()


# =========================================================
# TEXTOVÉ STRÁNKY
# =========================================================

PAGE_CSS = """
body{margin:0;background:#f3f5fa;color:#0d1633;font:16px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif}
header{background:#101b44;padding:16px 20px}
header a{display:inline-flex;align-items:center;gap:10px;font-weight:800;font-size:21px;text-decoration:none;color:#38d8ff;
  text-shadow:0 0 4px rgba(56,216,255,.55),0 0 14px rgba(56,216,255,.45)}
main{max-width:760px;margin:24px auto 60px;padding:28px 24px;background:#fff;border:1px solid #e2e6ef;border-radius:14px}
h1{font-size:28px;line-height:1.2;margin:6px 0 14px}h2{font-size:18px;margin:1.6em 0 .4em}
p,li{color:#3c4766}a{color:#2453d6}.back{font-size:14px;text-decoration:none}
header img{width:34px;height:34px;border-radius:9px;display:block}
code{background:#f3f5fa;border-radius:5px;padding:1px 5px;font-size:.92em}
.upd{margin-top:2em;font-size:13px;color:#8b94ad}
.box{text-align:center}.btn{display:inline-block;margin-top:12px;background:#ffcf3a;color:#101b44;
  padding:11px 18px;border-radius:10px;font-weight:800;text-decoration:none}
input,button{font:inherit;padding:10px;border-radius:9px;border:1px solid #d3d9e6}
button{background:#ffcf3a;border:0;font-weight:800;cursor:pointer}
.row{display:flex;gap:8px;flex-wrap:wrap;margin:6px 0}.ok{color:#0a8a4a}.err{color:#d4334b}
.w{color:#b7791f}small{color:#8b94ad}li{margin:4px 0}
details{border:1px solid #e2e6ef;border-radius:12px;padding:10px 12px;margin:8px 0}summary{cursor:pointer}
table{width:100%;border-collapse:collapse}td,th{padding:7px 6px;border-bottom:1px solid #e2e6ef;text-align:left}
.wrap{overflow-x:auto}.bar{height:12px;background:#eef1f7;border-radius:99px;overflow:hidden}
.bar i{display:block;height:100%;background:#2453d6}
"""


def page(title, body, back=True, status=200, extra_head="", css=""):
    home = PUBLIC_URL or "/"
    link = f'<a class="back" href="{home}">← Späť na CardRadar</a>' if back else ""
    doc = f"""<!doctype html><html lang="sk"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)} – CardRadar</title>
<link rel="icon" href="/static/cr-logo.png?v=3">
{extra_head}<style>{PAGE_CSS}{css}</style><header><a href="{home}"><img src="/static/cr-logo.png" alt="" onerror="this.remove()">CardRadar</a></header>
<main>{link}{body}</main>"""
    return Response(doc, status=status, mimetype="text/html", headers={"Cache-Control": "no-store"})


def simple_page(title, text):
    """Krátke oznámenie (potvrdenie strážcu a pod.)."""
    return page(title, f'<div class="box"><h1>{title}</h1><p>{text}</p>'
                       f'<a class="btn" href="{PUBLIC_URL or "/"}">Späť na CardRadar</a></div>', back=False)


def _contact():
    if CONTACT_EMAIL:
        e = html.escape(CONTACT_EMAIL)
        return f'<a href="mailto:{e}">{e}</a>'
    return "cez kontakt uvedený na webe"


def _text_page(title, body):
    who = html.escape(OPERATOR_NAME) + (", " + html.escape(OPERATOR_DETAILS) if OPERATOR_DETAILS else "")
    body = body.replace("{KONTAKT}", _contact()).replace("{PREVADZKOVATEL}", who)
    return page(title, f"<h1>{title}</h1>{body}<p class='upd'>Platné od 8. októbra 2026</p>")


TERMS_HTML = """
<p>Prevádzkovateľ webu CardRadar (getcardradar.com): {PREVADZKOVATEL}. Kontakt: {KONTAKT}.</p>
<h2>Čo CardRadar je</h2>
<p>CardRadar je bezplatný porovnávač cien Pokémon TCG produktov (karty, Elite Trainer Boxy, booster boxy
a ďalšie balenia) zo slovenských a českých internetových obchodov. CardRadar <b>nič nepredáva</b>,
nie je predajcom ani sprostredkovateľom kúpy a neuzatvára s tebou žiadnu zmluvu o kúpe tovaru.
Kliknutím na ponuku prejdeš na web obchodu; kúpna zmluva vzniká výlučne medzi tebou a obchodom
a riadi sa jeho obchodnými podmienkami, reklamačným poriadkom a cenami.</p>
<h2>Odkiaľ sú údaje</h2>
<p>Ceny, dostupnosť, názvy a obrázky produktov pochádzajú z XML feedov, ktoré nám obchody poskytli,
alebo z ich verejne dostupných stránok. Aktualizujú sa automaticky, spravidla raz za hodinu, a môžu
byť oneskorené alebo nepresné. <b>Rozhodujúca je vždy cena a dostupnosť na stránke obchodu.</b>
Ceny z českých obchodov prepočítavame z Kč na € podľa denného kurzu Európskej centrálnej banky,
preto sú orientačné. Poštovné uvádzame len tam, kde ho poznáme.</p>
<h2 id="poradie">Ako zoraďujeme ponuky</h2>
<p>Ponuky sú predvolene zoradené <b>podľa ceny od najnižšej</b> (ponuky skladom pred vypredanými).
Zoradenie si môžeš zmeniť (podľa názvu, dostupnosti, ceny za booster alebo ceny s dopravou).
Rovnaký produkt z viacerých obchodov zobrazujeme spolu. Označenie „Najlacnejšie“ dostane
najlacnejšia ponuka skladom.</p>
<p><b>Poradie ponúk nie je možné kúpiť</b> a nijako ho neovplyvňuje, či s nami obchod spolupracuje
alebo nám platí.</p>
<h2 id="partneri">Ako CardRadar zarába</h2>
<p>Používanie CardRadaru je pre teba zadarmo. Náklady na prevádzku pokrývame z odmien od obchodov:</p>
<ul>
<li><b>Platba za preklik</b> – niektoré obchody nám platia dohodnutú sumu za preklik z CardRadaru na ich web.</li>
<li><b>Partnerské (affiliate) odkazy</b> – niektoré odkazy vedú cez partnerský program obchodu alebo
affiliate siete. Ak nakúpiš, môžeme dostať províziu.</li>
</ul>
<p>Cenu, ktorú v obchode zaplatíš, to nijako nemení. Obchody, ktoré s nami takto spolupracujú,
sú pri ponukách označené štítkom <b>„Partner“</b>. Zobrazujeme aj obchody, ktoré nám nič neplatia.</p>
<h2>Strážca ceny</h2>
<p>Strážca ceny a naskladnenia je bezplatná služba bez záruky, že upozornenie príde vždy včas
alebo že produkt bude v čase nákupu dostupný za uvedenú cenu. Zrušíš ho kedykoľvek odkazom v e-maile.</p>
<h2>Zodpovednosť</h2>
<p>CardRadar nezodpovedá za obsah webov obchodov, za ich tovar, dodanie ani za rozdiel medzi cenou
u nás a cenou v obchode. Ak nájdeš chybu v údajoch, napíš nám a opravíme ju.</p>
<h2>Ochranné známky</h2>
<p>Pokémon a súvisiace názvy a obrázky sú ochranné známky ich vlastníkov (Nintendo, Creatures,
GAME FREAK, The Pokémon Company). CardRadar nie je s nimi spojený ani nimi podporovaný.
Obrázky produktov pochádzajú od obchodov.</p>
<h2>Riešenie sporov</h2>
<p>S podnetmi a sťažnosťami sa obráť na kontakt vyššie. Za kúpený tovar zodpovedá obchod, v ktorom
si nakúpil. Dozor nad ochranou spotrebiteľa vykonáva Slovenská obchodná inšpekcia (soi.sk).</p>
"""

PRIVACY_HTML = """
<p>Prevádzkovateľ: {PREVADZKOVATEL}. Kontakt: {KONTAKT}.</p>
<h2>Aké údaje spracúvame a prečo</h2>
<ul>
<li><b>Strážca ceny:</b> e-mail, sledovaný produkt, cieľová cena a čas potvrdenia. Účel: poslať
upozornenie, o ktoré si požiadal. Právny základ: tvoj súhlas (čl. 6 ods. 1 písm. a) GDPR),
potvrdený kliknutím v e-maile. Súhlas odvoláš odkazom „Zrušiť strážcu“ v každom e-maile.</li>
<li><b>Prekliky do obchodov:</b> počítame, koľko prekliknutí smeruje do ktorého obchodu a v ktorý deň
(podklad na vyúčtovanie s obchodmi). Aby sa jeden preklik nezapočítal viackrát, uložíme na 2 dni
len jednosmerný odtlačok (hash) zo skrátených technických údajov – z neho sa IP adresa ani tvoja
identita nedajú spätne zistiť. Právny základ: oprávnený záujem (čl. 6 ods. 1 písm. f) GDPR).</li>
<li><b>Hľadané výrazy:</b> ukladáme len text hľadania a počet za deň, bez väzby na teba
(zobrazenie „Najhľadanejšie“).</li>
<li><b>IP adresa:</b> drží sa v pamäti servera asi minútu na ochranu pred zneužitím
(obmedzenie počtu požiadaviek), do databázy sa neukladá. Krátkodobo sa môže objaviť
v technických záznamoch servera. Právny základ: oprávnený záujem (bezpečnosť).</li>
<li><b>Obľúbené produkty, posledné hľadania a tmavý režim</b> sa ukladajú iba v tvojom prehliadači,
nie na našom serveri.</li>
</ul>
<h2>Prechod do obchodu</h2>
<p>Keď klikneš na ponuku, odkaz obsahuje označenie zdroja (<code>utm_source=cardradar</code>)
a pri partnerských obchodoch môže viesť cez server affiliate siete. Na webe obchodu alebo siete
sa už riadi spracovanie údajov a cookies ich vlastnými zásadami ochrany súkromia – CardRadar
k týmto údajom nemá prístup.</p>
<h2>Ako dlho</h2>
<p>Nepotvrdený strážca sa zmaže po 7 dňoch, splnený 30 dní po odoslaní upozornenia, aktívny
trvá, kým ho nezrušíš. Odtlačky prekliknutí sa mažú po 2 dňoch. Súhrnné počty prekliknutí
a hľadaní (bez osobných údajov) uchovávame dlhodobo.</p>
<h2>Kto k údajom má prístup</h2>
<p>Web beží na serveroch spoločnosti Render Services, Inc. v dátovom centre vo Frankfurte (EÚ).
E-maily strážcu odosiela služba Resend zo serverov v Írsku (EÚ). Doménu a DNS spravuje Cloudflare,
cez ktorý prechádza prevádzka webu. Títo dodávatelia spracúvajú údaje len v našom mene.
Stránka nenačítava reklamy, analytické nástroje, písma ani skripty od tretích strán.
Údaje nepredávame ani nezdieľame na reklamné účely.</p>
<h2>Cookies</h2>
<p>CardRadar nepoužíva reklamné ani sledovacie cookies. Používame len nevyhnutné technické
úložisko prehliadača (obľúbené produkty, posledné hľadania, tmavý režim, rýchlejšie načítanie),
na ktoré sa súhlas nevyžaduje.</p>
<h2>Tvoje práva</h2>
<p>Máš právo na prístup k údajom, ich opravu, vymazanie, obmedzenie spracúvania, prenosnosť,
námietku proti spracúvaniu na základe oprávneného záujmu a odvolanie súhlasu. Stačí napísať na
kontakt vyššie. Sťažnosť môžeš podať na Úrad na ochranu osobných údajov SR (dataprotection.gov.sk).</p>
"""

SHOPS_HTML = """
<p>CardRadar je porovnávač cien Pokémon TCG kariet, ETB a booster boxov zo slovenských a českých
obchodov. Zberateľ zadá kartu alebo set a na jednom mieste vidí, kde je produkt skladom a za koľko.
Kliknutím ide <b>priamo na stránku produktu vo vašom e-shope</b>. CardRadar nič nepredáva a nikomu
neberie objednávky – každý predaj je váš.</p>
<h2>Prečo byť na CardRadare</h2>
<ul>
<li>Návštevníci, ktorí už vedia, čo chcú kúpiť – hľadajú konkrétny set, ETB alebo kartu.</li>
<li>Všetky odkazy majú <code>utm_source=cardradar</code>, takže návštevy aj objednávky vidíte
v Google Analytics alebo v štatistikách e-shopu ako samostatný zdroj.</li>
<li>Nové produkty a predobjednávky sa u nás objavia hneď, ako sú vo vašom feede.</li>
</ul>
<h2>Spolupráca cez XML feed</h2>
<p>Stačí nám poslať adresu XML feedu, ktorý už pravdepodobne máte pre Heureku, Google Merchant
alebo Zboží. Shoptet, Upgates, Shopify aj WooCommerce ho vedia vytvoriť niekoľkými kliknutiami.
Feed čítame raz za hodinu, váš web to nijako nezaťaží.</p>
<p>Z feedu používame len Pokémon produkty a tieto údaje:</p>
<ul>
<li><b>názov</b> (<code>PRODUCTNAME</code> / <code>g:title</code>),</li>
<li><b>odkaz na produkt</b> (<code>URL</code> / <code>g:link</code>),</li>
<li><b>cena s DPH</b> (<code>PRICE_VAT</code> / <code>g:price</code>, prípadne <code>g:sale_price</code>),</li>
<li><b>dostupnosť</b> (<code>DELIVERY_DATE</code> / <code>g:availability</code>),</li>
<li><b>obrázok</b> (<code>IMGURL</code> / <code>g:image_link</code>),</li>
<li><b>kategória alebo značka</b> (<code>CATEGORYTEXT</code>, <code>MANUFACTURER</code>, <code>g:brand</code>) –
podľa nej spoznáme Pokémon produkty, ak to nie je v názve.</li>
</ul>
<h2>Možnosti spolupráce</h2>
<ul>
<li><b>Základ – zadarmo:</b> zaradenie vašich produktov cez XML feed.</li>
<li><b>Partnerský (affiliate) program:</b> ak máte program cez eHUB, Dognet, CJ alebo vlastný,
odkazy na vaše produkty pôjdu cez neho a platíte len z uskutočnených objednávok.</li>
<li><b>Platba za preklik:</b> dohodnutá cena za unikátny preklik do vášho e-shopu, mesačne
s prehľadom počtu preklikov. Ako partner máte pri ponukách odznak „Partner“.</li>
</ul>
<p>Poradie ponúk je vždy podľa ceny – za lepšie umiestnenie sa platiť nedá, aby porovnanie
zostalo dôveryhodné pre zákazníkov aj pre vás.</p>
<h2>Bez feedu</h2>
<p>Bez feedu čítame verejne dostupné stránky kategórií, šetrne: najviac raz za hodinu s pauzou
medzi stránkami. Náš robot sa predstavuje ako <code>CardRadarBot</code>.</p>
<h2>Opravy a odstránenie</h2>
<p>Ak je niektorý údaj nesprávny, opravíme ho. Ak si neželáte, aby bol váš obchod na CardRadare,
stačí napísať a odstránime ho, zvyčajne do 24 hodín.</p>
<h2>Kontakt</h2>
<p>Pošlite nám adresu feedu alebo sa ozvite s otázkami: {KONTAKT}</p>
"""


def terms_page():
    return _text_page("Podmienky používania", TERMS_HTML)


def privacy_page():
    return _text_page("Ochrana osobných údajov", PRIVACY_HTML)


def shops_page():
    return _text_page("Pre obchody", SHOPS_HTML)


def robots_txt():
    if ALLOW_INDEXING:
        body = ("User-agent: *\nAllow: /\nDisallow: /api/\nDisallow: /admin/\nDisallow: /alerts/\n"
                "Disallow: /go\n" + (f"Sitemap: {PUBLIC_URL}/sitemap.xml\n" if PUBLIC_URL else ""))
    else:
        body = "User-agent: *\nDisallow: /\n"
    return Response(body, mimetype="text/plain")


# =========================================================
# ADMIN: KONTROLA HĽADANIA (/admin/test)
# Beží na pozadí, priebeh sa ukladá do databázy (vidia ho všetky procesy).
# =========================================================

SELFTEST_QUERIES = [
    "pitch black booster box", "pitch black etb", "chaos rising etb",
    "destined rivals etb", "destined rivals booster box", "prismatic evolutions etb",
    "surging sparks booster bundle", "151 etb", "ascended heroes etb",
    "charizard ex", "pikachu ex", "umbreon vmax", "rare candy", "trick or trade",
    "bundle", "display",
]


def _kind(t):
    if _ETB_RE.search(t):
        return "etb"
    if _BOX_RE.search(t):
        return "box"
    if re.search(r"booster|bundle|collection|kolekci|\btins?\b|blister|battle\s+deck|premium|display", t, re.I):
        return "other"
    return "card"


def _check_query(q):
    parsed = L.normalize_query(q)
    norm = parsed["normalized"] or q
    t0 = time.monotonic()
    results, diagnostics = O.search_all(norm, wait_all=True)
    want = {"elite trainer box": "etb", "booster box": "box"}.get(parsed["product_type"])
    issues, merge = [], {}
    for r in results:
        t, p, c = r["title"], r["price_eur"] or 0, _kind(r["title"])
        asia = r.get("language") in L.ASIAN_LANGS
        why = []
        if p <= 0 or p > 3000:
            why.append("nezmyselná cena")
        if c == "etb" and p < 30 and not asia:
            why.append("ETB pod 30 €")
        if c == "box" and p < 70 and not asia:
            why.append("booster box pod 70 €")
        if c == "box" and p > 700:
            why.append("booster box nad 700 €")
        if parsed["set_name"] and not L.set_matches_text(t, parsed["set_name"]):
            why.append("iný set")
        if want and c != want:
            why.append("iný typ produktu")
        if not r["image"]:
            why.append("bez obrázka")
        if not r["stock"]:
            why.append("sklad neuvedený")
        if L.merch_reason(t) and r.get("kind") != "accessory":   # sleeves/albumy majú vlastnú záložku
            why.append("príslušenstvo vo výsledkoch")
        if why:
            issues.append({"shop": r["shop"], "title": t, "price": p, "why": why})
        if c != "card" and not L.is_combo(t):
            gt = next((n for n, rx in L.GROUP_TYPES if rx.search(t)), "")
            pp = L.normalize_query(t)
            if gt and gt not in L._DETAIL_TYPES and gt != "collection" and pp["set_name"]:
                k = (pp["set_name"], gt, pp["pokemon"].lower(), r.get("language") or "EN")
                merge.setdefault(k, []).append(r)
    missed = [[f"{r['shop']}: {r['title']}" for r in rs] for rs in merge.values()
              if len({r["shop"] for r in rs}) > 1 and len({r["group"] for r in rs}) > 1]
    return {"query": q, "ms": round((time.monotonic() - t0) * 1000), "results": len(results),
            "shops": O.shops_status(diagnostics), "issues": issues, "missed": missed}


def _run_selftest(queries):
    state = {"status": "running", "started": time.time(), "done": 0, "total": len(queries), "report": []}
    O.meta_set("selftest", json.dumps(state))
    out = {}
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = {ex.submit(_check_query, q): q for q in queries}
        for f in as_completed(futs):
            q = futs[f]
            try:
                out[q] = f.result()
            except Exception as e:
                out[q] = {"query": q, "ms": 0, "results": 0, "shops": [], "missed": [],
                          "issues": [{"shop": "-", "title": "Chyba testu: " + str(e)[:150],
                                      "price": 0, "why": ["chyba testu"]}]}
            state["done"] = len(out)
            O.meta_set("selftest", json.dumps(state))
    state.update(status="done", finished=time.time(), report=[out[q] for q in queries if q in out])
    O.meta_set("selftest", json.dumps(state, ensure_ascii=False))


def _report_html(state):
    e = html.escape
    report = state.get("report", [])
    shops = {}
    for qr in report:
        for d in qr["shops"]:
            s = shops.setdefault(d["name"], {"ok": 0, "n": 0, "res": 0, "ms": 0, "errs": set()})
            s["n"] += 1
            s["ms"] += d["elapsed_ms"] or 0
            s["res"] += d["results"] or 0
            if d["ok"]:
                s["ok"] += 1
            else:
                s["errs"].add(d["status"])
    rows = "".join(
        f"<tr><td>{e(n)}</td><td class='{'ok' if s['ok'] == s['n'] else 'err'}'>{s['ok']}/{s['n']}</td>"
        f"<td>{s['res']}</td><td>{round(s['ms'] / max(1, s['n']))} ms</td><td>{e(', '.join(sorted(s['errs'])))}</td></tr>"
        for n, s in shops.items())
    kinds = {}
    for qr in report:
        for it in qr["issues"]:
            for w in it["why"]:
                kinds[w] = kinds.get(w, 0) + 1
    kinds_html = "".join(f"<li><b>{e(k)}</b>: {v}×</li>" for k, v in sorted(kinds.items(), key=lambda x: -x[1])) \
        or "<li>Nič 🎉</li>"
    blocks = ""
    for qr in report:
        serious = [i for i in qr["issues"] if set(i["why"]) - {"bez obrázka", "sklad neuvedený"}]
        lines = "".join(f"<li><b>{e(i['shop'])}</b> · {e(i['title'])} · {i['price']:.2f} € "
                        f"<span class='w'>{e(', '.join(i['why']))}</span></li>" for i in serious[:15])
        miss = "".join(f"<li>{'<br>'.join(e(x) for x in m)}</li>" for m in qr["missed"][:6])
        blocks += (f"<details{' open' if lines or miss else ''}><summary><b>{e(qr['query'])}</b> · "
                   f"{qr['results']} ponúk · {qr['ms']} ms · {len(serious)} problémov</summary>"
                   + (f"<p>Podozrivé:</p><ul>{lines}</ul>" if lines else "")
                   + (f"<p>Rovnaký produkt v rôznych obchodoch, ale nespojený:</p><ul>{miss}</ul>" if miss else "")
                   + ("" if lines or miss else "<p class='ok'>Bez problémov.</p>") + "</details>")
    when = datetime.fromtimestamp(state.get("finished", time.time()), timezone.utc).strftime("%d.%m. %H:%M UTC")
    again = "/admin/test?start=1"
    return (f"<h1>Kontrola hľadania</h1><p>Dokončené {when}. <a href='{e(again)}'>Spustiť znova</a> · "
            f"<a href='/admin/logout'>Odhlásiť admina</a></p>"
            f"<h2>Obchody</h2><div class='wrap'><table><tr><th>Obchod</th><th>Odpovedal</th><th>Ponúk</th>"
            f"<th>Čas</th><th>Chyby</th></tr>{rows}</table></div>"
            f"<h2>Typy problémov</h2><ul>{kinds_html}</ul><h2>Hľadania</h2>{blocks}")


def admin_test():
    if not is_admin():
        return page("Nepovolené", "<h1>Nepovolené</h1><p>Pridaj ?key=ADMIN_KEY</p>", status=403)
    try:
        state = json.loads(O.meta_get("selftest") or "null")
    except Exception:
        state = None
    running = bool(state and state.get("status") == "running" and time.time() - state.get("started", 0) < 600)
    if not running and (request.args.get("start") or not state or state.get("status") == "running"):
        queries = [q.strip() for q in request.args.get("q", "").split(",") if q.strip()] or SELFTEST_QUERIES
        threading.Thread(target=_run_selftest, args=(queries[:20],), daemon=True, name="selftest").start()
        state, running = {"status": "running", "done": 0, "total": len(queries[:20])}, True
    if running:
        pct = round(state.get("done", 0) / max(1, state.get("total", 1)) * 100)
        resp = page("Kontrola beží", f"<div class='box'><h1>Kontrola beží…</h1>"
                                     f"<p>Hotových {state.get('done', 0)} z {state.get('total', 0)} hľadaní</p>"
                                     f"<div class='bar'><i style='width:{pct}%'></i></div>"
                                     f"<p><small>Stránka sa obnovuje sama, trvá to 1 – 2 minúty.</small></p></div>",
                    back=False)
        # obnovuje sa na /admin/test (bez start=1), inak by sa kontrola spúšťala stále dookola
        resp.headers["Refresh"] = "4; url=/admin/test"
        return resp
    if request.args.get("format") == "json":
        return jsonify(state)
    return page("Kontrola hľadania", _report_html(state), back=False)


# =========================================================
# ADMIN: OBCHODY (/admin/obchody)
# Otestuješ ľubovoľný e-shop a jedným ťuknutím ho zapneš / vypneš.
# =========================================================

def _num(v):
    v = L.to_float(str(v or "").replace("€", "").strip()) if str(v or "").strip() else None
    return v if v is not None and v >= 0 else None


def _shop_form_values(form):
    """Hodnoty z formulára pre feed / partnera (prázdne = nenastavené)."""
    vals = {
        "feed": (form.get("feed") or "").strip(),
        "affiliate": (form.get("affiliate") or "").strip(),
        "cpc_eur": _num(form.get("cpc_eur")),
        "partner": True if form.get("partner") else None,
        "pokemon_only": True if form.get("pokemon_only") else None,
        "currency": (form.get("currency") or "").strip().upper() or None,
    }
    if vals["affiliate"] and "{url}" not in vals["affiliate"]:
        vals["affiliate"] = ""   # bez {url} by všetky kliky išli na úvodnú stránku obchodu
    return vals


def _feed_fields(e, cur=None, new=False):
    cur = cur or {}
    sel = lambda c: " selected" if (cur.get("currency") or "") == c else ""
    return (("<label>Názov obchodu<br><input name=name required style='width:100%'></label>"
             "<label>Krajina <select name=country><option>SK</option><option>CZ</option></select></label>"
             if new else "")
            + f"<label>Adresa XML feedu<br><input name=feed value='{e(cur.get('feed', ''))}' "
              f"placeholder='https://www.obchod.sk/heureka.xml' style='width:100%'></label>"
            + f"<label>Partnerský odkaz (nepovinné, musí obsahovať {{url}})<br><input name=affiliate "
              f"value='{e(cur.get('affiliate', ''))}' placeholder='https://ehub.cz/system/scripts/click.php?a_aid=…&amp;desturl={{url}}' "
              f"style='width:100%'></label>"
            + f"<label>Cena za unikátny klik € (nepovinné) <input name=cpc_eur value='{e(str(cur.get('cpc_eur') or ''))}' "
              f"style='width:90px' inputmode=decimal></label> "
            + f"<label>Mena feedu <select name=currency><option value=''>podľa krajiny</option>"
              f"<option{sel('EUR')}>EUR</option><option{sel('CZK')}>CZK</option></select></label><br>"
            + f"<label><input type=checkbox name=partner{' checked' if cur.get('partner') else ''}> Partner "
              f"(odznak na webe)</label> "
            + f"<label><input type=checkbox name=pokemon_only{' checked' if cur.get('pokemon_only') else ''}> "
              f"Feed obsahuje len Pokémon</label>")


def _feed_test_html(e, r):
    if not r.get("ok"):
        inf = r.get("info") or {}
        extra = f" Preskočené: {e(json.dumps(inf.get('skipped', {}), ensure_ascii=False))}." if inf else ""
        return f"<p class=err>Feed nevrátil žiadne Pokémon produkty. {e(r.get('error', ''))}{extra}</p>"
    inf = r["info"]
    rows = "".join(f"<li>{e(x['title'])} – <b>{x['price_eur']:.2f} €</b>"
                   f"{' (' + str(x['price_czk']) + ' Kč)' if x.get('price_czk') else ''} "
                   f"<small>{e(x['stock'] or 'sklad ?')} · {e(x['link'])}</small></li>" for x in r["items"])
    return (f"<p class=ok>Feed funguje ({r['ms']} ms). Ukážka prvých {len(r['items'])} Pokémon produktov:</p>"
            f"<ul>{rows}</ul><p><small>Prečítaných položiek: {inf['scanned']}, domény: "
            f"{e(', '.join(inf['hosts']))}. Preskočené: {e(json.dumps(inf['skipped'], ensure_ascii=False))}</small></p>")


def admin_obchody():
    e = html.escape
    if not is_admin():
        return page("Nepovolené", "<h1>Nepovolené</h1><p>Pridaj ?key=ADMIN_KEY</p>", status=403)
    base = "/admin/obchody"
    saved = O.extra_shops()
    msg = ""

    if request.method == "POST":
        act = request.form.get("act")
        if act == "add":
            try:
                c = json.loads(request.form.get("config", ""))
                ok = O.host_of(c["search_url"]) == O.host_of(c["base_url"])
            except Exception:
                ok = False
            if ok:
                c["name"] = (request.form.get("name") or c["name"]).strip()[:40]
                c = {k: c[k] for k in ("name", "country", "base_url", "search_url", "link_selector", "shopify")
                     if k in c}
                O.save_extra_shops([s for s in saved if s["name"] != c["name"]] + [c])
            return redirect(base, code=303)
        if act == "remove":
            O.save_extra_shops([s for s in saved if s["name"] != request.form.get("name")])
            return redirect(base, code=303)
        if act in ("feed_test", "feed_add"):
            name = (request.form.get("name") or "").strip()[:40]
            vals = _shop_form_values(request.form)
            shop = {"name": name or "test", "country": request.form.get("country", "SK"), "base_url": "",
                    **{k: v for k, v in vals.items() if v not in (None, "")}}
            if not vals["feed"].startswith(("http://", "https://")):
                msg = "<p class=err>Zadaj adresu feedu (https://…).</p>"
            else:
                r = O.test_feed(shop)
                msg = _feed_test_html(e, r)
                hosts = (r.get("info") or {}).get("hosts") or []
                if act == "feed_add" and r.get("ok") and name:
                    if O.shop_by_link(r["items"][0]["link"]):
                        msg += ("<p class=err>Tento obchod už na CardRadare je – feed mu nastav v zozname "
                                "obchodov hore (tlačidlo Nastavenia).</p>")
                    else:
                        shop["base_url"] = "https://" + O.host_of(r["items"][0]["link"]) + "/"
                        O.save_extra_shops([x for x in saved if x["name"] != name] + [shop])
                        msg += (f"<p class=ok><b>{e(name)}</b> je zapnutý. Celý feed sa načíta do pár minút "
                                f"(stav v <a href='/admin/katalog'>/admin/katalog</a>).</p>")
                elif act == "feed_add" and r.get("ok") and not name:
                    msg += "<p class=err>Doplň názov obchodu.</p>"
                if hosts and len(hosts) > 1:
                    msg += f"<p class=w>Feed obsahuje viac domén ({e(', '.join(hosts))}) – berie sa len prvá.</p>"
        if act == "settings":
            name = request.form.get("name", "")
            vals = _shop_form_values(request.form)
            ex = next((x for x in saved if x["name"] == name), None)
            if ex is not None:   # obchod pridaný cez admin: upraví sa priamo
                for k, v in vals.items():
                    if v in (None, ""):
                        ex.pop(k, None)
                    else:
                        ex[k] = v
                O.save_extra_shops(saved)
            else:
                O.save_shop_override(name, vals)
            if vals["feed"]:
                shop = next((x for x in O.all_shops() if x["name"] == name), None)
                if shop:
                    O.crawl_in_background(shop)
            return redirect(base + "?saved=" + urllib.parse.quote(name), code=303)

    h = "<h1>Obchody a partneri</h1>"
    if request.args.get("saved"):
        h += f"<p class=ok>Uložené: {e(request.args['saved'])}</p>"
    h += ("<p><a href='/admin/partneri'>Kliky a fakturácia →</a> · <a href='/admin/katalog'>Stav katalógov →</a></p>"
          "<h2>Zapnuté obchody</h2>")
    for s in O.active_shops():
        kind = "XML feed" if s.get("feed") else "katalóg" if O.is_catalog(s) else "vyhľadávanie"
        tags = []
        if s.get("partner"):
            tags.append("<span class=ok>partner</span>")
        if s.get("affiliate"):
            tags.append("affiliate")
        if s.get("cpc_eur"):
            tags.append(f"{s['cpc_eur']:.2f} €/klik")
        fi = O.feed_info(s["name"]) if s.get("feed") else None
        if fi:
            tags.append(f"feed: {fi['items']} produktov" + (" <span class=err>chyba</span>" if fi.get("errors") else ""))
        h += (f"<details><summary><b>{e(s['name'])}</b> <small>{e(s['country'])} · {kind}"
              f"{' · ' + ' · '.join(tags) if tags else ''}</small></summary>"
              f"<form method=post><input type=hidden name=act value=settings>"
              f"<input type=hidden name=name value='{e(s['name'])}'>{_feed_fields(e, s)}"
              f"<div class=row><button>Uložiť nastavenia</button></div></form>")
        if s.get("_extra"):
            h += (f"<form method=post class=row><input type=hidden name=act value=remove>"
                  f"<input type=hidden name=name value='{e(s['name'])}'><button>Vypnúť obchod</button></form>")
        h += "</details>"
    h += ("<p><small>Feed nastavený obchodu zo zoznamu SHOPS nahradí čítanie jeho kategórií "
          "(presnejšie a bez záťaže pre jeho web).</small></p>")

    h += (f"<h2 id=feed>Pridať obchod cez XML feed</h2>{msg}"
          f"<form method=post action='{base}#feed'>{_feed_fields(e, request.form if request.method == 'POST' else None, new=True)}"
          "<div class=row><button name=act value=feed_test>Otestovať feed</button>"
          "<button name=act value=feed_add>Otestovať a zapnúť</button></div></form>"
          "<p><small>Podporované: Heureka (SK/CZ), Google Merchant (RSS/Atom), Zboží a väčšina e-shopových "
          "XML exportov. Berú sa len Pokémon produkty (podľa názvu, kategórie alebo značky).</small></p>")

    test = request.args.get("test", "").strip()
    q = request.args.get("q", "").strip() or "pikachu"
    h += (f"<h2>Otestovať obchod bez feedu</h2><form class=row>"
          f"<input name=test placeholder='https://www.obchod.cz' value='{e(test)}' style='flex:1'>"
          f"<input name=q value='{e(q)}' style='width:130px'><button>Test</button></form>")
    if test:
        r = O.detect_shop(test, q)
        if r.get("platform"):
            h += f"<p>Platforma: <b>{e(r['platform'])}</b></p>"
        for t in r.get("tried", []):
            h += (f"<p><small>{e(t['url'])} → {e(str(t['status']))}, odkazov {t['links']}, "
                  f"produktov {t['results']}</small></p>")
        if r.get("error"):
            h += f"<p class=err>{e(r['error'])}</p>"
        if r.get("config"):
            h += "<p class=ok>Funguje. Ukážka:</p><ul>" + "".join(
                f"<li>{e(x['title'])} – <b>{x['price_eur']:.2f} €</b> <small>{e(x['stock'] or '?')}</small></li>"
                for x in r["sample"]) + "</ul>"
            h += (f"<form method=post class=row><input type=hidden name=act value=add>"
                  f"<input type=hidden name=config value='{e(json.dumps(r['config']))}'>"
                  f"<input name=name value='{e(r['config']['name'])}'><button>Zapnúť obchod</button></form>"
                  f"<p><small>Skontroluj ceny v ukážke. Ak sedia, zapni.</small></p>")

    h += "<h2>Návrhy na otestovanie</h2><ul>" + "".join(
        f"<li><a href='{e(base)}?test={urllib.parse.quote(u)}'>{e(n)}</a> <small>{e(u)}</small></li>"
        for n, u in O.KANDIDATI) + "</ul>"
    return page("Obchody", h, back=False, css=ADMIN_CSS)


ADMIN_CSS = "label{display:inline-block;margin:6px 8px 6px 0;font-size:14px}select{font:inherit;padding:8px;border-radius:9px}"


# =========================================================
# ADMIN: PARTNERI A FAKTURÁCIA (/admin/partneri)
# Unikátne kliky = 1 návštevník + 1 produkt za deň (roboty sa nepočítajú).
# Faktúra za mesiac = unikátne kliky × cena za klik dohodnutá s obchodom.
# =========================================================

def _month_range(ym):
    y, m = (int(x) for x in ym.split("-"))
    start = f"{y:04d}-{m:02d}-01"
    y2, m2 = (y + 1, 1) if m == 12 else (y, m + 1)
    return start, f"{y2:04d}-{m2:02d}-01"


def admin_partneri():
    e = html.escape
    if not is_admin():
        return page("Nepovolené", "<h1>Nepovolené</h1><p>Pridaj ?key=ADMIN_KEY</p>", status=403)
    now = datetime.now(timezone.utc)
    this = now.strftime("%Y-%m")
    prev = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    ym = request.args.get("m", this)
    if not re.fullmatch(r"\d{4}-\d{2}", ym):
        ym = this
    since, until = _month_range(ym)
    stats = {x["shop"]: x for x in O.click_stats(since=since, until=until)}
    shops = {s["name"]: s for s in O.all_shops()}
    rows, total = "", 0.0
    for name in sorted(set(stats) | {n for n, s in shops.items() if s.get("partner") or s.get("cpc_eur")},
                       key=lambda n: -(stats.get(n, {}).get("unique") or 0)):
        st, sh = stats.get(name, {}), shops.get(name, {})
        cpc = sh.get("cpc_eur")
        amount = (st.get("unique") or 0) * cpc if cpc else None
        total += amount or 0
        rows += (f"<tr><td><b>{e(name)}</b>{' <small class=ok>partner</small>' if sh.get('partner') else ''}</td>"
                 f"<td>{st.get('unique', 0)}</td><td>{st.get('clicks', 0)}</td>"
                 f"<td>{f'{cpc:.2f} €' if cpc else '–'}</td><td>{_eur(amount) if amount is not None else '–'}</td>"
                 f"<td>{'áno' if sh.get('affiliate') else '–'}</td></tr>")
    if request.args.get("format") == "csv":
        lines = ["obchod;unikatne_kliky;vsetky_kliky;cena_za_klik;suma"]
        for name, st in stats.items():
            cpc = shops.get(name, {}).get("cpc_eur") or 0
            dec = lambda v: f"{v:.2f}".replace(".", ",")
            lines.append(f"{name};{st['unique']};{st['clicks']};{dec(cpc)};{dec(st['unique'] * cpc)}")
        return Response("\ufeff" + "\n".join(lines), mimetype="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=cardradar-kliky-{ym}.csv"})
    links = " · ".join(f"<a href='?m={m}'>{lab}</a>" for m, lab in ((this, "tento mesiac"), (prev, "minulý mesiac")))
    h = (f"<h1>Partneri a kliky</h1><p>Obdobie: <b>{e(ym)}</b> · {links} · <a href='?m={e(ym)}&format=csv'>CSV</a> · "
         f"<a href='/admin/obchody'>Nastavenia obchodov</a></p>"
         "<div class='wrap'><table><tr><th>Obchod</th><th>Unikátne kliky</th><th>Všetky</th><th>Cena/klik</th>"
         f"<th>Na faktúru</th><th>Affiliate</th></tr>{rows or '<tr><td colspan=6>Zatiaľ žiadne kliky.</td></tr>'}"
         f"<tr><th>Spolu</th><th></th><th></th><th></th><th>{_eur(total)}</th><th></th></tr></table></div>"
         "<p><small>Unikátny klik = jeden návštevník a jeden produkt za deň; roboty a náhľady odkazov sa "
         "nepočítajú. Províziu z affiliate sietí (eHUB, Dognet…) vidíš v ich rozhraní – tu sú len kliky.</small></p>")
    return page("Partneri", h, back=False)


# =========================================================
# ADMIN: KATALÓGY (/admin/katalog  a  /admin/katalog?name=imago&refresh=1)
# =========================================================

def admin_katalog():
    if not is_admin():
        return jsonify({"error": "Nepovolené. Pridaj ?key=ADMIN_KEY"}), 403
    shops = [s for s in O.all_shops() if O.is_catalog(s)]
    name = L.clean_text(request.args.get("name", "")).lower()
    if name:
        shop = next((s for s in shops if L.fold(s["name"]) == L.fold(name)), None)   # „posbirej to“ = „Posbírej to“
        if not shop:
            return jsonify({"error": "Neznámy obchod.", "shops": [s["name"] for s in shops]}), 400
        if request.args.get("refresh"):
            return jsonify(O.crawl_shop(shop))
        if request.args.get("debug") and shop.get("catalog") and not shop.get("feed"):
            return jsonify(O.debug_listing(shop, request.args.get("url")))
        items, updated = O.load_catalog(shop["name"])
        return jsonify({"shop": shop["name"], "items": len(items), "updated": updated, "sample": items[:20]})
    return jsonify({"refresh_min": O.CATALOG_REFRESH_MIN, "catalogs": [
        {"shop": s["name"], "enabled": s.get("enabled", True), "items": len(O.load_catalog(s["name"])[0]),
         "updated": O.load_catalog(s["name"])[1], "crawling": O.is_crawling(s["name"])} for s in shops]})


# =========================================================
# STRÁNKY SETOV (/sety, /set/pitch-black) – pre Google aj návštevníkov
# Údaje sú z rovnakej databázy ako úvodná stránka (posledné 3 dni, skladom).
# Nový set = pridaj ho do NOVE_SETY v logika.py, stránka vznikne sama.
# =========================================================

SET_SECTIONS = [
    ("Elite Trainer Box", _ETB_RE),
    ("Booster Box", _BOX_RE),
    ("Booster Bundle", _BUNDLE_RE),
    ("Ostatné balíky", _SEALED_RE),
]


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", L.fold(name)).strip("-")


def set_by_slug(slug):
    return next((s for s in L.NOVE_SETY if slugify(s["name"]) == slug), None)


def _all_items():
    if _items["all"] is None or time.monotonic() - _items["t"] > 900:
        home_data()   # build_home naplní _items
        if _items["all"] is None:
            try:
                build_home()
            except Exception:
                _items["all"] = []
    return _items["all"] or []


def _abs(path):
    return (PUBLIC_URL or "") + path


SET_CSS = """
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:12px;margin:10px 0 6px}
.card{border:1px solid #e2e6ef;border-radius:12px;padding:12px;display:flex;flex-direction:column;gap:6px;background:#fff}
.card img{width:100%;height:150px;object-fit:contain;background:#f6f8fc;border-radius:8px}
.card .t{font-weight:700;font-size:14px;line-height:1.35;color:#0d1633}
.card .s{font-size:13px;color:#6b7aa0}.card .p{font-size:19px;font-weight:800;color:#0d1633}
.card .pp{font-size:12px;color:#6b7aa0}.card a.go{margin-top:auto;text-align:center;background:#101b44;color:#fff;
 border-radius:9px;padding:8px;font-weight:700;text-decoration:none;font-size:14px}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:10px 0}.chips a{border:1px solid #d3d9e6;border-radius:99px;
 padding:6px 12px;text-decoration:none;font-size:14px;color:#0d1633;background:#fff}
main{max-width:1000px}
"""


def _eur(v):
    return f"{v:,.2f}".replace(",", " ").replace(".", ",") + " €"


def _offer_card(it):
    e = html.escape
    img = f'<img src="{e(it["image"])}" alt="{e(it["title"])}" loading="lazy">' if it["image"].startswith("https://") else ""
    packs = L.estimate_packs(it["title"], it.get("language", ""))
    pp = (f'<span class="pp">{_eur(it["price_eur"] / packs)} / booster</span>' if packs and packs > 1 else "")
    return (f'<div class="card">{img}<span class="t">{e(it["title"])}</span><span class="s">{e(it["shop"])}</span>'
            f'<span class="p">{_eur(it["price_eur"])}</span>{pp}'
            f'<a class="go" href="{e(O.go_link(it["link"]))}" rel="nofollow sponsored">Do obchodu</a></div>')


def set_page(slug):
    s = set_by_slug(slug)
    if not s:
        return page("Set nenájdený", "<h1>Set nenájdený</h1><p>Pozri si <a href='/sety'>všetky sety</a>.</p>",
                    status=404)
    e = html.escape
    mine = [i for i in _all_items() if L.set_matches_text(i["title"], s["query"])]
    used, blocks = set(), ""
    for label, rx in SET_SECTIONS:
        part = [i for i in mine if i["link"] not in used and rx.search(i["title"])
                and not L.is_combo(i["title"]) and not L._BULK_RE.search(i["title"])]
        part = _uniq(part, 12)
        used |= {i["link"] for i in part}
        if part:
            blocks += f"<h2>{e(s['name'])} {e(label)}</h2><div class='grid'>" + "".join(map(_offer_card, part)) + "</div>"
    cards = _uniq([i for i in mine if i["link"] not in used and L.product_kind(i["title"]) == "card"], 12,
                  reverse=True)
    if cards:
        blocks += f"<h2>Karty zo setu {e(s['name'])}</h2><div class='grid'>" + "".join(map(_offer_card, cards)) + "</div>"
    if not blocks:
        blocks = "<p>Momentálne nemáme žiadne ponuky skladom. Skús to neskôr.</p>"
    search = "/?q=" + urllib.parse.quote(s["query"])
    others = "".join(f'<a href="/set/{slugify(x["name"])}">{e(x["name"])}</a>'
                     for x in L.NOVE_SETY if x["name"] != s["name"])
    body = (f"<h1>Pokémon {e(s['name'])} – ceny ETB, booster boxov a bundle</h1>"
            f"<p>Najlepšie ceny setu <b>{e(s['name'])}</b> zo slovenských a českých obchodov skladom. "
            f"Ceny z českých obchodov sú prepočítané z Kč podľa denného kurzu ECB. "
            f"<a href='{e(search)}'>Hľadať všetko zo setu →</a></p>{blocks}"
            f"<h2>Ďalšie sety</h2><div class='chips'>{others}</div>"
            f"<p class='upd'>Ceny sa aktualizujú priebežne, pred nákupom ich over v obchode.</p>")
    desc = f"Porovnanie cien Pokémon {s['name']}: Elite Trainer Box, Booster Box, Booster Bundle a karty skladom."
    return page(f"Pokémon {s['name']} ceny", body, extra_head=_meta(desc, f"/set/{slug}"), css=SET_CSS)


def sets_page():
    e = html.escape
    items = _all_items()
    rows = ""
    for s in L.NOVE_SETY:
        mine = [i for i in items if L.set_matches_text(i["title"], s["query"])]
        etb = [i["price_eur"] for i in mine if _ETB_RE.search(i["title"]) and not L.is_combo(i["title"])]
        box = [i["price_eur"] for i in mine if _BOX_RE.search(i["title"]) and not L.is_combo(i["title"])]
        rows += (f"<tr><td><a href='/set/{slugify(s['name'])}'><b>{e(s['name'])}</b></a></td>"
                 f"<td>{_eur(min(etb)) if etb else '–'}</td><td>{_eur(min(box)) if box else '–'}</td>"
                 f"<td>{len(mine)}</td></tr>")
    body = ("<h1>Pokémon sety – najlepšie ceny</h1><p>Najnižšie ceny skladom v slovenských a českých obchodoch.</p>"
            "<div class='wrap'><table><tr><th>Set</th><th>ETB od</th><th>Booster Box od</th><th>Ponúk</th></tr>"
            f"{rows}</table></div>")
    return page("Pokémon sety", body, extra_head=_meta("Ceny Pokémon setov: ETB a booster boxy skladom.", "/sety"),
                css=SET_CSS)


def _meta(desc, path):
    e = html.escape
    canon = f'<link rel="canonical" href="{e(_abs(path))}">' if PUBLIC_URL else ""
    return f'<meta name="description" content="{e(desc)}">{canon}'


def sitemap_xml():
    base = PUBLIC_URL or request.url_root.rstrip("/")
    paths = ["/", "/sety"] + [f"/set/{slugify(s['name'])}" for s in L.NOVE_SETY] + \
            ["/podmienky", "/ochrana-udajov", "/pre-obchody"]
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    urls = "".join(f"<url><loc>{html.escape(base + p)}</loc><lastmod>{day}</lastmod></url>" for p in paths)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>'
    return Response(xml, mimetype="application/xml")


# =========================================================
# DENNÁ KONTROLA (e-mail prevádzkovateľovi)
# Raz denne (REPORT_HOUR UTC) prebehne /admin/test a príde e-mail na ADMIN_EMAIL
# (inak CONTACT_EMAIL): obchody, ktoré nefungujú, podozrivé ceny, kurz, databáza, katalógy.
# =========================================================

ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "") or CONTACT_EMAIL
REPORT_HOUR = int(os.environ.get("REPORT_HOUR", "5"))   # 5 UTC = 6:00 / 7:00 u nás


def build_report(state):
    """Zhrnutie problémov z testu + stav servera. Vráti (zoznam problémov, zoznam OK správ)."""
    problems, ok = [], []
    shops = {}
    for qr in state.get("report", []):
        for d in qr["shops"]:
            x = shops.setdefault(d["name"], {"n": 0, "ok": 0, "res": 0, "errs": set()})
            x["n"] += 1
            x["res"] += d["results"] or 0
            if d["ok"]:
                x["ok"] += 1
            else:
                x["errs"].add(d["status"])
    for name, x in sorted(shops.items()):
        if x["ok"] < x["n"] / 2:
            problems.append(f"{name}: odpovedal len v {x['ok']} z {x['n']} hľadaní ({', '.join(sorted(x['errs']))})")
        elif x["res"] == 0:
            problems.append(f"{name}: odpovedá, ale nevrátil ani jeden produkt – asi sa zmenil web obchodu")
        else:
            ok.append(f"{name}: {x['res']} ponúk")
    serious = [(qr["query"], i) for qr in state.get("report", []) for i in qr["issues"]
               if set(i["why"]) - {"bez obrázka", "sklad neuvedený"}]
    if serious:
        problems.append(f"Podozrivé ponuky: {len(serious)} (detail v /admin/test)")
        for q, i in serious[:8]:
            problems.append(f"  · {q}: {i['shop']} – {i['title'][:70]} – {i['price']:.2f} € ({', '.join(i['why'])})")
    if O.db_problem():
        problems.append("Databáza: " + O.db_problem())
    if L.KURZ_INFO.get("source") != "ECB":
        problems.append(f"Kurz CZK sa nenačítal z ECB, používa sa záložný {L.KURZ['CZK']}")
    else:
        try:
            age = (datetime.now(timezone.utc).date() - datetime.strptime(L.KURZ_INFO["date"], "%Y-%m-%d").date()).days
        except Exception:
            age = 0
        (problems if age > 4 else ok).append(f"Kurz CZK {L.KURZ['CZK']} z {L.KURZ_INFO.get('date')}")
    for s in O.active_shops():
        if O.is_catalog(s):
            items, updated = O.load_catalog(s["name"])
            if not items or O._is_stale(updated, factor=4 * s.get("refresh_factor", 1)):
                problems.append(f"Katalóg {s['name']}: {len(items)} položiek, naposledy {updated or 'nikdy'}")
    clicks = O.click_stats(1)
    if clicks:
        ok.append("Kliky včera/dnes (unikátne): " + ", ".join(f"{c['shop']} {c['unique']}" for c in clicks))
    for s in O.active_shops():
        fi = O.feed_info(s["name"]) if s.get("feed") else None
        if fi and (fi.get("errors") or not fi.get("items")):
            problems.append(f"Feed {s['name']}: {fi.get('items', 0)} produktov, "
                            f"{'; '.join(x.get('error', '') for x in fi.get('errors', []))}")
    return problems, ok


def send_report(state):
    import strazca
    problems, ok = build_report(state)
    e = html.escape
    subject = (f"CardRadar: ⚠️ {len([p for p in problems if not p.startswith('  ')])} problémov"
               if problems else "CardRadar: ✅ všetko funguje")
    text = "Denná kontrola CardRadar\n\n" + ("PROBLÉMY:\n" + "\n".join(problems) + "\n\n" if problems else "") + \
           "OK:\n" + "\n".join(ok) + f"\n\nDetail: {PUBLIC_URL}/admin/test"
    body = (f"<div style='font-family:Arial,sans-serif;font-size:14px;line-height:1.6'>"
            f"<h2>Denná kontrola CardRadar</h2>"
            + (f"<h3 style='color:#d4334b'>Problémy</h3><ul>{''.join(f'<li>{e(p)}</li>' for p in problems)}</ul>"
               if problems else "<p style='color:#0a8a4a'><b>Všetko funguje.</b></p>")
            + f"<h3>OK</h3><ul>{''.join(f'<li>{e(x)}</li>' for x in ok)}</ul>"
            f"<p>Detail: <a href='{e(PUBLIC_URL)}/admin/test'>{e(PUBLIC_URL)}/admin/test</a></p></div>")
    return strazca.send_admin_mail(ADMIN_EMAIL, subject, text, body)


def _report_loop():
    time.sleep(120)
    while True:
        try:
            now = datetime.now(timezone.utc)
            day = now.strftime("%Y-%m-%d")
            if now.hour >= REPORT_HOUR and O.meta_get("report_day") != day:
                O.meta_set("report_day", day)   # najprv zapísať, aby sa nespustil dvakrát
                _run_selftest(SELFTEST_QUERIES)
                state = json.loads(O.meta_get("selftest") or "{}")
                if not send_report(state):
                    print("[CardRadar] Denná kontrola: e-mail sa neodoslal (chýba SMTP alebo ADMIN_EMAIL).",
                          flush=True)
        except Exception as ex:
            print(f"[CardRadar] Denná kontrola: {ex}", flush=True)
        time.sleep(900)


def admin_report():
    """/admin/report – ukáže zhrnutie poslednej kontroly; ?send=1 ho pošle e-mailom."""
    if not is_admin():
        return jsonify({"error": "Nepovolené."}), 403
    state = json.loads(O.meta_get("selftest") or "{}")
    problems, ok = build_report(state)
    sent = send_report(state) if request.args.get("send") else None
    return jsonify({"problems": problems, "ok": ok, "email": ADMIN_EMAIL or None, "sent": sent,
                    "clicks_30d": O.click_stats(30)})
