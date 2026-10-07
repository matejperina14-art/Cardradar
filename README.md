# CardRadar

Porovnávač cien Pokémon TCG kariet, ETB a booster boxov zo slovenských a českých obchodov.
Web: https://getcardradar.com

## Súbory

| Súbor | Čo robí |
|---|---|
| `app.py` | spúšťa web, všetky adresy (routy), ikony, PWA |
| `logika.py` | rozpoznanie hľadania, sety, filtre merchu, ceny, sklad, jazyk |
| `obchody.py` | zoznam obchodov, sťahovanie, katalógy, cache, databáza |
| `strazca.py` | strážca ceny a naskladnenia, e-maily |
| `stranky.py` | úvodná stránka, stránky setov, podmienky, admin |
| `templates/index.html` | samotný web (HTML, CSS, JavaScript) |
| `static/` | logo a ikony |

## Premenné prostredia (Render → Environment)

| Premenná | Príklad | Na čo |
|---|---|---|
| `ADMIN_KEY` | dlhé náhodné heslo | prístup do admina |
| `PUBLIC_URL` | `https://getcardradar.com` | hlavná adresa webu |
| `DB_PATH` | `/var/data/cardradar.db` | databáza na trvalom disku |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`, `SMTP_FROM` | | e-maily strážcu |
| `ADMIN_EMAIL` | | kam chodí denná kontrola |
| `CONTACT_EMAIL`, `OPERATOR_NAME` | | do podmienok a ochrany údajov |
| `ALLOW_INDEXING` | `1` | web môže byť v Google |

## Admin

Prihlásenie: raz otvor `https://getcardradar.com/admin/test?key=ADMIN_KEY`.
Prehliadač si to zapamätá na 30 dní a heslo z adresy zmizne. Odhlásenie: `/admin/logout`.

| Adresa | Čo ukáže |
|---|---|
| `/health` | stav webu (prihlásený admin vidí detail) |
| `/admin/test` | kontrola hľadania vo všetkých obchodoch |
| `/admin/obchody` | otestovanie a zapnutie nového obchodu |
| `/admin/katalog` | stav katalógových obchodov |
| `/admin/report` | zhrnutie dennej kontroly (`?send=1` pošle e-mail) |
| `/admin/cache?clear=1` | vymaže pamäť hľadania |
| `/admin/kurz` | hneď načíta kurz CZK z ECB |

## Keď vyjde nový set

1. V `logika.py` pridaj set navrch zoznamu `NOVE_SETY`.
2. Ak má skratku (napr. `me06`), pridaj ju do `SET_ALIASES`.
3. Pri katalógových obchodoch (iHRYsko) pridaj novú kategóriu do `SHOPS` v `obchody.py`.

## Nový obchod

- Shoptet, Shopify, Upgates, WooCommerce: cez `/admin/obchody`, bez úpravy kódu.
- Ostatné: nový záznam v `SHOPS` v `obchody.py` (katalóg alebo XML feed).
- Poštovné: do záznamu obchodu pridaj `"shipping": {"price": 3.9, "free_from": 60, "currency": "EUR"}`.

## Po nahratí zmien

Render nasadí web sám. Skontroluj `/health`: musí byť `"status": "ok"`.
