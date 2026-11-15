import socket
import threading
import unittest

from haggle.drafter import DISCLOSURE, Draft, DraftRequest, FakeDrafter, LLMDrafter, ensure_safe
from haggle.fees import Outcome, quote, valid_image
from haggle.payments import FakePayments, StripePayments
from haggle.vault import InMemoryVault


class CoreTests(unittest.TestCase):
    def test_draft(self):
        draft = FakeDrafter().draft(DraftRequest("electronics", 24000, "Monitor", "", 15))
        self.assertLess(draft.offer_cents, draft.walkaway_cents)
        self.assertLess(draft.walkaway_cents, 24000)
        for message in (draft.opening, draft.counter_if_countered, draft.counter_if_declined):
            self.assertTrue(message.endswith(DISCLOSURE))

    def test_guard_and_stub(self):
        source = "visit https://bad.test or a@b.com 212-555-1212 lowball " + "x" * 1000
        draft = ensure_safe(Draft(source, source, source, 1, 2, False))
        for message in (draft.opening, draft.counter_if_countered, draft.counter_if_declined):
            self.assertNotIn("bad.test", message)
            self.assertNotIn("lowball", message.lower())
            self.assertLessEqual(len(message), 600)
            self.assertTrue(message.endswith(DISCLOSURE))
        with self.assertRaises(NotImplementedError):
            LLMDrafter().draft(DraftRequest("x", 1000, "x", ""))

    def test_fees(self):
        result = quote(Outcome(20000, 17000, "countered", "Deal at $170"), 2000)
        self.assertEqual((result.savings_cents, result.fee_cents), (3000, 600))
        self.assertEqual(quote(Outcome(200000, 100000, "accepted", "$1,000"), 2000).fee_cents, 5000)
        waived = quote(Outcome(1000, 950, "accepted", "$9.50"), 1000)
        self.assertTrue(waived.verified)
        self.assertEqual(waived.fee_cents, 0)
        self.assertEqual(waived.reason, "Savings too small to charge.")
        self.assertFalse(quote(Outcome(20000, 17000, "declined", "$170"), 2000).verified)
        self.assertFalse(quote(Outcome(20000, 17000, "accepted", "$160"), 2000).verified)
        self.assertTrue(valid_image(b"\x89PNG\r\n\x1a\nabc"))
        self.assertFalse(valid_image(b"GIF89a"))

    def test_payments_are_concurrently_idempotent(self):
        payments = FakePayments()
        token = payments.authorize_card_on_file("abc")
        results = []
        threads = [
            threading.Thread(target=lambda: results.append(payments.charge(token, 100, "one")))
            for _ in range(20)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(payments.charges), 1)
        self.assertEqual(len({result.charge_id for result in results}), 1)
        with self.assertRaises(ValueError):
            payments.charge(token, 200, "one")
        with self.assertRaises(ValueError):
            payments.charge(token, 0, "two")
        with self.assertRaises(NotImplementedError):
            StripePayments().authorize_card_on_file("x")

    def test_vault_retains_no_plaintext(self):
        vault = InMemoryVault()
        address = "unique-secret-address@example.test"
        first = vault.put("email", address)
        second = vault.put("email", address)
        self.assertNotEqual(first, second)
        self.assertNotIn(address, repr(vault.__dict__))
        for name in ("get", "read", "list", "export", "__getitem__"):
            self.assertFalse(hasattr(vault, name))

    def test_non_loopback_network_is_forbidden(self):
        sock = socket.socket()
        try:
            with self.assertRaises(RuntimeError):
                sock.connect(("192.0.2.1", 80))
        finally:
            sock.close()
