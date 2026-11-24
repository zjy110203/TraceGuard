"""Port and HTTP-server helpers for tests.

Every test server binds an OS-assigned port (``bind(("127.0.0.1", 0))``) and
tears down with ``shutdown()`` + ``server_close()`` + thread join. Fixed ports
were the root cause of the historical CI failure
``OSError: [Errno 98] Address already in use`` in test_network_gateway.
"""

from __future__ import annotations

import http.server
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager


def free_port() -> int:
    """Ask the OS for a currently unused loopback port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def serve_http(handler: type[http.server.BaseHTTPRequestHandler]) -> Iterator[str]:
    """Serve ``handler`` on an ephemeral port; yield its base URL."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="test-http")
    thread.start()
    try:
        yield base
    finally:
        server.shutdown()
        server.server_close()  # release the listening socket before the next test
        thread.join(timeout=5)
        assert not thread.is_alive(), "test HTTP server thread did not stop"
