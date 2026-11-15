import re

from .drafter import DISCLOSURE, DraftRequest, FakeDrafter
from .gate import check

TEMPLATE_NOTE = "Drafted with a template tool, sent by me."


def scrub(value):
    value = str(value or "")
    value = re.sub(r"https?://\S+|www\.\S+", "", value, flags=re.I)
    value = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "", value)
    value = re.sub(r"(?<!\d)(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}(?!\d)", "", value)
    return re.sub(r"\s+", " ", value).strip()


def friendly(cents):
    step = 500 if cents < 10000 else 1000 if cents <= 50000 else 2500
    return max(100, cents // step * step)


def money(cents):
    dollars, remainder = divmod(int(cents), 100)
    return f"${dollars:,}.{remainder:02d}"


def _message(value, template_note):
    value = scrub(str(value).replace(DISCLOSURE, ""))
    suffix = (" " + TEMPLATE_NOTE) if template_note else ""
    return value[: 600 - len(suffix)].rstrip() + suffix


def plan_item(title, listing_text, asking_price, discount=15, template_note=False):
    title = scrub(title)[:120]
    notes = scrub(listing_text)[:4000]
    try:
        discount = int(discount)
    except (TypeError, ValueError) as error:
        raise ValueError("Discount must be a whole number from 5 to 30.") from error
    if not 5 <= discount <= 30:
        raise ValueError("Discount must be a whole number from 5 to 30.")
    result = check(notes, str(asking_price), title, allow_general=True)
    if not result.allowed:
        raise ValueError(result.reason)
    draft = FakeDrafter().draft(DraftRequest(result.category or "general", result.asking_cents or 0, title, notes, discount))
    return {
        "title": title,
        "notes": notes,
        "category": result.category or "general",
        "asking_cents": result.asking_cents,
        "discount": discount,
        "offer_cents": draft.offer_cents,
        "walkaway_cents": draft.walkaway_cents,
        "messages": {
            "opening": _message(draft.opening, template_note),
            "countered": _message(draft.counter_if_countered, template_note),
            "declined": _message(draft.counter_if_declined, template_note),
        },
        "template_note": bool(template_note),
    }


def next_counter(offer, walkaway, seller_counter):
    offer, walkaway, seller_counter = map(int, (offer, walkaway, seller_counter))
    if seller_counter <= walkaway:
        return "accept"
    return min(walkaway, friendly((offer + seller_counter) // 2))


def summary(items):
    active = [item for item in items if item.get("status") == "in_progress"]
    closed = [item for item in items if item.get("status") == "closed" and int(item.get("final_cents") or 0) > 0]
    counted = active + closed
    asked = sum(int(item.get("asking_cents") or 0) for item in counted)
    paid = sum(int(item.get("final_cents") or 0) for item in closed)
    closed_asked = sum(int(item.get("asking_cents") or 0) for item in closed)
    return {
        "open": len(active),
        "closed": len(closed),
        "asked": asked,
        "paid": paid,
        "saved": max(0, closed_asked - paid),
    }
