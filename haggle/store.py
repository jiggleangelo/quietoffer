import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field


@dataclass
class Negotiation:
    csrf: str = field(default_factory=lambda: secrets.token_hex(16))
    url: str = ""
    title: str = ""
    text: str = ""
    category: str = ""
    asking_cents: int = 0
    rate_variant: str = "r20"
    terms_accepted: bool = False
    card_token: str = ""
    draft: object = None
    outcome: object = None
    fee_quote: object = None
    fee_finalized: bool = False
    charge_id: str = ""
    vault_token: str = ""
    sample_id: str = ""
    marked_sent: bool = False
    lock: object = field(default_factory=threading.RLock, repr=False)


class Store:
    def __init__(self, limit=5000, ttl_seconds=3600):
        self.items = OrderedDict()
        self._touched = {}
        self.limit = limit
        self.ttl_seconds = ttl_seconds
        self.lock = threading.Lock()

    def _purge_locked(self, now):
        expired = [sid for sid, touched in self._touched.items() if now - touched >= self.ttl_seconds]
        for sid in expired:
            self.items.pop(sid, None)
            self._touched.pop(sid, None)
        while len(self.items) > self.limit:
            sid, _ = self.items.popitem(last=False)
            self._touched.pop(sid, None)

    def get_existing(self, sid):
        now = time.monotonic()
        with self.lock:
            self._purge_locked(now)
            item = self.items.get(sid)
            if item is None:
                return None
            self.items.move_to_end(sid)
            self._touched[sid] = now
            return item

    def get(self, sid, variant="r20"):
        existing = self.get_existing(sid)
        if existing is not None:
            return existing
        return self.reset(sid, variant)

    def reset(self, sid, variant="r20"):
        now = time.monotonic()
        with self.lock:
            self._purge_locked(now)
            item = Negotiation(rate_variant=variant)
            self.items[sid] = item
            self.items.move_to_end(sid)
            self._touched[sid] = now
            self._purge_locked(now)
            return item

    def delete(self, sid):
        with self.lock:
            self.items.pop(sid, None)
            self._touched.pop(sid, None)
