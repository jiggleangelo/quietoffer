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


class AppTests(unittest.TestCase):
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
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request(method, path, body, request_headers)
        response = connection.getresponse()
        data = response.read()
        result = response.status, dict(response.getheaders()), data
        connection.close()
        return result

    def workspace(self):
        status, headers, _ = self.request("POST", "/new", {})
        self.assertEqual(status, 303)
        return headers["Location"]

    def test_create_core_export_delete(self):
        workspace = self.workspace()
        page = self.request("GET", workspace)[2].decode()
        self.assertIn("Bookmark this link", page)
        self.assertIn("manykiln.com/privacy", page)
        fields = {"title": "IKEA MALM dresser", "notes": "6 drawers. Call Jane 415-555-0199 or jane@x.com", "asking": "$140", "discount": "15"}
        status, headers, _ = self.request("POST", workspace + "/items", fields)
        self.assertEqual(status, 303)
        item_path = headers["Location"]
        item_page = self.request("GET", item_path)[2].decode()
        self.assertIn("Opening message", item_page)
        self.assertNotIn("415-555-0199", item_page)
        self.request("POST", item_path + "/log", {"kind": "countered", "price": "130", "note": ""})
        self.request("POST", item_path + "/log", {"kind": "accepted", "price": "125", "note": ""})
        dashboard = self.request("GET", workspace)[2].decode()
        self.assertIn("$15.00", dashboard)
        exported = self.request("GET", workspace + "/export")
        self.assertEqual(exported[0], 200)
        self.assertNotIn("jane@x.com", exported[2].decode())
        json.loads(exported[2])
        self.request("POST", workspace + "/delete", {})
        self.assertEqual(self.request("GET", workspace)[0], 404)

    def test_general_blocking_host_and_origin(self):
        workspace = self.workspace()
        status = self.request("POST", workspace + "/items", {"title": "Vintage typewriter", "notes": "Works", "asking": "100", "discount": "15"})[0]
        self.assertEqual(status, 303)
        status, _, body = self.request("POST", workspace + "/items", {"title": "Rifle", "notes": "rifle", "asking": "100", "discount": "15"})
        self.assertEqual(status, 400)
        self.assertNotIn(b"Rifle", body)
        self.assertEqual(self.request("GET", "/", headers={"Host": "evil.test"})[0], 421)
        self.assertEqual(self.request("POST", "/new", {}, headers={"Origin": "https://evil.test"})[0], 403)
        self.assertNotEqual(self.request("GET", "/")[1]["Referrer-Policy"], "no-referrer")

    def test_persistence_and_token_not_on_disk(self):
        workspace = self.workspace()
        token = workspace.rsplit("/", 1)[1]
        self.request("POST", workspace + "/items", {"title": "Lamp", "notes": "Brass lamp", "asking": "80", "discount": "15"})
        for base, _, files in os.walk(self.temp.name):
            self.assertNotIn(token, base)
            for name in files:
                path = os.path.join(base, name)
                self.assertNotIn(token, name)
                self.assertNotIn(token.encode(), open(path, "rb").read())
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.server = app.main(self.port, "127.0.0.1")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.assertIn("Lamp", self.request("GET", workspace)[2].decode())


if __name__ == "__main__":
    unittest.main()
