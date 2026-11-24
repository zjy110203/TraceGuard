"""Port and HTTP-server helpers for tests.

R5-4.10 port-allocation contract
--------------------------------
The previous helper asked the OS for a free port, **closed** the socket, and
only then let servers (uvicorn, http.server) bind that number. Between the
``close`` and the ``bind`` the port belongs to nobody, so another process — or
another test running concurrently — can claim it. That is a textbook TOCTOU
race and the reason ``OSError: [Errno 98/10048] Address already in use`` kept
reappearing under parallel gate runs.

The fix is not "retry harder": it is to never release the port in the first
place. :func:`bind_ephemeral` returns a socket that is *already bound and
listening* on an OS-assigned port, and :func:`serve_uvicorn` hands that exact
socket to uvicorn via ``Server.run(sockets=[...])``. The server therefore binds
the port the OS gave it, with no window for anyone else to take it.
"""

from __future__ import annotations

import http.server
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

# How long a test server may take to report itself started / stopped. Generous
# enough for a cold interpreter, bounded so a hang is a failure not a wait.
START_TIMEOUT_S = 30.0
STOP_TIMEOUT_S = 15.0


def bind_ephemeral(host: str = "127.0.0.1") -> socket.socket:
    """Return a listening socket on an OS-assigned port (never released).

    The caller owns the socket and must close it. Hand it to the real server
    instead of passing a bare port number: keeping the descriptor open for the
    whole hand-off is what removes the bind race.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        # Windows has no SO_REUSEPORT and SO_REUSEADDR there means "steal a
        # port in TIME_WAIT", so it is only set where it is actually about
        # rebinding safety (POSIX), keeping Windows semantics unchanged.
        import os

        if os.name != "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, 0))
        sock.listen(128)
        sock.set_inheritable(True)
    except BaseException:
        sock.close()
        raise
    return sock


def socket_port(sock: socket.socket) -> int:
    """The port an already-bound socket is listening on."""
    return int(sock.getsockname()[1])


def free_port() -> int:
    """Port that was free a moment ago.

    Kept only for call sites that genuinely cannot share a socket (a subprocess
    that must bind by itself). New test code should use
    :func:`reserve_port` / :func:`serve_uvicorn`; those are race-free.
    """
    sock = bind_ephemeral()
    try:
        return socket_port(sock)
    finally:
        sock.close()


# Module-level ``TF_PORT = free_port()`` was the worst instance of the race: the
# socket is closed at import, so the very next ``free_port()`` call in another
# test module is likely to be handed the SAME number by the OS (it reuses the
# most recently freed port). Two modules then believe they own one port.
# ``reserve_port`` keeps the descriptor open for the whole hand-off.
_RESERVED: dict[int, socket.socket] = {}
_RESERVED_LOCK = threading.Lock()


def reserve_port(host: str = "127.0.0.1") -> int:
    """Reserve a port by holding its socket open until :func:`take_reserved`.

    The returned number cannot be taken by anyone else, so a module-level
    ``TF_PORT = reserve_port()`` is safe even though the server only starts
    later in a fixture.
    """
    sock = bind_ephemeral(host)
    port = socket_port(sock)
    with _RESERVED_LOCK:
        _RESERVED[port] = sock
    return port


def take_reserved(port: int) -> list[socket.socket]:
    """Hand a reserved socket to a server (``server.run(sockets=...)``).

    The socket leaves the registry: the server now owns it. If the port was
    never reserved (a plain ``free_port()`` call), raise instead of silently
    starting a racy server.
    """
    with _RESERVED_LOCK:
        sock = _RESERVED.pop(port, None)
    if sock is None:
        raise RuntimeError(
            f"port {port} was not reserved with reserve_port(); "
            "a server must be handed its own bound socket"
        )
    return [sock]


def release_reserved(port: int) -> None:
    """Close a reservation that no server ever consumed (test teardown)."""
    with _RESERVED_LOCK:
        sock = _RESERVED.pop(port, None)
    if sock is not None:
        try:
            sock.close()
        except OSError:
            pass


def reserved_ports() -> list[int]:
    """Ports currently held open — used by tests to prove teardown released them."""
    with _RESERVED_LOCK:
        return sorted(_RESERVED)


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


def _wait_started(server: Any, thread: threading.Thread, what: str) -> None:
    deadline = time.monotonic() + START_TIMEOUT_S
    while time.monotonic() < deadline:
        if getattr(server, "started", False):
            return
        if not thread.is_alive():
            raise RuntimeError(f"{what} thread exited before it started")
        time.sleep(0.02)
    raise RuntimeError(f"{what} did not start within {START_TIMEOUT_S}s")


def stop_uvicorn(server: Any, thread: threading.Thread, what: str = "uvicorn") -> None:
    """Stop a server started by :func:`serve_uvicorn` and prove the thread died.

    A silently surviving thread keeps the port and the event loop alive, which
    is how a "passed" suite starts failing on the *next* run.
    """
    server.should_exit = True
    thread.join(timeout=STOP_TIMEOUT_S)
    if thread.is_alive():
        # Second chance: force the loop to unwind, then fail loudly.
        server.force_exit = True
        thread.join(timeout=5)
    assert not thread.is_alive(), f"{what} did not stop within {STOP_TIMEOUT_S}s"


@contextmanager
def serve_uvicorn(
    app: Any,
    *,
    host: str = "127.0.0.1",
    log_level: str = "warning",
) -> Iterator[str]:
    """Run ``app`` on a pre-bound ephemeral socket; yield its base URL.

    ``app`` is either an ASGI application or an import string. The socket is
    bound BEFORE the thread starts and handed to uvicorn, so the port cannot be
    taken by another process between allocation and bind.
    """
    import uvicorn

    sock = bind_ephemeral(host)
    port = socket_port(sock)
    config = uvicorn.Config(app, host=host, port=port, log_level=log_level)
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [sock]},
        daemon=True,
        name="test-uvicorn",
    )
    thread.start()
    try:
        _wait_started(server, thread, "uvicorn")
        yield f"http://{host}:{port}"
    finally:
        try:
            stop_uvicorn(server, thread)
        finally:
            # uvicorn closes the sockets it was handed; closing again is a
            # no-op on POSIX and safe on Windows, and guarantees release.
            try:
                sock.close()
            except OSError:
                pass
