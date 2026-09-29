# Discovery & attribution — resource server requirements

**Status: specification. Applies to every paid route Earnest serves.**

Payment and discovery are two independent systems. Settling payments correctly does not get an
endpoint into the Bazaar catalog, and being in the catalog does not get its volume attributed to the
Global x402 Challenge. Three separate things must be true, and two of them are only fixable before
the first MainNet settlement.

| Outcome | Turned on by | Repairable later? |
|---|---|---|
| Payment settles | payment middleware on the route | n/a |
| Endpoint is catalogued in the Bazaar | `extensions` declaring the discovery extension | Yes — declare it, take one more payment |
| Volume counts toward the challenge | `price.extra.tag` at settlement time | **No.** Attribution is stamped per settlement and earlier payments are never reclassified |
| Merchant branding (name, logo, blurb) | metadata at the domain root | Yes — a metadata enrichment engine re-crawls the domain roughly daily |

The rule that follows from row 3: **no MainNet settlement happens before the tag and the discovery
declaration are both live.** Everything else can be corrected by taking another payment.

---

## 1. Reference sources

- AF, *"Is your x402 endpoint showing up in the facilitator leaderboard? How to troubleshoot if
  not."* (2026-08-13) —
  https://algorand.co/blog/is-your-x402-endpoint-showing-up-in-the-facilitator-leaderboard-how-to-troubleshoot-if-not
- AF, *"Enabling x402 payments on Algorand: A best practices guide"* (2026-08-07) —
  https://algorand.co/blog/enabling-x402-payments-on-algorand-a-best-practices-guide
- AF, *"Introducing the GoPlausible x402 Facilitator"* (2026-07-24) —
  https://algorand.co/blog/introducing-the-goplausible-x402-facilitator-payments-and-intelligence-for-agentic-commerce-on-algorand
- AF, *"The x402 Global Challenge is live: how to build & submit your entry"* (2026-07-21) —
  https://algorand.co/blog/the-x402-global-challenge-is-live-how-to-build-submit-your-entry

Where these disagree, the 2026-08-13 post wins on cataloguing mechanics and the 2026-08-07 post wins
on domain enrichment; each is the one that treats its subject directly. In particular: the older
posts describe cataloguing as fully automatic on first settlement, which is true only once the route
declares the discovery extension (§2). Do not re-derive that from the marketing copy.

Package API for the Python stack: `x402.extensions.bazaar`, installed as `x402[extensions]`.

Fee note: the facilitator currently sponsors network fees, so buyers hold no ALGO for gas. Both AF
posts describe this as current behaviour that may change — treat gasless buyers as a facilitator
subsidy, not a property of the design.

---

## 2. Every paid route declares a discovery extension

The facilitator builds the catalog entry from the **payment payload**. It does not fetch the route to
find out what the route does, so a settlement on its own carries nothing describable: an address
received an amount. The description has to ride in the 402 response, and it only does so if the route
declares it.

A route without a discovery declaration is invisible to the Bazaar no matter how many payments it
settles, and nothing in the payment path reports the omission.

```python
from x402.extensions.bazaar import declare_discovery_extension

routes = {
    "POST /escrow/jobs": {
        "accepts": {
            "scheme": "exact",
            "network": ALGORAND_MAINNET_CAIP2,
            "payTo": ESCROW_APP_ACCOUNT,
            "price": {
                "amount": "10000",
                "asset": str(USDC_ASA_ID),
                "extra": {
                    "decimals": 6,
                    "feePayer": FEE_PAYER,
                    "tag": "x402-global-challenge",
                },
            },
        },
        "description": "Commission a job; payment is escrowed until the release condition is met.",
        "extensions": {
            **declare_discovery_extension(
                input={...},
                input_schema={"properties": {...}, "required": [...]},
                body_type="json",
                output={"example": {...}},
            ),
        },
    },
}
```

### `extra` belongs to the price, not beside it

The scheme builds the settled `PaymentRequirements.extra` from the **price's** `extra` and from
nothing else. Two ways of writing this look right and silently lose `feePayer` and `tag`:

- putting `extra` beside `price` in the `accepts` entry. An `accepts`-level `extra` key is not read
  on this path;
- using the string shorthand `"price": "$0.01"`. A bare money value is converted to USDC with `extra`
  replaced by `{"decimals": 6}`, discarding anything else.

