import hashlib
import hmac
import secrets
import threading
import time
from collections import OrderedDict
from typing import Literal, Protocol


class Vault(Protocol):
    def put(self, kind: Literal["email", "contact"], value: str) -> str: ...


class _WriteOnlyVault:
    def __init__(self, limit=1000, ttl_seconds=3600):
        self._key = secrets.token_bytes(32)
        self._records = OrderedDict()
        self._limit = limit
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()

    def put(self, kind, value):
        if kind not in ("email", "contact"):
            raise ValueError("unsupported value")
        if not isinstance(value, str) or not value or len(value) > 254:
            raise ValueError("invalid value")
        digest = hmac.new(
            self._key,
            kind.encode("ascii") + b"\0" + value.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        token = "v_" + secrets.token_hex(12)
        now = time.monotonic()
        with self._lock:
            expired = [key for key, (_, deadline) in self._records.items() if deadline <= now]
            for key in expired:
                self._records.pop(key, None)
            self._records[token] = (digest, now + self._ttl_seconds)
            while len(self._records) > self._limit:
                self._records.popitem(last=False)
        return token



def InMemoryVault(limit=1000, ttl_seconds=3600):
    return _WriteOnlyVault(limit, ttl_seconds)
