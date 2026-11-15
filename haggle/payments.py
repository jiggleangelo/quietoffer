import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ChargeResult:
    charge_id: str
    amount_cents: int


class Payments(Protocol):
    def authorize_card_on_file(self, session_id: str) -> str: ...
    def charge(self, token: str, amount_cents: int, idempotency_key: str) -> ChargeResult: ...


class FakePayments:
    def __init__(self, token_limit=10000):
        self.charges = {}
        self._charge_tokens = {}
        self._tokens = OrderedDict()
        self._token_limit = token_limit
        self._lock = threading.Lock()

    def authorize_card_on_file(self, session_id, card="4242"):
        if card != "4242":
            raise ValueError("Use demo card 4242")
        token = "fake_tok_" + secrets.token_hex(16)
        with self._lock:
            self._tokens[token] = session_id
            self._tokens.move_to_end(token)
            while len(self._tokens) > self._token_limit:
                self._tokens.popitem(last=False)
        return token

    def charge(self, token, amount_cents, idempotency_key):
        if amount_cents <= 0 or not idempotency_key:
            raise ValueError("invalid fake charge")
        with self._lock:
            if token not in self._tokens:
                raise ValueError("invalid fake charge")
            existing = self.charges.get(idempotency_key)
            if existing is not None:
                if existing.amount_cents != amount_cents:
                    raise ValueError("idempotency key reused with a different amount")
                if self._charge_tokens[idempotency_key] != token:
                    raise ValueError("idempotency key reused with a different token")
                return existing
            result = ChargeResult("fake_ch_" + secrets.token_hex(12), amount_cents)
            self.charges[idempotency_key] = result
            self._charge_tokens[idempotency_key] = token
            return result


class StripePayments:
    def authorize_card_on_file(self, session_id):
        raise NotImplementedError("Stripe is not wired in the MVP")

    def charge(self, token, amount_cents, idempotency_key):
        raise NotImplementedError("Stripe is not wired in the MVP")