Neither failure is visible from the outside: payments still settle. The 402 simply carries no
`feePayer`, so buyers must hold ALGO for their own gas, and every settlement lands untagged and
therefore unattributed — which, per the table at the top of this document, cannot be repaired
afterwards. Give `price` the explicit amount/asset form whenever `feePayer` or `tag` matter, which is
every route we serve.

`price` accepts either a dict carrying `amount` (as above) or an `AssetAmount`; both preserve `extra`
verbatim.

Two separate things happen here, and only one is per-route: the Bazaar resource-server extension is
registered on the server **once**, for the whole product, while a discovery declaration is attached
to **each route** along with its own description.

### The `description` field is the Bazaar listing

`description` is what the Bazaar catalog API returns and what both people and agents read when
browsing. It is product copy that happens to live in a config file: name what the caller actually
receives, not the topic. *"Escrow a payment until the supplier's sha256 commitment is met, or refund
it at the deadline"* is a listing; *"escrow API"* is not.

Every route's description follows the project's copy line — lead with the condition being chosen,
not with the escrow mechanism — so that the catalog entry, the landing page and the submission all
say the same thing.

Notes that are easy to get wrong:

- **A valid declaration is not necessarily a catalogued one.** The crawler applies its own rules, not
  the ones a declaration's own schema carries, and reports nothing when it declines one. `POST /index`
  settled real USDC under two declarations that both validated, and was catalogued under neither.
- **Every `POST` route declares `body_type="json"`**, including one that takes no input. The default
  shape is a `GET` with query parameters. Measured on 2026-09-29, all 1,001 catalogued `POST`
  resources are body-shaped; none declares `queryParams`, and none declares an input of only `type`
  and `method`, which is what an empty example produces in the query shape. Both were tried for
  `POST /index` and neither was catalogued; its first settlement in the body shape was listed within
  seconds. An empty *body* example is fine: it still declares `body: {}`, a form catalogued `POST`
  resources carry.
- **The handler reads what the declaration describes, and the example works when sent verbatim.** A
  buyer agent builds its request from the catalogue, and nothing in the library checks the
  declaration against the request. If a route declares a body, it accepts its input in that body.
  An example must be a request that succeeds: `POST /index` declares `body: {}` rather than a sample
  `agreementId`, because a fixed id would name a pool other than the open one and be refused.
  `POST /pin` does not meet this rule yet: it reads its input from the query string, because the price
  callable that quotes it gets no body through its adapter interface. It stays off until that is
  resolved (`delivery-escrow-backend.md`, "Open before this route is enabled on a deployment").
- No separate `register_extension` call is needed. The middleware registers the server-side extension
  on the first paid request when routes declare it.
- **The route row appears after the first successful payment**, not when the route deploys. Seeing
  the declaration in the 402 response is necessary and not sufficient.

## 3. Every `accepts` entry carries three fields

Per entry, not per route, and all three:

| Field | Why it is mandatory |
|---|---|
| `payTo` | The Python client model requires it on every entry and rejects the whole 402 response if any entry omits it. TypeScript clients skip such entries silently, so this breaks only Python buyers — invisibly, from our side |
| `price.extra.feePayer` | The facilitator sponsors network fees through it; buyers pay no gas |
| `price.extra.tag` = `"x402-global-challenge"` | Challenge attribution. Metadata only: no effect on settlement mechanics and none on Bazaar cataloguing |

Both `extra` keys live **inside `price`**, not beside it in the `accepts` entry, and the string
shorthand form of `price` drops them. See "`extra` belongs to the price, not beside it" in section 2.

`price.asset` (the USDC ASA id) is set alongside them; it does not replace `feePayer`.

**The tag must be present on the very first MainNet settlement.** Attribution is written at settlement
time, so a soft-launch that runs untagged donates that volume permanently to the `DIRECT` bucket.

## 4. The landing page is the input; the directory entry is the output

The catalog row comes from the payment payload, but the merchant *listing* is enriched separately: a
metadata enrichment engine crawls each merchant domain **roughly once a day** and builds the listing
from what it finds. The landing page is therefore part of the integration, not decoration — write it
to be read by a crawler first and a person second.

Publish at the domain root:

- **Open Graph tags** in `<head>`: `og:site_name`, `og:title`, `og:description`, `og:image`. Use a
  publicly reachable logo URL. These four are the fields the enrichment engine pulls.
- **`llms.txt`** (the llmstxt.org convention) — among the first things checked when the domain is
  probed.
- **Agent descriptors** we support: `/.well-known/x402.json`, and any of an A2A agent card, an
  ai-plugin manifest or an MCP manifest.
