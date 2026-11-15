import http.client
import socket
import threading
import unittest
import urllib.parse

import demo


class DemoTests(unittest.TestCase):
    def setUp(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        self.server = demo.main(port, "127.0.0.1")
        self.port = port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cookie = ""

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def request(self, method, path, fields=None, headers=None):
        body = None
        request_headers = dict(headers or {})
        if fields is not None:
            body = urllib.parse.urlencode(fields)
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        if self.cookie:
            request_headers["Cookie"] = self.cookie
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(method, path, body, request_headers)
        response = connection.getresponse()
        data = response.read().decode("utf-8", "replace")
        if response.getheader("Set-Cookie"):
            self.cookie = response.getheader("Set-Cookie").split(";", 1)[0]
        result = response.status, dict(response.getheaders()), data
        connection.close()
        return result

    @staticmethod
    def csrf(body):
        marker = 'name="csrf" value="'
        return body.split(marker, 1)[1].split('"', 1)[0]

    def home_token(self):
        return self.csrf(self.request("GET", "/")[2])

    def complete_charged_flow(self):
        token = self.home_token()
        status, headers, _ = self.request("POST", "/listing", {"csrf": token, "sample_id": "s1"})
        self.assertEqual((status, headers["Location"]), (303, "/terms"))
        terms = self.request("GET", "/terms")[2]
        token = self.csrf(terms)
        self.request("POST", "/terms", {"csrf": token, "accept": "yes"})
        draft = self.request("GET", "/draft")[2]
        token = self.csrf(draft)
        self.request("POST", "/sent", {"csrf": token})
        outcome = self.request("GET", "/outcome")[2]
        token = self.csrf(outcome)
        self.request(
            "POST",
            "/outcome",
            {
                "csrf": token,
                "seller_response": "countered",
                "final_price": "150",
                "proof_text": "Ok, $150 works",
            },
        )
        fee = self.request("GET", "/fee")[2]
        self.assertIn("$6.00", fee)
        token = self.csrf(fee)
        self.request("POST", "/fee", {"csrf": token})
        return token

    def test_health_headers_errors_and_limits(self):
        self.assertEqual(self.request("GET", "/healthz")[::2], (200, "ok"))
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("Demo operated by AI agents", body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("<style>", body)
        missing_status, _, missing_body = self.request("GET", "/missing")
        self.assertEqual(missing_status, 404)
        self.assertIn("Demo operated by AI agents", missing_body)
        connection = http.client.HTTPConnection("127.0.0.1", self.port)
        connection.request("POST", "/listing", b"x" * 65537, {"Content-Length": "65537"})
        response = connection.getresponse()
        self.assertEqual(response.status, 413)
        self.assertIn("Demo operated", response.read().decode())
        connection.close()
        self.assertEqual(self.request("POST", "/listing", {})[0], 403)

    def test_out_of_order_posts_are_rejected(self):
        token = self.home_token()
        for path in ("/terms", "/draft", "/sent", "/outcome", "/fee", "/done"):
            with self.subTest(path=path):
                self.assertIn(self.request("POST", path, {"csrf": token})[0], (403, 409))

    def test_full_flow_idempotent_charge_and_state_reset(self):
        token = self.complete_charged_flow()
        self.request("POST", "/fee", {"csrf": token})
        self.assertEqual(len(self.server.app.payments.charges), 1)
        self.assertIn("fake_ch_", self.request("GET", "/done")[2])

        home = self.request("GET", "/")[2]
        self.request("POST", "/listing", {"csrf": self.csrf(home), "sample_id": "s2"})
        self.assertEqual(self.request("GET", "/draft")[1]["Location"], "/terms")

    def test_finalized_negotiation_rejects_state_changing_replays(self):
        token = self.complete_charged_flow()
        # Snapshot both keys and charge records for a like-for-like comparison.
        charges = dict(self.server.app.payments.charges)
        self.assertEqual(len(charges), 1)

        replay_forms = {
            "/terms": {"csrf": token, "accept": "yes"},
            "/draft": {"csrf": token, "discount": "20"},
            "/sent": {"csrf": token},
            "/outcome": {
                "csrf": token,
                "seller_response": "countered",
                "final_price": "100",
                "proof_text": "Ok, $100 works",
            },
        }
        for path, fields in replay_forms.items():
            with self.subTest(path=path):
                status, _, body = self.request("POST", path, fields)
                self.assertEqual(status, 409)
                self.assertIn("already been finalized", body)
                self.assertEqual(self.server.app.payments.charges, charges)

        self.request("POST", "/fee", {"csrf": token})
        self.assertEqual(self.server.app.payments.charges, charges)
        self.assertIn("fake_ch_", self.request("GET", "/done")[2])

    def test_repeated_fee_posts_preserve_charge_records(self):
        token = self.complete_charged_flow()
        charges = dict(self.server.app.payments.charges)
        self.assertEqual(len(charges), 1)

        for replay in range(3):
            with self.subTest(replay=replay):
                self.request("POST", "/fee", {"csrf": token})
                self.assertEqual(self.server.app.payments.charges, charges)
                self.assertIn("fake_ch_", self.request("GET", "/done")[2])

    def test_blocked_input_is_not_echoed(self):
        token = self.home_token()
        payload = "<script>alert(123456789)</script>"
        _, _, body = self.request(
            "POST",
            "/listing",
            {"csrf": token, "title": payload, "text": "mystery", "asking": "10"},
        )
        self.assertNotIn("123456789", body)
        self.assertNotIn("<script>alert", body)

    def test_vehicle_blocked(self):
        token = self.home_token()
        _, _, body = self.request("POST", "/listing", {"csrf": token, "sample_id": "s4"})
        self.assertIn("not supported", body.lower())
