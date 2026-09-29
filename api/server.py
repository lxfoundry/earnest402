"""Earnest resource server — the x402 entrypoint for two paid products
sharing one `payTo`.

**POST /pin?sha256=&size=** is the delivery escrow (see
docs/specs/delivery-escrow-backend.md), served only while
`PIN_ROUTE_ENABLED` is on. The unpaid pass creates the agreement and answers
402; the buyer's settlement group carries the join call that records the
payer; the 200 mints the jobId that the bytes are later uploaded against.

**POST /index** is the pool-seat route: it creates nothing. It names
whichever agreement the operator has configured as the open pool
(`INDEX_AGREEMENT_ID`), and answers 503 -- never a payable 402 -- when none
is configured or the configured one is no longer joinable.

This module is the only place that reads the environment or opens a network
connection: it builds the real collaborators and hands them to
api.app.create_app(), which is what a test constructs instead, against fakes.
"""

from __future__ import annotations

from algosdk.v2client import algod as algod_client
from dotenv import load_dotenv

from api.app import create_app
from api.config import load_settings, verify_startup
from api.deps import build_deps
from api.routes import build_routes

load_dotenv()

settings = load_settings()

algod = algod_client.AlgodClient(settings.algod_token, settings.algod_url)

deps = build_deps(settings, algod)

routes = build_routes(deps)

# Before the app exists, so a misconfiguration is a failure to start rather
# than a 402 we cannot honour. The challenge tag is stamped at settlement and
# never reclassified: a first MainNet settlement served from a wrong
# configuration is unrepairable.
verify_startup(settings, routes, algod)

app = create_app(deps, routes)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
