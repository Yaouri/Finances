"""Merchant normalisation: "PINGO DOCE LISBOA 123" / "Pingo Doce Amadora" -> "Pingo Doce".

1. clean: accents/punctuation off, drop tokens with digits (store ids, refs, dates), trailing
   country codes, known PT place names and legal/noise suffixes.
2. brands: a curated prefix list wins outright.
3. clustering: a new key joins an existing merchant when one is a prefix of the other (card
   terminals truncate at ~20 chars) or the fuzzy token ratio is very high.
"""

from __future__ import annotations

import re

from rapidfuzz import fuzz

from ..ingest.model import norm_text

COUNTRIES = {
    "PT", "PRT", "ES", "IE", "NL", "LU", "DE", "FR", "GB", "UK", "US", "USA", "IT", "BE", "CV", "MA", "CH", "SE", "DK",
}
PLACES = {
    "LISBOA", "LISBO", "LISB", "LISBON", "LIX", "AMADORA", "PORTO", "CARNAXIDE", "LEIRIA", "OEIRAS", "CASCAIS",
    "SINTRA", "ODIVELAS", "LOURES", "ALMADA", "SEIXAL", "BARREIRO", "SETUBAL", "BRAGA", "COIMBRA", "FARO",
    "AVEIRO", "POMBAL", "MATOSINHOS", "GAIA", "AMOREIRAS", "COLOMBO", "ARROIOS", "DUBLIN", "AMSTERDAM",
    "HILVERSUM", "MADRID", "MURCIA", "VALENCIA", "PRAIA", "CVERDE", "LUXEMBOURG",
}
NOISE = {
    "CONTACTLESS", "PENDING", "LDA", "SA", "S", "A", "UNIPESSOAL", "UNIP", "SOC", "SOCIEDADE", "SC", "LTD",
    "LIMITED", "BV", "SL", "INC", "COM", "WWW", "BILL", "INS", "PAYPAL", "SUMUP", "IZI", "ECOM", "EC",
    "HTTPS", "HTTP", "MY", "PT", "NV",
}
# foreign-card suffix "USD TC 0.87" and dangling connectors
TRAILING = {"USD", "GBP", "CHF", "MAD", "CVE", "BRL", "TC", "DE", "DA", "DO", "DAS", "DOS", "E"}

# canonical brand -> prefixes of the cleaned key
BRANDS = {
    "Pingo Doce": ["PINGO DOCE"],
    "Continente": ["CONTINENTE", "MODELO CONTINENTE", "CONTIN BOM"],
    "Lidl": ["LIDL"],
    "Aldi": ["ALDI"],
    "Minipreço": ["MINIPRECO"],
    "Auchan": ["AUCHAN", "MY AUCHAN", "JUMBO"],
    "Mercadona": ["MERCADONA"],
    "Intermarché": ["INTERMARCHE"],
    "El Corte Inglés": ["EL CORTE INGLES"],
    "Glovo": ["GLOVO"],
    "Uber Eats": ["UBER EATS"],
    "Uber": ["UBER", "UBR"],
    "Bolt": ["BOLT"],
    "Amazon Prime": ["AMAZON PRIME"],
    "Amazon": ["AMAZON", "AMZN"],
    "Apple": ["APPLE"],
    "PlayStation": ["PLAYSTATION", "SONY INTERACTIVE"],
    "Netflix": ["NETFLIX"],
    "Spotify": ["SPOTIFY"],
    "Anthropic": ["ANTHROPIC", "CLAUDE"],
    "Torbox": ["TORBOX", "CA TORBOX"],
    "OpenAI": ["OPENAI", "CHATGPT"],
    "Google": ["GOOGLE"],
    "Zara Home": ["ZARA HOME"],
    "Zara": ["ZARA"],
    "Mango": ["MANGO"],
    "Revolut": ["REVOLUT"],
    "EMEL": ["EMEL"],
    "Metro de Lisboa": ["METROPOLITANO", "METRO LISBOA"],
    "CP": ["CP COMBOIOS", "COMBOIOS DE PORTUGAL", "INTERCIDADES"],
    "Carris": ["CARRIS"],
    "TAP": ["TRANSPORTES AEREOS POR", "TAP AIR"],
    "Ryanair": ["RYANAIR"],
    "easyJet": ["EASYJET"],
    "Booking.com": ["BOOKING"],
    "Airbnb": ["AIRBNB"],
    "Galp": ["GALP"],
    "Repsol": ["REPSOL"],
    "BP": ["BP"],
    "Ascendi": ["ASCENDI"],
    "Via Verde": ["VIA VERDE"],
    "NOS Cinemas": ["NOS CINEMA"],
    "Worten": ["WORTEN"],
    "FNAC": ["FNAC"],
    "IKEA": ["IKEA"],
    "Decathlon": ["DECATHLON"],
    "PcComponentes": ["PCCOMPONENTES"],
    "Celeiro": ["CELEIRO"],
    "Gertal": ["GERTAL"],
    "Burger King": ["BK", "BURGER KING"],
    "McDonald's": ["MCDONALDS", "MCDONALD", "MC DONALDS"],
    "Talho Povo": ["TALHO POVO"],
    "Farmácia": ["FARMACIA"],
    "DEGIRO": ["DEGIRO", "FLATEX"],
    "Trading 212": ["TRADING 212"],
}
_BRAND_LIST = sorted(((p, b) for b, ps in BRANDS.items() for p in ps), key=lambda x: -len(x[0]))


