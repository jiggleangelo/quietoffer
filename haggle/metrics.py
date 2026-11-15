import threading
NAMES = {"visit", "paste", "gate_blocked", "terms_accepted", "draft_created", "marked_sent", "outcome_accepted", "outcome_countered", "outcome_declined", "outcome_no_reply", "fee_verified", "fee_unverified", "charged", "fee_waived"}
class Metrics:
    def __init__(self):
        self.counts, self.lock = {name: 0 for name in NAMES}, threading.Lock()
    def incr(self, name):
        if name not in NAMES:
            raise ValueError("unknown metric")
        with self.lock:
            self.counts[name] += 1
    def snapshot(self):
        with self.lock:
            return dict(self.counts)
