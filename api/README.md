# api/

The x402 resource server. Each paid route is an entire purchase, and they
share one `payTo`:

- `POST /index` — one seat in the edition pool the deployment is configured to
  serve (`INDEX_AGREEMENT_ID`). It creates nothing, and answers `503
  no_pool_open` rather than a 402 when that pool is not joinable. Always served.
- `POST /pin?sha256=<64 hex>&size=<bytes>` — settled through the facilitator,
  and **served only while `PIN_ROUTE_ENABLED` is on, which boot refuses on
  MainNet**: its delivery half (upload, leg 2, release) is not built. Off, which
  is the default, it answers 404 and the server holds no signing key. The
  unpaid pass creates the escrow agreement and answers 402; the buyer's
  settlement group carries the join call that records the payer; the 200 mints
  the `jobId` the bytes are later uploaded against. Declares Bazaar discovery
  and the challenge attribution tag.

  A malformed query is a `400` before any agreement is created. A buyer who
  loses the race for the single seat gets a `409` carrying **both** outcomes —
  re-quote, which is a purchase with its own deadline and refund, or attach to
  the winner's job, which costs nothing and guarantees nothing — because the
  two are not equivalent and the difference is not ours to hide.
- `GET /healthz` — unpaid liveness probe, no payment logic.
- `GET /`, `/app`, `/app/*` — the built browser client (`WEB_DIST_DIR`), when
  it exists. Answered by `web.py` before the payment middleware.
- `GET /app/editions/<file>` — each edition's free tier: its free report,
  extract and manifest, copied into the bundle directory by the image and
  answered like any other file there. The image carries the published files
  only, never the sold ones (`tests/test_image_editions.py`).

