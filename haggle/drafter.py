import re
from dataclasses import dataclass, replace
from typing import Callable, Protocol

DISCLOSURE = "(Drafted with help from an AI assistant; sent and reviewed by me.)"
MAX_MESSAGE_CHARS = 600


@dataclass(frozen=True)
class DraftRequest:
    category: str
    asking_cents: int
    item_title: str
    condition_notes: str
    target_discount_pct: int = 15


@dataclass(frozen=True)
class Draft:
    opening: str
    counter_if_countered: str
    counter_if_declined: str
    offer_cents: int
    walkaway_cents: int
    requires_review: bool


class Drafter(Protocol):
    def draft(self, req: DraftRequest) -> Draft: ...


def _money(cents):
    dollars, remainder = divmod(cents, 100)
    return f"${dollars:,}" if remainder == 0 else f"${dollars:,}.{remainder:02d}"


def _friendly(cents):
    step = 500 if cents < 10000 else 1000 if cents <= 50000 else 2500
    return max(100, cents // step * step)


def ensure_safe(draft: Draft) -> Draft:
    banned = re.compile(r"\blowball\b|final\s+final|\bthreat\w*\b|I(?:'m| am) a dealer", re.I)

    def clean(value):
        value = str(value)
        value = re.sub(r"https?://\S+|www\.\S+", "[removed]", value, flags=re.I)
        value = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "[removed]", value)
        value = re.sub(
            r"(?<!\d)(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]?\d{3}[-. ]?\d{4}(?!\d)",
            "[removed]",
            value,
        )
        value = banned.sub("", value)
        value = value.replace(DISCLOSURE, "")
        value = re.sub(r"\s+", " ", value).strip()
        suffix = " " + DISCLOSURE
        return value[: MAX_MESSAGE_CHARS - len(suffix)].rstrip() + suffix

    return replace(
        draft,
        opening=clean(draft.opening),
        counter_if_countered=clean(draft.counter_if_countered),
        counter_if_declined=clean(draft.counter_if_declined),
    )


class FakeDrafter:
    def draft(self, req):
        if req.asking_cents < 500:
            raise ValueError("asking price must be at least $5")
        discount = min(30, max(5, int(req.target_discount_pct)))
        offer = min(req.asking_cents - 2, _friendly(req.asking_cents * (100 - discount) // 100))
        offer = max(1, offer)
        walkaway = min(req.asking_cents - 1, _friendly(req.asking_cents * (200 - discount) // 200))
        if walkaway <= offer:
            walkaway = min(req.asking_cents - 1, offer + max(1, min(500, req.asking_cents - offer - 1)))
        title = req.item_title[:120] or "item"
        draft = Draft(
            f"Hi! I'm interested in the {title}. Would you consider {_money(offer)}? "
            "I can arrange pickup at a convenient time.",
            "Thanks for the counter. I'm open to finding a fair middle ground if that works for you.",
            f"Thanks for letting me know. My {_money(offer)} offer remains open if you reconsider.",
            offer,
            walkaway,
            False,
        )
        return ensure_safe(draft)


class LLMDrafter:
    def __init__(self, transport: Callable[[dict], dict] | None = None):
        self.transport = transport or self._offline

    @staticmethod
    def _offline(_payload):
        raise NotImplementedError("no network in MVP")

    def draft(self, req):
        data = self.transport(req.__dict__)
        return ensure_safe(Draft(**data))
