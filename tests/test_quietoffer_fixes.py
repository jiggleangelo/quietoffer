import http.client
import json
import os
import socket
import tempfile
import threading
import unittest
import urllib.parse
from unittest.mock import patch

import app
from haggle.planner import next_counter, plan_item, summary
from haggle.wstore import WorkspaceStore


class StoreFixTests(unittest.TestCase):
    def test_unknown_tokens_do_not_accumulate_locks(self):
        with tempfile.TemporaryDirectory() as root:
            store = WorkspaceStore(root)
            for number in range(1000):
                self.assertIsNone(store.read("x" + str(number)))
            self.assertEqual(len(store._locks), 0)

    def test_read_modify_write_is_atomic(self):
        with tempfile.TemporaryDirectory() as root:
            store = WorkspaceStore(root)
            token = store.create()
            threads = []
            for _ in range(40):
                def increment():
                    def mutation(data):
                        data["count"] = data.get("count", 0) + 1
                    store.update(token, mutation)
                threads.append(threading.Thread(target=increment))
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(store.read(token)["count"], 40)

    def test_usage_is_cached_after_startup(self):
        with tempfile.TemporaryDirectory() as root:
            store = WorkspaceStore(root)
            token = store.create()
            with patch("haggle.wstore.os.walk", side_effect=AssertionError("full scan")):
                store.update(token, lambda data: data.update({"marker": "saved"}))


class PlannerFixTests(unittest.TestCase):
    def test_closed_without_deal_is_not_open_or_counted(self):
        totals = summary([
            {"status": "in_progress", "asking_cents": 10000},
            {"status": "closed", "asking_cents": 20000, "final_cents": 15000},
            {"status": "closed_no_deal", "asking_cents": 90000, "final_cents": None},
        ])
        self.assertEqual(totals, {"open": 1, "closed": 1, "asked": 30000, "paid": 15000, "saved": 5000})

    def test_next_counter_uses_the_supplied_last_offer(self):
        self.assertEqual(next_counter(9000, 12000, 15000), 12000)
        self.assertEqual(next_counter(11000, 12000, 15000), 12000)
        self.assertEqual(next_counter(11000, 12000, 12000), "accept")

    def test_templates_do_not_reveal_ceiling_or_claim_cash(self):
        item = plan_item("Lamp", "Brass lamp", "100", 20)
        self.assertNotIn("cash-equivalent", item["messages"]["opening"])
        self.assertNotIn("$", item["messages"]["countered"])


class HttpFixTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()
        self.env = patch.dict(os.environ, {"DATA_DIR": self.temp.name, "PUBLIC_BASE_URL": "https://app-quietoffer.manykiln.com"})
        self.env.start()
        self.server = app.main(self.port, "127.0.0.1")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.env.stop()
        self.temp.cleanup()

    def request(self, method, path, fields=None, body=None, headers=None):
        request_headers = {"Host": "app-quietoffer.manykiln.com"}
        request_headers.update(headers or {})
        if method == "POST":
            request_headers.setdefault("Origin", "https://app-quietoffer.manykiln.com")
        if fields is not None:
            body = urllib.parse.urlencode(fields)
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=8)
        connection.request(method, path, body, request_headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def workspace(self):
        return self.request("POST", "/new", {})[1]["Location"]

    def upload(self, path, value):
        boundary = "quietoffer-boundary"
        payload = json.dumps(value).encode()
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"import\"; filename=\"export.json\"\r\nContent-Type: application/json\r\n\r\n").encode() + payload + f"\r\n--{boundary}--\r\n".encode()
        return self.request("POST", path, body=body, headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})

    def test_pages_disclose_retention_and_use_external_assets(self):
        status, headers, body = self.request("GET", "/")
        text = body.decode()
        self.assertEqual(status, 200)
        self.assertIn("expire after 24 hours", text)
        self.assertNotIn("<style>", text)
        self.assertNotIn("oninput=", text)
        self.assertNotIn("'unsafe-inline'", headers["Content-Security-Policy"])

    def test_export_has_security_headers(self):
        workspace = self.workspace()
        status, headers, _ = self.request("GET", workspace + "/export")
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_url_label_is_scrubbed_and_visible(self):
        workspace = self.workspace()
        status, headers, _ = self.request("POST", workspace + "/items", {
            "title": "Lamp", "notes": "Brass lamp", "asking": "80", "discount": "15",
            "url_label": "seller@example.com 415-555-0199 https://market.test/item/1 favorite listing",
        })
        self.assertEqual(status, 303)
        page = self.request("GET", headers["Location"])[2].decode()
        self.assertIn("favorite listing", page)
        self.assertNotIn("seller@example.com", page)
        self.assertNotIn("415-555-0199", page)
        self.assertNotIn("market.test", page)

    def test_malformed_import_returns_an_error(self):
        workspace = self.workspace()
        response = self.upload(workspace + "/import", {"version": 1, "items": ["not an item"]})
        self.assertEqual(response[0], 400)
        self.assertIn(b"Invalid import", response[2])

    def test_import_is_idempotent_and_preserves_closed_no_deal(self):
        workspace = self.workspace()
        exported = {
            "version": 1,
            "items": [{
                "id": "abcdef12", "title": "Lamp", "notes": "Brass lamp", "asking_cents": 8000,
                "discount": 15, "template_note": False, "url_label": "person@example.com label",
                "status": "closed_no_deal", "final_cents": None,
                "log": [{"ts": 10**100, "kind": "countered", "price_cents": -500, "note": "none"}],
            }],
        }
        self.assertEqual(self.upload(workspace + "/import", exported)[0], 200)
        self.assertEqual(self.upload(workspace + "/import", exported)[0], 200)
        data = json.loads(self.request("GET", workspace + "/export")[2])
        self.assertEqual(len(data["items"]), 1)
        item = data["items"][0]
        self.assertEqual(item["status"], "closed_no_deal")
        self.assertIsNone(item["log"][0]["price_cents"])
        self.assertLess(item["log"][0]["ts"], 10**12)
        self.assertEqual(item["url_label"], "label")
        dashboard = self.request("GET", workspace)[2].decode()
        self.assertIn("In progress<br><strong>0</strong>", dashboard)


if __name__ == "__main__":
    unittest.main()
