"""Run an ASGI app on a loopback port for as long as a test needs it.

The suite serves the app itself rather than pointing at a deployed host,
because the point of a pre-merge circuit test is to exercise the code on the
branch. It goes through `api.app.create_app` -- the seam `api/server.py` is
built around -- and never imports `api.server`, which reads the environment
and reaches algod at import time.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import uvicorn

STARTUP_TIMEOUT_SECONDS = 20
SHUTDOWN_TIMEOUT_SECONDS = 10


@contextmanager
def serve(build_app: Callable[[str], Any]) -> Iterator[str]:
    """Serve `build_app(base_url)` on an ephemeral loopback port.

    The app is built *from* the URL rather than the other way round: the
    route's advertised `resource` has to carry the port, and the port is not
    known until something has bound it. Binding here and handing uvicorn the
    open socket closes the window in which another process could take that
    port between our probe and uvicorn's own bind.

    A loopback resource is also the honest value, and it has a property worth
    keeping: the facilitator never catalogues one, so this suite leaves no
    directory row and attaches to no merchant identity however often it runs.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    base_url = f"http://127.0.0.1:{sock.getsockname()[1]}"

    server = uvicorn.Server(
        uvicorn.Config(build_app(base_url), log_level="warning", lifespan="off")
    )
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, daemon=True
    )
    thread.start()

    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError(
            f"the resource server did not start within {STARTUP_TIMEOUT_SECONDS}s"
        )

    try:
        yield base_url
    finally:
        server.should_exit = True
        thread.join(timeout=SHUTDOWN_TIMEOUT_SECONDS)
