import atexit
import socket

_original_connect = socket.socket.connect


def _loopback_only(sock, address):
    host = address[0]
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise RuntimeError("tests forbid non-loopback network access")
    return _original_connect(sock, address)


def _restore():
    if socket.socket.connect is _loopback_only:
        socket.socket.connect = _original_connect


socket.socket.connect = _loopback_only
atexit.register(_restore)