- `/logo.png`, `/favicon.ico`.
- A `<title>` and meta description that name what is actually sold. Specific beats generic: *"Escrow
  a payment until a sha256 commitment is met"* is a better listing than *"Payments API"*.

Because the crawl is at most daily, **branding changes take up to 24 hours to appear**. Fix it early;
do not expect a same-afternoon correction before a demo. Verify against `/discovery/merchants` (§6)
and believe what it returns.

This surface is also the channel for machine-readable detail aimed at buyer agents — release
conditions, job semantics, timeout behaviour. It belongs in `llms.txt` and the agent descriptors on
our own domain, not in the route config, which is schema-validated and rejects unrecognised fields.

## 5. Traffic shape: what must never be built

The facilitator classifies settlements into source buckets, and `DEV` is assigned automatically — it
is not a flag anyone sets. It catches localhost traffic and repeating loop patterns: pings, bots,
retry storms, cron jobs, health checks, and orchestrators that pay the same route on a schedule. A
settlement can be a real MainNet payment between real addresses and still be filed as developer
traffic, in which case it counts toward nothing.

Requirements:

1. **Liveness probes never pay.** Expose an unauthenticated, unpaid health route. Monitoring asserts
   on the *shape of the 402 response* (see §6, check 1); it never completes a payment.
2. **Outbound payments are event-driven, never scheduled.** Collection from a supplier endpoint is
   triggered by that supplier's completion signal. No fixed tick, no batch sweep, no polling loop
   that pays.
3. **Retries are bounded**, with backoff and a hard attempt cap. A retry storm against a slow
   supplier is indistinguishable from the pattern the classifier targets, and every attempt that
   settles is a real payment.
4. **Development and production use different escrow applications**, and therefore different `payTo`
   addresses. Two independent gates decide whether a route is catalogued at all, and both were
   measured on TestNet. First, a route served on a loopback host is **never catalogued**, however
   correctly it declares discovery, and no catalogued resource anywhere is served on a loopback
   address. Second, **public reachability is necessary but not sufficient**: a public route priced in
   real USDC is catalogued within seconds of its first settlement, with a merchant identity, while a
   sibling route on the same host priced in a locally minted ASA settles just as cleanly and is never
   listed. Whatever TestNet activity does get catalogued must never attach to the production merchant
   identity.
5. **Self-driven traffic is a demo, and demos run on TestNet.** Anything the project pays for itself
   to exercise a flow belongs on TestNet, including seeded pools.

## 6. Verification

### Before any deploy — route config is valid

`validate_discovery_extension()` is a pure function over the declaration. Assert it in the test suite
for every route. A malformed declaration fails silently in production: verify succeeds, settle
succeeds, funds arrive, and the catalog row simply never appears.

```python
from x402.extensions.bazaar import declare_discovery_extension, validate_discovery_extension

result = validate_discovery_extension(declare_discovery_extension(...)["bazaar"])
assert result.valid, result.errors
```

A bare `{}` passed where a schema belongs fails with `schema must be object or boolean`.

### After deploy — three commands

Every check below is per **route**, not per deployment. Two paid products can be served from
one host and one `payTo`: `/index` always, and `/pin` only where `PIN_ROUTE_ENABLED` is on — it is
off by default and refused on MainNet until its delivery half is built. Run checks 1 and 1b once
for each route the deployment actually serves — a declaration is opt-in per route, and one correct
route says nothing about the other. Where `/pin` is off it answers 404, carries no
`payment-required` header, and has nothing to check.

`/index` is the one that cannot be checked at any time: it answers a payable 402 only while
a pool is open, and 503 `no_pool_open` otherwise. Its ordering is therefore create the pool →
set `INDEX_AGREEMENT_ID` (a platform secret; setting it restarts the server) → run these checks
→ only then let a buyer pay. Creating
an agreement is not a settlement and attribution stamps at settlement, so nothing is spent by
checking after the create; what must not happen is a *payment* before the check.

A 503 `pool_status_unavailable` is not a failed check — it means the chain did not answer and
the check has not run. Retry.

