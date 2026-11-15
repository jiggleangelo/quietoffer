import os
import ssl
from http import cookies
from http.server import ThreadingHTTPServer
from threading import BoundedSemaphore
from urllib.parse import urlsplit

from haggle.app import make_demo_app
from haggle.web import handler_for


class BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, max_workers=16):
        self._slots = BoundedSemaphore(max_workers)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        self._slots.acquire()
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        # Do not emit client addresses, tracebacks, or submitted values.
        pass


def _hosting_settings(listen_host):
    value = os.getenv("PUBLIC_BASE_URL", "").strip()
    public_base_url = ""
    public_host = ""
    if value:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("PUBLIC_BASE_URL must be an http(s) origin without credentials, a path, query, or fragment")
        public_base_url = f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        public_host = parsed.hostname.lower()

    allowed_hosts = {"127.0.0.1", "localhost", "::1"}
    if listen_host not in ("0.0.0.0", "::", ""):
        allowed_hosts.add(listen_host.lower())
    if public_host:
        allowed_hosts.add(public_host)

    secure_cookies = os.getenv("DEMO_HTTPS_TERMINATED", "").lower() == "true"
    return public_base_url, allowed_hosts, secure_cookies


def _hosting_handler(app, listen_host):
    base_handler = handler_for(app)
    public_base_url, allowed_hosts, secure_cookies = _hosting_settings(listen_host)

    class HostingHandler(base_handler):
        _public_base_url = public_base_url
        _allowed_hosts = frozenset(allowed_hosts)
        _secure_cookies = secure_cookies

        def _host_allowed(self):
            raw_host = self.headers.get("Host", "")
            try:
                hostname = urlsplit("//" + raw_host).hostname
            except ValueError:
                return False
            return bool(hostname) and hostname.lower() in self._allowed_hosts

        def _reject_bad_host(self):
            if self._host_allowed():
                return False
            self._send(421, b"Misdirected Request", "text/plain; charset=utf-8")
            return True

        def do_GET(self):
            if not self._reject_bad_host():
                super().do_GET()

        def do_POST(self):
            if not self._reject_bad_host():
                super().do_POST()

        def _base_headers(self, cookie=None):
            super()._base_headers(None)
            if cookie:
                secure = "; Secure" if self._secure_cookies or isinstance(self.connection, ssl.SSLSocket) else ""
                self.send_header(
                    "Set-Cookie",
                    f"hs={cookie}; HttpOnly; SameSite=Strict; Path=/; Max-Age=3600{secure}",
                )

        def _send(self, status, data, mime="text/html; charset=utf-8", cookie=None):
            if self._public_base_url and mime.startswith("text/html"):
                base = self._public_base_url.encode("utf-8")
                data = data.replace(b'href="/', b'href="' + base + b'/')
                data = data.replace(b'src="/', b'src="' + base + b'/')
                data = data.replace(b'action="/', b'action="' + base + b'/')
            super()._send(status, data, mime, cookie)

        def _redirect(self, path, cookie=None):
            if self._public_base_url and path.startswith("/"):
                path = self._public_base_url + path
            super()._redirect(path, cookie)

    return HostingHandler


def main(port: int | None = None, host: str | None = None):
    app = make_demo_app()
    listen_host = host if host is not None else os.getenv("DEMO_HOST", "127.0.0.1")
    listen_port = port if port is not None else int(os.getenv("PORT", "8080"))
    workers = int(os.getenv("HAGGLE_MAX_WORKERS", "16"))
    if workers < 1 or workers > 128:
        raise ValueError("HAGGLE_MAX_WORKERS must be between 1 and 128")
    server = BoundedHTTPServer((listen_host, listen_port), _hosting_handler(app, listen_host), workers)
    server.app = app
    return server


if __name__ == "__main__":
    server = main()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