Both routes are declared to the Bazaar through the JSON body shape, because
that is the only shape the catalogue lists a POST route under (see
`_declare_post_discovery` in `routes.py`). `POST /index` reads its one optional
input, `agreementId`, from a JSON body or the query string; if both name one
and they differ, the request is refused before settlement. Its declared example
body is `{}`, so an agent that copies the listing buys a seat in whichever pool
is open. `POST /pin` still reads its input from the query string only: the price
callable that quotes it gets no body through its adapter interface (the FastAPI
adapter's `get_body()` is hardcoded to `None`). Its declared body is therefore
not what it reads, which has to be resolved before `PIN_ROUTE_ENABLED` is
turned on for a deployment.

## Layout

`api/server.py` is the entrypoint and **the only module that reads the
environment or opens a network connection**. It builds the real collaborators,
runs the startup guard, and hands the result to `api/app.py`. Everything below
it takes what it needs as an argument, so the whole package can be constructed
in a test against fakes with no network involved.

| module | what it owns |
| --- | --- |
| `server.py` | the entrypoint: environment, clients, startup guard |
| `app.py` | assembles the FastAPI app from already-built collaborators |
| `deps.py` | `Deps`, the injected bundle, and the agreement-creation lock |
| `config.py` | `Settings`, the startup guards, and `POST /index`'s default listing |
| `routes.py` | the route table and `POST /pin`'s listing copy |
| `web.py` | serves the built browser client under `/app/` |
| `pool.py` | the open pool read from the chain, and the pool route's price |
| `pricing.py` | the price callable — **the only place an agreement is created** |
| `escrow.py` | the escrow application as seen from off chain |
| `jobs.py` | the job lifecycle and its store |

The design these implement is specified in `docs/specs/` —
`delivery-escrow-backend.md`, `discovery-and-attribution.md` and
`escrow-contract.md`.

## Running locally

From the repository root, with dependencies installed (`requirements.txt`) and
a `.env` populated from `.env.example`:

```bash
uvicorn api.server:app --host 0.0.0.0 --port 8000
```

A `.env` shared with the operator scripts in `agents/` carries
`ADMIN_MNEMONIC`, which a server with `PIN_ROUTE_ENABLED` off refuses to hold.
Start that server with the key emptied — `load_dotenv()` never overrides a
variable already set, and an empty one counts as absent:

```bash
ADMIN_MNEMONIC= uvicorn api.server:app --host 0.0.0.0 --port 8000
```

## Configuration

`.env.example` is the full list and documents each value. Note that **no route
serves without configuration, `/healthz` included**: importing `api.server`
calls `load_settings()` and then `verify_startup()`, which reaches algod, so a
misconfigured process fails to start rather than answering a 402 it cannot
honour. That is deliberate — the challenge tag is stamped at settlement and
never reclassified, so a first MainNet settlement served from a wrong
configuration cannot be repaired afterwards.

`ALGOD_URL`, `ALGOD_TOKEN`, `USDC_ASA_ID`, `APP_ID`, `APP_ACCOUNT`,
`PUBLIC_HOST`, `FEE_PAYER`, `TREASURY` and `VERIFIER` have no defaults, and
nor, while `PIN_ROUTE_ENABLED` is on, do `PRICE_MICRO_USDC` and
`ADMIN_MNEMONIC`. With it off neither is needed, and `ADMIN_MNEMONIC` must be
absent: the escrow client is read-only, and a key the server never uses is one
it must not hold. An integer among them refuses to boot when absent; a string
reaches the guards as `""`, which is what the guards below are there to catch.

`verify_startup()` refuses to start on any of: a network and USDC asset that do
not match; `PIN_ROUTE_ENABLED` on in a MainNet configuration; `ADMIN_MNEMONIC`
set while `PIN_ROUTE_ENABLED` is off; an `APP_ACCOUNT`
that is not the application address of `APP_ID`; the production application id
appearing in a non-MainNet configuration; an app account that is not funded, not
opted in to the asset, or holding it frozen; a `RESOURCE_URL` that is not
`https://` or not on `PUBLIC_HOST`; a `WEB_DIST_DIR` that exists with no
`index.html`; an `INDEX_DESCRIPTION` missing a required clause (below); and a
route whose `resource`, `payTo`, `max_timeout_seconds`, attribution tag or fee
payer is wrong — a `/pin` route present while the switch is off among them,
since its resource is not one this configuration serves.
Each of those is otherwise **silent** in production: verification succeeds,
settlement succeeds, funds arrive, and only something downstream is quietly
wrong.

**`INDEX_DESCRIPTION` is product copy, not a tuning knob.** It is `POST
/index`'s description, and so, once the route has settled, its public catalogue
listing: what a discovering agent reads before it pays. Empty, it is
`DEFAULT_INDEX_DESCRIPTION` in `config.py`. Set, it replaces that text for this
deployment only. An override may reword and reorder, but must keep every clause
in `INDEX_DESCRIPTION_CLAUSES` — `join call`, `sha256`, `refunded`,
`Machine-verified`, `no arbitration` and `One seat per address`, matched
case-sensitively — or the server refuses to boot. The join-call clause is the
one that protects money: a transfer sent with no join call is not a purchase,
and the USDC it sends cannot be recovered. The others are the terms the escrow
enforces. The guard checks that the clauses are there, not that the sentences
around them are true, so an override is reviewed as the listing it becomes. The
discovery declaration follows the deployment in the same way: its output
example's `seatsTotal` is `INDEX_SEATS`.

Two route facts are asserted in `tests/` rather than at boot: that the pinned
resource carries no query string, and that the Bazaar declaration still
validates *after* the enrichment a live request applies to it.

Because the route's price is dynamic, the guard observes the tag and the fee
payer by asking the callable for `price.sample()` — the same `_quote` the live
callable returns, built without a chain call or a store write. **Boot therefore
parks no deposit.** Running the callable for real would create an agreement on
every cold start outside the previous sample's funding window, and since
`count_unfunded()` has no window filter those rows keep counting: a restart loop
walks the unfunded ceiling until boot refuses with a capacity error that
restarting cannot clear. `tests/test_pricing.py` pins the sample against the
callable's real output so the two cannot drift.

The guard also refuses a route with no `hook_timeout_seconds`, or one whose
timeout is not inside the funding window. x402-avm's default is
`wait_for(timeout=None)` — no bound at all on a call that goes to chain.

## Deploying

`Dockerfile` builds the client and the server into one image. Each deployment
has its own Fly config, because the client's `VITE_*` values are compiled into
the bundle as build arguments: an image is built for one network, one
application and one host.

| config | Fly app | network | application | pool |
| --- | --- | --- | --- | --- |
| `fly.testnet.toml` | `earnest-testnet` | TestNet | `771795120` | 2 seats at 0.1 USDC |
| `fly.travel-testnet.toml` | `earnest-travel` | TestNet | `772795100` | 3 seats at 5 USDC |
| `fly.mainnet.toml` | `earnest-mainnet` | MainNet | `3710645149` | 5 seats at 5 USDC |

Every value in each file is public, and no host holds a signing key.
`INDEX_AGREEMENT_ID` is set only as a secret (`fly secrets set
INDEX_AGREEMENT_ID=<id> -a <app>`), which restarts the machine; in `[env]`, the
next deploy would restore a stale id. Pass `-a` to every `fly` command: the
`fly.toml` at the repository root belongs to the TestNet discovery spike
(`earnest-spike-discovery`), and a command given neither `-a` nor `-c` acts on
that app.

`fly.travel-testnet.toml` is a second TestNet deployment of the same product. It
serves a second escrow application — the same program as the first TestNet one,
deployed under another application name (`ESCROW_APP_NAME`, read by
`contracts/smart_contracts/escrow/deploy_config.py`), so with its own
application account, `ZUGHACLDTHEDD5QYKCCQLKFN3TUUS27VTOV22R37FKWCRDRSVMWPPBUJIY`,
and its own `payTo` — from its own host, `https://earnest-travel.lxfoundry.ai`,
with its own seat terms and its own `INDEX_DESCRIPTION`. The other two set no
`INDEX_DESCRIPTION` and serve the default. Its client is built with
`VITE_BRAND=travel` and renders an offer the repository does not hold, supplied
at deploy time as the build argument `VITE_OFFER_B64`: the offer's UTF-8 JSON,
base64-encoded on one line (`base64 -w0 <offer.json>`).

```bash
fly deploy -c fly.travel-testnet.toml -a earnest-travel --build-arg VITE_OFFER_B64=<b64> --depot=false --remote-only --ha=false
```

The image build refuses a brand other than `index` or `travel`, and a travel
build whose offer is missing or is not what the client can decode: standard,
padded base64 (line breaks are ignored) of UTF-8 bytes holding a JSON object.
`VITE_BRAND` defaults to `index`, so the other two configs build as before. Of
the offer, the build checks nothing more: its fields are validated by the client
(`web/src/config.ts`) when it starts, in the buyer's browser, so open the page
after every deploy.

**One `payTo`, one domain.** A deployment's host is fixed before its
application's first settlement and kept for the life of that application. The
boot guard holds every resource to `PUBLIC_HOST`; keeping `PUBLIC_HOST` itself
unchanged is the operator's rule. Serving from a different host means deploying
a new application, not editing the config.

## Payment integrity

Two behaviours exist to stop a payment settling for something the route did not
serve, and both are pinned in `tests/test_payment_integrity.py`.

**`redirect_slashes=False` is a payment setting, not a style choice.** x402-avm
normalises a request path by collapsing slash runs and stripping trailing
slashes, so `/pin/` and `/pin//` are matched for payment against `POST /pin`.
With FastAPI's default the router answers those with a `307`, and
`PaymentMiddlewareASGI` declines to settle only on a response `>= 400` — so the
payment settled against a redirect. A redirect-following client then retried and
settled a *second* time: two settlements for one purchase. Turned off, those
paths `404` and nothing moves.

**The `jobId` is minted against the agreement in the verified payment**, read
from `request.state.payment_requirements`, not from a second store lookup. The
requirements are server-built — x402-avm re-runs the price callable on the paid
pass and matches on scheme, network, amount, asset and `payTo`, never on
`extra` — so `extra.agreementId` cannot be chosen by the buyer.

## Interrupted creates

An agreement's id only exists once the create returns, so the job row naming it
is necessarily written afterwards. A cancellation, crash or redeploy inside that
gap would leave an agreement holding minimum balance that nothing enumerating
`jobs` could find.

Two things close it. The create is recorded in a `create_intents` row
*before* it is submitted, and `unreconciled_create_intents()` returns the ones
with no job row — enough for a reconciler to settle them against chain state.
And the create runs to completion even when the caller goes away (`asyncio.shield`),
because abandoning it would release the creation lock while the worker thread is
still submitting, letting the next create read the same `agreement_count` and
fail on an existing roster box.

## Known limitations

**Two of three refusals do not reach the client as themselves.** A malformed
query *is* a real 400, produced by the query gate mounted outside the payment
middleware. `RateLimited` and `QuoteCeilingReached` are still a generic
`500 {"error": "Failed to process request"}`: both are stateful, so hoisting
them out of the price callable would double-count (a limiter counting the gate's
pass and the callable's pass charges one caller twice), and inside the callable
x402-avm swallows every exception before `ExceptionMiddleware` — where
`@app.exception_handler` lives — is reached. The handlers are registered and
inert. `tests/test_error_handling.py` pins the actual behaviour on both sides of
that split.

**The `jobId` is minted before settlement.** `PaymentMiddlewareASGI` calls
`process_settlement()` only after this handler returns, so a settlement that
then fails leaves a `FUNDED` row holding a `jobId` for an agreement nobody paid
into. The reverse direction is safe: a `409` or `500` is `>= 400`, so the
middleware skips settlement and the loser of a race is never charged. Closing
this needs a settlement the backend controls.

**The rate limiter is per process**, so the effective per-source rate is the
configured number times the worker count. The unfunded ceiling has no such
property — it queries the shared store — which is why the ceiling is the
guarantee and the limiter is a speed bump.
