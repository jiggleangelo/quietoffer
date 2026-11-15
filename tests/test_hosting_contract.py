import http.client
import os
import socket
import subprocess
import sys
import time
import unittest
from pathlib import Path


class HostingContractTests(unittest.TestCase):
    def test_contract_environment_starts_and_accepts_public_host(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        public_host = "try-quietoffer.manykiln.com"
        env = os.environ.copy()
        env.update(
            {
                "DEMO_HOST": "127.0.0.1",
                "PORT": str(port),
                "PUBLIC_BASE_URL": f"https://{public_host}",
                "DEMO_HTTPS_TERMINATED": "true",
            }
        )
        root = Path(__file__).resolve().parents[1]
        process = subprocess.Popen(
            [sys.executable, "demo.py"],
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            deadline = time.monotonic() + 5
            last_error = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    output = process.stdout.read() if process.stdout else ""
                    self.fail(f"demo exited before becoming healthy: {output}")
                try:
                    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.5)
                    connection.request("GET", "/healthz", headers={"Host": public_host})
                    response = connection.getresponse()
                    body = response.read()
                    connection.close()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(body, b"ok")
                    break
                except OSError as error:
                    last_error = error
                    time.sleep(0.05)
            else:
                self.fail(f"demo did not become healthy: {last_error}")
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            if process.stdout:
                process.stdout.close()


if __name__ == "__main__":
    unittest.main()
