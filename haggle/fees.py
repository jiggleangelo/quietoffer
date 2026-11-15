import re
from dataclasses import dataclass
from typing import Literal
from .gate import parse_price

RATES_BPS = {"r10": 1000, "r15": 1500, "r20": 2000}
CAP_CENTS = 5000
MIN_FEE_CENTS = 100

@dataclass(frozen=True)
class Outcome:
    asking_cents: int
    final_cents: int
    seller_response: Literal["accepted", "countered", "declined", "no_reply"]
    proof_text: str = ""
    proof_image_bytes: int = 0
    proof_image_valid: bool = False

@dataclass(frozen=True)
class FeeQuote:
    verified: bool
    savings_cents: int
    fee_cents: int
    reason: str

def valid_image(data: bytes) -> bool:
    return 0 < len(data) <= 49152 and (data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff"))

def _proof_matches(text, target):
    for token in re.findall(r"(?<!\w)\$?\d[\d,]*(?:\.\d{1,2})?", text):
        cents = parse_price(token)
        if cents is not None and abs(cents-target) <= 100:
            return True
    return False

def quote(outcome: Outcome, rate_bps: int) -> FeeQuote:
    if outcome.seller_response not in ("accepted", "countered"):
        return FeeQuote(False, 0, 0, "No deal, no fee.")
    if outcome.final_cents <= 0 or outcome.final_cents >= outcome.asking_cents:
        return FeeQuote(False, 0, 0, "The final price must be below the asking price.")
    if not (_proof_matches(outcome.proof_text[:2000], outcome.final_cents) or outcome.proof_image_valid):
        return FeeQuote(False, 0, 0, "Add proof of the agreed price.")
    savings = outcome.asking_cents-outcome.final_cents
    fee = min(CAP_CENTS, savings*rate_bps//10000)
    if fee < MIN_FEE_CENTS:
        return FeeQuote(True, savings, 0, "Savings too small to charge.")
    return FeeQuote(True, savings, fee, "Verified from the proof provided.")
