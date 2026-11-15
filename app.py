import os
import threading
from http.server import ThreadingHTTPServer
from threading import BoundedSemaphore

from haggle.uweb import handler_for, hosting_settings
from haggle.wstore import WorkspaceStore


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
        pass


def main(port=None, host=None):
    listen_host = host if host is not None else os.getenv("APP_HOST", "127.0.0.1")
    listen_port = port if port is not None else int(os.getenv("PORT", "8080"))
    public = os.getenv("PUBLIC_BASE_URL", "https://app-quietoffer.manykiln.com")
    secure = os.getenv("APP_HTTPS_TERMINATED", "").lower() == "true"
    data_dir = os.getenv("DATA_DIR", "./data")
    try:
        cap_mb = int(os.getenv("APP_DATA_CAP_MB", "200"))
    except ValueError as error:
        raise ValueError("APP_DATA_CAP_MB must be a positive integer") from error
    if cap_mb < 1:
        raise ValueError("APP_DATA_CAP_MB must be a positive integer")
    settings = hosting_settings(public, listen_host, secure)
    store = WorkspaceStore(data_dir, cap_mb * 1024 * 1024)
    store.sweep()
    server = BoundedHTTPServer((listen_host, listen_port), handler_for(store, settings), 16)
    server.store = store

    def sweep_loop():
        while not server._BaseServer__shutdown_request:
            if server._BaseServer__is_shut_down.wait(86400):
                return
            store.sweep()

    threading.Thread(target=sweep_loop, name="quietoffer-sweep", daemon=True).start()
    return server


if __name__ == "__main__":
    server = main()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
