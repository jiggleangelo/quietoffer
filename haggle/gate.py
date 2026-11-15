import re
from dataclasses import dataclass


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    category: str | None
    reason: str
    asking_cents: int | None


DENY = {
    "vehicles": ("car", "truck", "suv", "motorcycle", "motorbike", "scooter", "boat", "rv", "trailer", "atv", "vehicle", "vin", "mileage", "clean title", "vehicle title"),
    "firearms_weapons": ("gun", "rifle", "pistol", "handgun", "shotgun", "revolver", "ammo", "ammunition", "firearm", "suppressor", "crossbow"),
    "alcohol_tobacco": ("wine", "beer", "liquor", "whiskey", "whisky", "vape", "cigar", "tobacco", "nicotine"),
    "drugs_medical": ("prescription", "rx", "cbd", "thc", "insulin", "cpap"),
    "real_estate_rentals": ("apartment", "sublet", "lease", "rental", "for rent", "house for sale", "room for rent"),
    "live_animals": ("puppy", "puppies", "kitten", "dog for sale", "cat for sale", "rehome"),
    "tickets": ("ticket", "e ticket", "admission", "event pass"),
}

ALLOW = {
    "electronics": ("monitor", "laptop", "computer", "tablet", "television", "tv", "camera", "speaker", "headphones", "phone"),
    "furniture": ("table", "chair", "desk", "sofa", "couch", "dresser", "bookshelf", "bed frame"),
    "baby_gear": ("stroller", "crib", "high chair", "baby carrier", "playpen", "car seat"),
    "sporting_goods": ("bicycle", "bike", "golf", "kayak", "treadmill", "weights", "tennis"),
    "collectibles": ("collectible", "trading card", "comic", "coin", "figurine", "vinyl record"),
    "home_kitchen": ("mixer", "blender", "cookware", "microwave", "vacuum", "lamp", "rug"),
    "tools": ("drill", "saw", "wrench", "sander", "toolbox", "hammer"),
    "clothing": ("jacket", "shirt", "dress", "shoes", "boots", "jeans", "coat"),
}

EXEMPT_PHRASES = (
    r"\bcar\s+seats?\b",
    r"\bcar\s+chargers?\b",
    r"\btoy\s+cars?\b",
    r"\bwine\s+(?:racks?|glasses?)\b",
    r"\bboat\s+shoes?\b",
    r"\bbeer\s+(?:mugs?|glasses?)\b",
    r"\bcigar\s+box(?:es)?\b",
    r"\b(?:kids?|children(?:'s)?)\s+(?:electric\s+)?scooters?\b",
    r"\btrailer\s+hitch(?:es)?\b",
    r"\bliving\s+rooms?\b",
)


def _norm(value):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (value or "").lower())).strip()


def _term_pattern(term):
    words = term.split()
    last = re.escape(words[-1])
    if words[-1].endswith("y"):
        last = re.escape(words[-1][:-1]) + r"(?:y|ies)"
    else:
        last += r"(?:s|es)?"
    prefix = r"\s+".join(re.escape(word) for word in words[:-1])
    return r"(?<![a-z0-9])" + prefix + (r"\s+" if prefix else "") + last + r"(?![a-z0-9])"


def _has(text, term):
    return re.search(_term_pattern(term), text) is not None


def parse_price(value):
    if value is None:
        return None
    s = value.strip().lower().replace("$", "").replace(",", "")
    match = re.fullmatch(r"(\d+(?:\.\d{1,2})?)(k)?", s)
    if not match:
        return None
    whole, _, fraction = match.group(1).partition(".")
    if match.group(2):
        if len(fraction) > 1:
            return None
        return int(whole) * 100000 + int(fraction or 0) * 10000
    return int(whole) * 100 + int((fraction + "00")[:2])


def check(listing_text, asking_price, title=None, allow_general=False):
    text = _norm(f"{title or ''} {listing_text}")
    deny_text = text
    for pattern in EXEMPT_PHRASES:
        deny_text = re.sub(pattern, " ordinary item ", deny_text)
    for category, words in DENY.items():
        if any(_has(deny_text, word) for word in words):
            return GateResult(False, category, "quietoffer cannot help with this category.", parse_price(asking_price))
    category_text = _norm(title) if title and _norm(title) else _norm(listing_text)
    category = next((name for name, words in ALLOW.items() if any(_has(category_text, word) for word in words)), None)
    if not category and allow_general:
        category = "general"
    if not category:
        return GateResult(False, None, "quietoffer cannot help with this category.", parse_price(asking_price))
    cents = parse_price(asking_price)
    if cents is None:
        return GateResult(False, category, "Enter a valid asking price.", None)
    if cents < 500:
        return GateResult(False, category, "The asking price must be at least $5.", cents)
    return GateResult(True, category, "Allowed", cents)
