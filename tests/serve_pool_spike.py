"""Serve the pooled route on a fixed loopback port, for the browser spike.

A runbook script, not a test. Named so pytest's default `python_files`
pattern does not collect it, in the same spirit as the `*_join_group.py`
spike scripts beside it.

Why this exists rather than `uvicorn api.server:app`: `api/server.py` refuses
to boot against a loopback host. Every route's advertised `resource` must be
https and must start with PUBLIC_HOST, which is correct for a deployment and
impossible for `http://127.0.0.1`. The suite has always gone around it
through the `api.app.create_app` seam, and so does this.

Why not `tests/e2e/serve.py`: that binds an ephemeral port, deliberately --
the app is built *from* the URL, because the advertised resource has to carry
the port. A browser needs the port to be knowable in advance so a dev-server
proxy can be pointed at it, so the socket is bound to a chosen port here.
Twenty lines duplicated rather than a signature changed, because `tests/e2e/`
belongs to another working copy today.

All three pool parameters are required and none is inferred: the route serves
an agreement only when its seat count and share price match what is
configured, so a guessed default would answer "no pool open" against a pool
that is sitting there open.

    python -m tests.serve_pool_spike \
        --agreement-id 25 --seats 5 --share-price-micro 100000

Read-only with respect to the chain: it creates no agreement and signs
nothing until a buyer pays. Creating pools is `agents/create_pool.py`.
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading
import time
from typing import Any

import uvicorn
from algosdk.v2client import algod as algod_client
from dotenv import dotenv_values

from api.app import create_app
from api.deps import Deps
from api.escrow import EscrowClient
from api.jobs import JobStore
from api.routes import build_routes
from tests.e2e.harness import (
    ENV_PATH,
    Account,
    Env,
    algod_url_from,
    build_settings,
)

STARTUP_TIMEOUT_SECONDS = 20

# Not 8000: another working copy may already be serving something there, and
# a spike that silently talks to the wrong process is worse than one that
# fails to bind.
DEFAULT_PORT = 8402


def build(env: Env, algod: Any, admin: Account, base_url: str, job_db_path: str):
    """The app, assembled exactly as production assembles it."""
    settings = build_settings(env, base_url, job_db_path)
    deps = Deps(
        settings=settings,
        escrow=EscrowClient(
            algod,
            app_id=settings.app_id,
            app_account=settings.app_account,
            usdc_asa_id=settings.usdc_asa_id,
            admin_address=admin.address,
            admin_signer=admin.atc_signer,
        ),
        jobs=JobStore(settings.job_db_path),
        algod=algod,
        http=None,
    )
    return create_app(deps, build_routes(deps))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agreement-id", type=int, required=True)
    parser.add_argument("--seats", type=int, required=True)
    parser.add_argument("--share-price-micro", type=int, required=True)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--job-db-path", default="spike-jobs.sqlite3")
    args = parser.parse_args(argv)

    # The overrides are folded in before they reach the loader, never after: a
    # plain dict copies the mnemonics out of the redacting mapping, and a
    # ConfigError raised downstream would then render every one of them into
    # the traceback.
    env = Env(
        {
            **dotenv_values(ENV_PATH),
            "INDEX_AGREEMENT_ID": str(args.agreement_id),
            "INDEX_SEATS": str(args.seats),
            "INDEX_SHARE_PRICE_MICRO": str(args.share_price_micro),
        }
    )

    admin_phrase = env.get("ADMIN_MNEMONIC")
    if not admin_phrase:
        print("ADMIN_MNEMONIC is not set in .env", file=sys.stderr)
        return 2

    algod = algod_client.AlgodClient(env.get("ALGOD_TOKEN") or "", algod_url_from(env))
    admin = Account(admin_phrase)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", args.port))
    base_url = f"http://127.0.0.1:{args.port}"

    app = build(env, algod, admin, base_url, args.job_db_path)
    server = uvicorn.Server(uvicorn.Config(app, log_level="info", lifespan="off"))
    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, daemon=True
    )
    thread.start()

    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        print("the resource server did not start", file=sys.stderr)
        return 1

    print(f"serving {base_url}/index for agreement {args.agreement_id}")
    print(f"  {args.seats} seats at {args.share_price_micro} micro-USDC each")
    print("  point the vite dev proxy here; ctrl-c to stop")
    try:
        while thread.is_alive():
            time.sleep(0.5)
    except KeyboardInterrupt:
        server.should_exit = True
        thread.join(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