```bash
# 1. the 402 response carries the discovery extension. Run once per route.
#    The requirements ride in the base64 `payment-required` HEADER, not the body, so the
#    response must be decoded before grepping. `curl -i ... | grep -i bazaar` never matches,
#    however correctly the route is configured.
curl -si -X POST 'https://<host>/pin?sha256=<64 hex>&size=1024' \
  | grep -i '^payment-required:' | cut -d' ' -f2- | tr -d '\r' \
  | python -c "import sys,base64,json; s=sys.stdin.read().strip(); print(list(json.loads(base64.urlsafe_b64decode(s+'='*(-len(s)%4))).get('extensions',{})))"
# expect: ['bazaar']

# 1b. the 402 advertises the PUBLIC url. The catalog row is built from this payload, so a
#     resource.url of `http://` or `localhost` is not catalogued at all -- and behind a TLS
#     terminator the app sees plain HTTP, so the server must run with `--proxy-headers`
#     (uvicorn: `--proxy-headers --forwarded-allow-ips '*'`) or it advertises the wrong scheme.
#     Run once per route: each carries its own pinned `resource`, and the tag has to be
#     present on both before either settles once.
curl -si -X POST https://<host>/index \
  | grep -i '^payment-required:' | cut -d' ' -f2- | tr -d '\r' \
  | python -c "import sys,base64,json; s=sys.stdin.read().strip(); d=json.loads(base64.urlsafe_b64decode(s+'='*(-len(s)%4))); print(d['resource']['url']); print(d['accepts'][0]['asset'], d['accepts'][0]['extra'].get('tag'))"
# expect: an https://<host>/... url, the ASA id, and the challenge tag

# 2. the resource is catalogued (after the first successful payment against it).
#    This endpoint reports a `pagination.total` larger than the maximum `limit` it accepts,
#    so one call returns only the first page and an absent row proves nothing on its own.
#    Page with `offset` until `total` is covered before concluding.
#    Cataloguing is not instant and not uniform: one route was listed six seconds after its
#    first settlement, another was still absent ~100 seconds after its own and was listed by
#    the next time it was checked. So an absence observed over a two-minute window is not a
#    negative -- re-check over hours before concluding a route is not catalogued, and never
#    write down a rule drawn from a short window.
#    `payTo` is NOT a top-level field on a row; it lives inside each row's `accepts` entries.
#    Filtering on a top-level `payTo` matches nothing and reads as "we were dropped from the
#    catalogue", which is far more alarming than the query being wrong.
curl -s "https://facilitator.goplausible.xyz/discovery/resources?includeTestnets=true&limit=1000&offset=0" \
  | jq '{total: .pagination.total, returned: (.items | length), matches: [.items[] | select(.resourceUrl | contains("<host>"))]}'

# 2b. one row per configured resource, no duplicates, and no query-bearing variant of any
#     of them. NOT one row in total whatever the deployment: both products share one
#     `payTo`, so a deployment serving both shows exactly two rows, and reading that as a
#     failure is the same false alarm this check exists to prevent. Count distinct
#     `resourceUrl`s against the number of paid routes the deployment serves. A route that takes
#     its input in the query string registers one directory entry per distinct query unless
#     `RouteConfig.resource` is pinned, so this is still the check that catches an unpinned
#     route: it shows up as more rows than routes, or as a `resourceUrl` carrying a `?`.
curl -s "https://facilitator.goplausible.xyz/discovery/resources?includeTestnets=true&limit=1000&offset=0" \
  | jq --arg payTo "<payTo>" '[.items[] | select(any(.accepts[]?; .payTo == $payTo))]
       | {rows: length, distinctUrls: ([.[].resourceUrl] | unique | length),
          urls: [.[].resourceUrl], anyQueryString: any(.[].resourceUrl; contains("?"))}'
# expect: rows == distinctUrls == the number of paid routes served (one, /index, while
#         PIN_ROUTE_ENABLED is off; two with it on), and anyQueryString false

# 3. the merchant identity exists for our payTo.
#    Read `.pagination.total` here too, though this catalogue has so far been small enough
#    for one call to cover it.
curl -s "https://facilitator.goplausible.xyz/discovery/merchants?includeTestnets=true&limit=500" \
  | jq '{total: .pagination.total, returned: (.items | length), matches: [.items[] | select(.addresses.avm == "<ESCROW_APP_ACCOUNT>")]}'
