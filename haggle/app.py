import os
from dataclasses import dataclass
from .drafter import FakeDrafter
from .metrics import Metrics
from .payments import FakePayments
from .store import Store
from .vault import InMemoryVault

@dataclass
class App:
    drafter: object
    payments: object
    vault: object
    store: Store
    metrics: Metrics
    rate_variant: str = "r20"
    show_metrics: bool = False

def make_demo_app():
    variant = os.getenv("HAGGLE_RATE_VARIANT", "r20")
    if variant not in ("r10", "r15", "r20"):
        raise ValueError("HAGGLE_RATE_VARIANT must be r10, r15 or r20")
    app = App(FakeDrafter(), FakePayments(), InMemoryVault(), Store(), Metrics(), variant, os.getenv("HAGGLE_SHOW_METRICS") == "1")
    assert isinstance(app.drafter, FakeDrafter) and isinstance(app.payments, FakePayments)
    return app