def clean_key(text: str) -> str:
    toks = norm_text(text).split()
    out = []
    for i, t in enumerate(toks):
        if not any(ch.isdigit() for ch in t) or t == "212":
            out.append(t)
        elif i == 0 and len(t) <= 3:
            out.append(t)  # short codes are names ("A1", "A5")
        elif i == 0 and (letters := re.sub(r"\d+", "", t)):
            out.append(letters)  # strip store numbers ("BK32777" -> "BK")
    while len(out) > 1 and (out[-1] in COUNTRIES or out[-1] in PLACES or out[-1] in NOISE or out[-1] in TRAILING):
        out.pop()
    return " ".join(t for t in out if t not in NOISE or len(out) <= 1)


def brand_for(key: str) -> str | None:
    """Prefix match (long prefixes also match truncated words, "CONTINENTE BOM DIAOI"), then
    whole-word containment for distinctive brands ("VIFRA MCDONALD SLE", "CPR LIDL CASAL")."""
    for prefix, brand in _BRAND_LIST:
        if key == prefix or key.startswith(prefix + " ") or (len(prefix) >= 6 and key.startswith(prefix)):
            return brand
    padded = f" {key} "
    for prefix, brand in _BRAND_LIST:
        if len(prefix) >= 4 and f" {prefix} " in padded:
            return brand
    return None


def display_name(key: str) -> str:
    small = {"de", "da", "do", "das", "dos", "e"}
    words = key.lower().split()
    return " ".join(w if (i and w in small) else w.capitalize() for i, w in enumerate(words)) or "Unknown"


def same_merchant(a: str, b: str) -> bool:
    if not a or not b:
        return False
    short, long_ = sorted((a, b), key=len)
    if len(short) >= 8 and long_.startswith(short):
        return True
    return fuzz.token_sort_ratio(a, b) >= 92


class MerchantResolver:
    """Maps cleaned keys to merchant names, persisting aliases so results are stable."""

    def __init__(self, con):
        self.con = con
        self.alias = {r["alias"]: (r["merchant_id"], r["name"]) for r in con.execute(
            "SELECT a.alias, a.merchant_id, m.name FROM merchant_aliases a JOIN merchants m ON m.id = a.merchant_id")}
        self.by_name = {r["name"]: r["id"] for r in con.execute("SELECT id, name FROM merchants")}

    def merchant_id(self, name: str) -> int:
        if name not in self.by_name:
            self.by_name[name] = self.con.execute("INSERT INTO merchants (name) VALUES (?)", (name,)).lastrowid
        return self.by_name[name]

    def resolve(self, party: str) -> tuple[str, int | None, str | None]:
        """Returns (key, merchant_id, merchant_name); empty party -> no merchant."""
        key = clean_key(party)
        if not key:
            return "", None, None
        if key in self.alias:
            mid, name = self.alias[key]
            return key, mid, name
        name = brand_for(key)
        if name is None:
            match = next((n for k, (_, n) in self.alias.items() if same_merchant(key, k)), None)
            name = match or display_name(key)
        mid = self.merchant_id(name)
        self.con.execute("INSERT OR REPLACE INTO merchant_aliases (alias, merchant_id) VALUES (?, ?)", (key, mid))
        self.alias[key] = (mid, name)
        return key, mid, name