```

`tests/verify_join_group.py --discovery <host>` runs all three with the header decoding and the
paging already handled. It checks the 402 of **one** route per invocation (`--route /pin`,
`--route /index`), so run it once per route to cover check 1; checks 2, 2b and 3 are
deployment-wide and read the same either way.

**Match on `resourceUrl`, `payTo` and `merchantId` — never on the counters.** The discovery records
are not a stable schema. Between two observations three days apart the resource record dropped
`verifyCount` entirely, and the merchant record's `totalVerifications` became `totalSettlements` —
a rename that also changed what the number counts. Treat every other field as liable to appear,
vanish or be renamed without notice.

`merchantId` is the Base64 encoding of the first 24 characters of the `payTo` address, so it can be
derived locally:

```bash
echo -n "<ESCROW_APP_ACCOUNT>" | cut -c1-24 | tr -d '\n' | base64
```

### 24h after the first MainNet settlement, then weekly — attribution bucket

Cataloguing and attribution fail independently. This is the only check that distinguishes them:

```bash
BASE=https://facilitator.goplausible.xyz/data/leaderboards
for s in x402-global-challenge bazaar direct dev; do
  echo "src=$s"
  curl -s "$BASE?cat=merchants&limit=200&range=all&env=mainnet&src=$s" \
    | jq '.items[] | select(.address=="<ESCROW_APP_ACCOUNT>") | {rank,settles,volume}'
done
```

Volume appearing under `direct` or `dev` while the route is correctly tagged is a defect to escalate
with this output attached, not a reason to wait.

## 7. Universal Receipts

A facilitator extension, independent of discovery. A receipt is minted on **first request**, not at
settlement:

```
GET https://facilitator.goplausible.xyz/api/receipt/{txId}
```

`txId` is the Algorand transaction id of the settled payment — the `transaction` field of the
facilitator's `SettleResponse`, also returned to the client in the payment-response header. The call
is idempotent and redirects (302) to a hosted page carrying the settlement facts, both counterparties,
the on-chain fee with an explorer link, the note, the settling facilitator, a QR code and share links.

The receipt id is derivable offline, so storing the transaction id is sufficient and the URL can be
rebuilt at render time without calling the endpoint:

```python
receipt_id = hashlib.sha256(tx_id.encode()).hexdigest()[:32]
# https://goplausible.xyz/api/receipt/{receipt_id}
```

Constraints to design around:

- **MainNet only** — a TestNet transaction returns 400.
- **404** for any transaction not settled through this facilitator.
- **Receipts are valid for 90 days.** They are a presentation layer, never the system of record: the
  contract's own ARC-28 event log is the permanent receipt spine, and any page that links to a
  receipt must render correctly once the link has lapsed.
- **Refunds have no receipt.** They are direct inner transactions and never touch the facilitator, so
  a refunded job renders from chain data alone.

## 8. Deploy checklist

Before the first MainNet settlement — the two irreversible items first:

- [ ] `price.extra.tag = "x402-global-challenge"` on every `accepts` entry of every paid route
- [ ] Discovery extension declared on every paid route, and `validate_discovery_extension` green in CI
- [ ] `payTo` and `price.extra.feePayer` present on every `accepts` entry
- [ ] Production escrow application distinct from the development one; `payTo` points at production
- [ ] Escrow app account funded and opted in to USDC before the endpoint can return a 402
- [ ] Landing page live at the domain root with the Open Graph block, `llms.txt`,
      `/.well-known/x402.json`, `/logo.png`, `/favicon.ico` — allow up to 24h for the listing to
      reflect it
- [ ] Unpaid health route live; no monitoring path completes a payment
- [ ] The decoded `payment-required` header of a 402 lists the `bazaar` extension (section 6 check 1)

After the first settlement:

- [ ] Resource row present in `/discovery/resources`
- [ ] Merchant row present in `/discovery/merchants` for the production `payTo`
- [ ] Volume lands under `src=x402-global-challenge` (check at 24h, then weekly)
- [ ] Receipt resolves for the first settled transaction id

## 9. Definition of done

The organisers' own five conditions. Nothing is ready until all five hold:

1. At least one real MainNet payment has settled through the GoPlausible facilitator.
2. The paid response was returned.
3. USDC landed in the `payTo` address.
4. The endpoint appears in the Bazaar catalog under the `x402-global-challenge` tag.
5. It is driving real usage and volume visible on the leaderboard.

Conditions 1–3 are one end-to-end test. Condition 4 is §6's three commands plus the source-bucket
check. Condition 5 is the only one that cannot be completed on the day it is started, which is why
the other four are gated on being right *before* the first settlement rather than after.

Additional constraints that hold for the whole competition:

- **One `payTo` for the entire competition** — the leaderboard attributes the entry by that address,
  so it never rotates.
- **One root domain per merchant account.** Endpoints differentiate resources, never domains: the
  same `payTo` must not serve endpoints on different domains.
- The resource server runs on a public host over HTTPS, never localhost.
