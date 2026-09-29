"""Re-runnable verification for the TestNet join-path and discovery spike.

Three independent checks, each of which can be run on its own:

  --group <groupId>   On-chain shape check. Fetches the atomic group from the
                      indexer and asserts it contains the axfer, the extra
                      application call, and a fee-payer leg signed by the
                      address the facilitator advertises at `/supported` --
                      the part that distinguishes a facilitator-cosigned
                      settlement from a local simulation. Override the expected
                      signer with --expected-fee-payer (needed for a dry-run
                      group, whose fee payer is a local throwaway key).
  --box <appId>:<n>   Contract-state check: `payers[n]` exists and decodes to an
                      address, plus the ARC-28 `Joined` event on that app.
  --discovery <host>  Discovery checks 1-3 from
                      docs/specs/discovery-and-attribution.md section 6. When
                      the route is absent from the catalogue it also prints an
                      attribution survey -- the catalogued population by
                      network, and how many catalogued resources are served on
                      a loopback host -- so "absent because unreachable" can be
                      told from "absent because TestNet" or "absent because the
                      declaration is wrong" from data rather than assertion.

Check 1 in that spec is written as `curl -i <host>/<route> | grep -i bazaar`.
That command cannot succeed against a V2 resource server: the payment
requirements, including the `extensions` object the grep is looking for, ride in
the base64-encoded `payment-required` response header rather than in the body.
This script decodes the header before looking for the declaration, which is what
the spec's check was actually trying to establish.

Usage:
    python -m tests.verify_join_group --discovery http://localhost:8001
    python -m tests.verify_join_group --box 769609080:10
    python -m tests.verify_join_group --group b3vgmbpoTkIoYyOwNpzRiGhRrqmzDJgidyoFfyujNOY=
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys

import httpx
from algosdk import encoding
from dotenv import load_dotenv

from agents.build_join_group import box_name
from agents.common import get_algod_client

load_dotenv()

DEFAULT_FACILITATOR = "https://facilitator.goplausible.xyz"
DEFAULT_INDEXER = "https://testnet-idx.algonode.cloud"

JOINED_EVENT_SIGNATURE = b"Joined(uint64,address,byte[])"
JOINED_SELECTOR = hashlib.new("sha512_256", JOINED_EVENT_SIGNATURE).digest()[:4]

ALGORAND_TESTNET_CAIP2 = "algorand:SGO1GKSzyE7IEPItTxCByw9x8FmnrCDexi9/cOUJOiI="
ALGORAND_MAINNET_CAIP2 = "algorand:wGHE2Pwdvd7S12BL5FaOP20EGYesN73ktiC1qzkkit8="

# Hosts that no external crawler can reach. A route served on one of these
# cannot be catalogued however correctly it declares discovery, so the scan in
# check_discovery uses this to tell "declaration is broken" from "nobody can
# reach the resource".
LOOPBACK_MARKERS = ("localhost", "127.0.0.1", "0.0.0.0", "[::1]")


def facilitator_sponsor() -> str | None:
    """The algorand sponsor address the facilitator advertises at `/supported`.

    This is the address whose signature on a group's fee-payer leg constitutes
    proof that the facilitator countersigned. Returns None if `/supported` is
    unreachable, in which case the caller must not claim cosigning either way.
    """
    facilitator = os.getenv("FACILITATOR_URL", DEFAULT_FACILITATOR)
    try:
        with httpx.Client(timeout=30) as client:
            resp = client.get(f"{facilitator}/supported")
            resp.raise_for_status()
            signers = resp.json().get("signers") or {}
    except Exception as exc:  # noqa: BLE001 -- unreachable facilitator is a normal outcome
        print(f"  WARN: could not read {facilitator}/supported: {exc}")
        return None
    for family, addresses in signers.items():
        if family.startswith("algorand") and addresses:
            return addresses[0]
    print(f"  WARN: {facilitator}/supported advertises no algorand signer")
    return None


def decode_joined_event(log: bytes) -> dict | None:
    """Decode one ARC-28 `Joined` log, or None if it is a different event.

    Layout after the 4-byte event selector, per ARC-4 tuple encoding:
    uint64 agreement_id | 32-byte address payer | uint16 offset -> byte[] tail.
    """
    if len(log) < 46 or log[:4] != JOINED_SELECTOR:
        return None
    body = log[4:]
    offset = int.from_bytes(body[40:42], "big")
    length = int.from_bytes(body[offset : offset + 2], "big")
    txn_id = body[offset + 2 : offset + 2 + length]
    return {
        "agreement_id": int.from_bytes(body[0:8], "big"),
        "payer": encoding.encode_address(body[8:40]),
        "payment_txn_id": base64.b32encode(txn_id).decode().rstrip("="),
    }


def check_group(group_id: str, expected_fee_payer: str | None = None) -> bool:
    """Assert the settled group has the 3-leg join shape and was cosigned.

    `expected_fee_payer` defaults to the facilitator's advertised sponsor, which
    is what a real settlement must carry. For a group produced by the
    facilitator-independent dry run, pass that run's own fee-payer address --
    the check is "signed by the party we expected", never "signed by anyone
    other than the buyer".
    """
    indexer = os.getenv("INDEXER_TESTNET_URL") or DEFAULT_INDEXER
    print(f"=== group {group_id} ===")
    with httpx.Client(timeout=60) as client:
        resp = client.get(
            f"{indexer}/v2/transactions",
            params={"group-id": group_id},
        )
        resp.raise_for_status()
        txns = resp.json().get("transactions", [])

    if not txns:
        print("FAIL: no transactions found for that group id")
        return False

    by_type: dict[str, list[dict]] = {}
    for txn in txns:
        by_type.setdefault(txn["tx-type"], []).append(txn)
        print(
            f"  {txn['tx-type']:<5} {txn['id']} sender={txn['sender']} fee={txn['fee']}"
        )

    ok = True
    if len(txns) != 3:
        print(f"FAIL: expected 3 legs, found {len(txns)}")
        ok = False
    for want in ("axfer", "appl", "pay"):
        if want not in by_type:
            print(f"FAIL: no {want} leg in the group")
            ok = False

    if ok:
        axfer_sender = by_type["axfer"][0]["sender"]
        appl_sender = by_type["appl"][0]["sender"]
        pay_sender = by_type["pay"][0]["sender"]
        if axfer_sender != appl_sender:
            print("FAIL: axfer and appl legs have different senders")
            ok = False

        # The cosigning test. "Not the buyer" is far too weak to prove this:
        # any second key -- including one the buyer controls, or the throwaway
        # signer the facilitator-independent dry run uses -- satisfies it. The
        # only thing that establishes the facilitator countersigned is the
        # fee-payer leg carrying the signature of the specific address the
        # facilitator advertises as its sponsor.
        if expected_fee_payer is None:
            expected_fee_payer = facilitator_sponsor()

        if expected_fee_payer is None:
            print(
                "  INCONCLUSIVE: no expected fee-payer address available, so "
                "cosigning is unverified. Pass --expected-fee-payer to assert it."
            )
            ok = False
        elif pay_sender == expected_fee_payer:
            print(f"  fee-payer leg signed by the expected party: {pay_sender}")
        else:
            print(
                f"FAIL: fee-payer leg was signed by {pay_sender}, "
                f"expected {expected_fee_payer}"
            )
            ok = False

    print("PASS" if ok else "FAIL")
    return ok


def check_box(app_id: int, agreement_id: int) -> bool:
    """Assert payers[agreement_id] is set, and show the matching Joined event."""
    print(f"=== app {app_id} box payers[{agreement_id}] ===")
    algod = get_algod_client()
    name = box_name(agreement_id)
    try:
        box = algod.application_box_by_name(app_id, name)
    except Exception as exc:  # noqa: BLE001 -- a missing box is a normal failure here
        print(f"FAIL: box not readable: {exc}")
        return False

    raw = base64.b64decode(box["value"])
    if len(raw) != 32:
        print(f"FAIL: box value is {len(raw)} bytes, expected a 32-byte address")
        return False
    print(f"  payer = {encoding.encode_address(raw)}")

    indexer = os.getenv("INDEXER_TESTNET_URL") or DEFAULT_INDEXER
    with httpx.Client(timeout=60) as client:
        resp = client.get(
            f"{indexer}/v2/applications/{app_id}/logs", params={"limit": 100}
        )
        resp.raise_for_status()
        entries = resp.json().get("log-data", [])

    found = False
    for entry in entries:
        for log_b64 in entry.get("logs", []):
            event = decode_joined_event(base64.b64decode(log_b64))
            if event and event["agreement_id"] == agreement_id:
                print(f"  Joined event in {entry['txid']}: {json.dumps(event)}")
                found = True
    if not found:
        print(f"FAIL: no Joined event for agreement {agreement_id}")
    print("PASS" if found else "FAIL")
    return found


def _fetch_all(client: httpx.Client, url: str, limit: int) -> list[dict]:
    """Page through a discovery endpoint; its limit caps out below the total."""
    items: list[dict] = []
    offset = 0
    while True:
        resp = client.get(
            url, params={"includeTestnets": "true", "limit": limit, "offset": offset}
        )
        resp.raise_for_status()
        body = resp.json()
        page = body.get("items", [])
        items += page
        total = (body.get("pagination") or {}).get("total", len(items))
        offset += limit
        if offset >= total or not page:
            return items


def _configured_resources(host: str) -> set[str]:
    """The resource URLs this deployment is supposed to catalogue.

    Read from the same `Settings` the server itself boots from -- two
    products now share one `payTo`, so "exactly one catalogued row" is no
    longer the right shape to check against, and the right one (one row per
    configured resource) is only knowable from configuration, not guessed
    from the host and a hardcoded route name.

    Falls back to the two route paths this repository serves if the
    environment does not carry the full backend configuration (a bare
    TestNet-spike `.env`, missing something `load_settings` requires): this
    script has always been runnable against both kinds of environment, and a
    missing setting should degrade the check rather than crash it.
    """
    try:
        from api.config import load_settings

        settings = load_settings()
        return set(settings.resource_urls)
    except Exception as exc:  # noqa: BLE001 -- a minimal .env is a normal case here
        fallback = {f"{host.rstrip('/')}/pin", f"{host.rstrip('/')}/index"}
        print(
            f"  WARN: could not load full Settings ({exc}); falling back to "
            f"{sorted(fallback)}"
        )
        return fallback


def check_discovery(host: str, route: str = "/pin") -> bool:
    """Discovery checks 1-3. Check 1 gates how a 2/3 failure is attributed."""
    facilitator = os.getenv("FACILITATOR_URL", DEFAULT_FACILITATOR)
    url = f"{host.rstrip('/')}{route}"

    print(f"=== check 1: discovery extension declared on {url} ===")
    declared = False
    pay_to = None
    with httpx.Client(timeout=30) as client:
        resp = client.post(url)
        header = resp.headers.get("payment-required")
        if resp.status_code != 402 or not header:
            print(
                f"FAIL: expected a 402 carrying payment-required, got {resp.status_code}"
            )
        else:
            padded = header + "=" * (-len(header) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded))
            extensions = payload.get("extensions") or {}
            declared = "bazaar" in extensions
            pay_to = payload["accepts"][0]["payTo"]
            print(f"  extensions: {list(extensions)}")
            print(f"  payTo: {pay_to}")
            print(
                "  PASS: bazaar declared" if declared else "  FAIL: no bazaar extension"
            )

    merchant_id = base64.b64encode(pay_to[:24].encode()).decode() if pay_to else None

    print(f"\n=== check 2: resource catalogued (payTo {pay_to}) ===")
    with httpx.Client(timeout=90) as client:
        resources = _fetch_all(client, f"{facilitator}/discovery/resources", 1000)
        print(f"  scanned {len(resources)} catalogued resources")
        # Three independent ways of finding ourselves, so a miss is not just an
        # artefact of one field being keyed differently than expected.
        matches = [
            r
            for r in resources
            if (r.get("resourceUrl") or "").rstrip("/") == url.rstrip("/")
        ]
        print(f"  by exact resourceUrl {url!r}:")
        print(json.dumps(matches, indent=2) if matches else "    no match")
        raw = [r for r in resources if pay_to and pay_to in json.dumps(r)]
        print(
            f"  by payTo address anywhere in the record: {len(raw)} match(es)"
            if raw
            else "  by payTo address anywhere in the record: no match"
        )
        by_mid = [
            r for r in resources if merchant_id and r.get("merchantId") == merchant_id
        ]
        print(
            f"  by derived merchantId {merchant_id!r}: {len(by_mid)} match(es)"
            if by_mid
            else f"  by derived merchantId {merchant_id!r}: no match"
        )

        # Check 2b: one row per *configured* resource, and no query-bearing
        # variant anywhere.
        #
        # payTo is NOT a top-level field -- it lives inside each row's `accepts`
        # entries. Reading r["payTo"] silently matches nothing and reads as "we
        # were dropped from the catalogue", which is a much more alarming answer
        # than "the query was written against the wrong shape".
        #
        # "Exactly one row for this payTo" stopped being the right shape once
        # two products started sharing one payTo: a correct deployment now
        # catalogues *two* rows, one per route, and the old check flagged that
        # as a failure. Compared against `_configured_resources` instead --
        # one row per configured resource, no more, no query string -- which
        # is right for one product or several.
        #
        # Asserted on our own rows and never on a delta in pagination.total: the
        # catalogue grows continuously on other merchants' traffic, so a global
        # count proves nothing. Match on resourceUrl, payTo and merchantId only,
        # never on a counter -- three field renames have been observed so far.
        configured = _configured_resources(host)
        print(f"\n=== check 2b: one row per configured resource, payTo {pay_to} ===")
        print(f"  configured resources: {sorted(configured)}")
        ours = [
            r
            for r in resources
            if any(a.get("payTo") == pay_to for a in (r.get("accepts") or []))
        ]
        print(f"  rows whose accepts carry our payTo: {len(ours)}")
        seen: dict[str, int] = {}
        for row in ours:
            accepts = (row.get("accepts") or [{}])[0]
            resource_url = row.get("resourceUrl") or ""
            seen[resource_url] = seen.get(resource_url, 0) + 1
            print(f"    {resource_url}")
            print(
                f"      settleCount={row.get('settleCount')} "
                f"maxTimeoutSeconds={accepts.get('maxTimeoutSeconds')} "
                f"lastSeen={row.get('lastSeen')}"
            )

        duplicates = sorted(u for u, n in seen.items() if n > 1)
        unexpected = sorted(u for u in seen if u and u not in configured)
        query_bearing = sorted(u for u in seen if "?" in u)
        missing = sorted(configured - set(seen))

        unique = not duplicates and not unexpected and not query_bearing
        if duplicates:
            print(f"  FAIL: more than one catalogued row for: {duplicates}")
        if unexpected:
            print(
                "  FAIL: catalogued row(s) not among configured resources: "
                f"{unexpected}"
            )
        if query_bearing:
            print(
                f"  FAIL: catalogued resourceUrl carries a query string: {query_bearing}"
            )
        if missing:
            print(f"  not yet catalogued (may just be early): {missing}")
        if unique:
            print(
                "  PASS: one row per configured resource, none carrying a query string"
            )

        query_rows = [r for r in resources if "?" in (r.get("resourceUrl") or "")]
        print(
            f"  query-bearing resourceUrls anywhere in the catalogue: {len(query_rows)}"
        )
        for row in query_rows[:5]:
            print(f"    {row.get('resourceUrl')}")

        print(f"\n=== check 3: merchant identity for {pay_to} ===")
        merchants = _fetch_all(client, f"{facilitator}/discovery/merchants", 500)
        print(f"  scanned {len(merchants)} merchants")
        mmatch = [
            m for m in merchants if (m.get("addresses") or {}).get("avm") == pay_to
        ]
        print("  by addresses.avm:")
        print(json.dumps(mmatch, indent=2) if mmatch else "    no match")
        m_raw = [m for m in merchants if pay_to and pay_to in json.dumps(m)]
        print(
            f"  by payTo address anywhere in the record: {len(m_raw)} match(es)"
            if m_raw
            else "  by payTo address anywhere in the record: no match"
        )

    # Identity is `payTo` and the merchant record, never exact URL equality.
    #
    # `matches` compares the catalogued resourceUrl against the URL that was
    # requested, which is wrong the moment a route is called with a query
    # string: pinning `RouteConfig.resource` exists precisely so the catalogued
    # URL is *not* the requested one, so `--route '/x?a=1'` would report a
    # correctly catalogued route as absent. It stays as a printed diagnostic in
    # check 2 -- where seeing it match or not is informative -- and out of the
    # verdict. Check 2b already establishes the stronger property: exactly one
    # row carries this payTo, and its resourceUrl carries no query string.
    catalogued = bool(mmatch) and unique
    if not catalogued:
        cause = (
            "route declares discovery, so this is 'not catalogued (yet)', "
            "not 'never declared'"
            if declared
            else "route never declared discovery at all"
        )
        print(f"\nFAIL: not catalogued -- {cause}")
        _attribution_survey(resources, merchants, host)
    else:
        print("\nPASS: catalogued")
    return catalogued


def _attribution_survey(
    resources: list[dict], merchants: list[dict], host: str
) -> None:
    """Two population statistics that separate the reasons a route can be absent.

    Printed only when the route is missing, because that is the only time the
    question "absent for which reason?" arises. Neither number is a pass/fail
    signal on its own; they exist so the attribution in the findings is
    reproducible rather than asserted.
    """
    print("\n=== attribution survey (why might it be absent?) ===")

    networks: dict[str, int] = {}
    for resource in resources:
        for accept in resource.get("accepts", []):
            net = accept.get("network")
            networks[net] = networks.get(net, 0) + 1
    testnet = networks.get(ALGORAND_TESTNET_CAIP2, 0)
    # Counted per accepts entry, not per resource: a resource may advertise
    # several networks, so these sum to more than the resource count.
    total_accepts = sum(networks.values())
    print(
        f"  accepts entries by network "
        f"({total_accepts} entries across {len(resources)} resources):"
    )
    for net, count in sorted(networks.items(), key=lambda kv: -kv[1]):
        label = ""
        if net == ALGORAND_TESTNET_CAIP2:
            label = "  <- algorand TESTNET"
        elif net == ALGORAND_MAINNET_CAIP2:
            label = "  <- algorand mainnet"
        print(f"    {count:>6}  {net}{label}")
    print(
        f"  => TestNet accepts entries are catalogued ({testnet} of them), so "
        f"'TestNet is excluded' does not explain the absence."
        if testnet
        else "  => no TestNet entry is catalogued at all; TestNet exclusion "
        "is a live explanation for the absence."
    )

    m_networks: dict[str, int] = {}
    for merchant in merchants:
        for net in merchant.get("networks", []):
            m_networks[net] = m_networks.get(net, 0) + 1
    m_total = sum(m_networks.values())
    print(
        f"  merchant network entries "
        f"({m_total} entries across {len(merchants)} merchants):"
    )
    for net, count in sorted(m_networks.items(), key=lambda kv: -kv[1]):
        label = ""
        if net == ALGORAND_TESTNET_CAIP2:
            label = "  <- algorand TESTNET"
        elif net == ALGORAND_MAINNET_CAIP2:
            label = "  <- algorand mainnet"
        print(f"    {count:>6}  {net}{label}")

    loopback = [
        r
        for r in resources
        if any(marker in (r.get("resourceUrl") or "") for marker in LOOPBACK_MARKERS)
    ]
    print(f"  catalogued resources served on a loopback host: {len(loopback)}")
    for resource in loopback[:5]:
        print(f"    {resource.get('resourceUrl')}")
    serving_loopback = any(marker in host for marker in LOOPBACK_MARKERS)
    if serving_loopback and not loopback:
        print(
            f"  => this route is served on a loopback host ({host}) and no "
            "catalogued resource anywhere is. The resource being unreachable "
            "from outside is the leading explanation."
        )
    elif serving_loopback:
        print(
            "  => this route is on a loopback host, but such resources do appear "
            "in the catalogue, so unreachability alone does not explain it."
        )
    else:
        print(f"  => this route is not on a loopback host ({host}).")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", help="base64 group id to verify on-chain")
    parser.add_argument("--box", help="<appId>:<agreementId>")
    parser.add_argument("--discovery", help="resource host, e.g. http://localhost:8001")
    parser.add_argument(
        "--route",
        default="/pin",
        help=(
            "route path to check with --discovery for check 1's single-route "
            "probe (check 2b checks every configured resource regardless), "
            "with or without a leading slash -- pass /index to probe the "
            "pool route instead (default: /pin)"
        ),
    )
    parser.add_argument(
        "--expected-fee-payer",
        help=(
            "address that must have signed the fee-payer leg. Defaults to the "
            "facilitator's advertised sponsor; pass the dry run's own fee-payer "
            "address when verifying a dry-run group."
        ),
    )
    args = parser.parse_args()

    if not any((args.group, args.box, args.discovery)):
        parser.error("pass at least one of --group, --box, --discovery")

    results = []
    if args.group:
        results.append(check_group(args.group, args.expected_fee_payer))
    if args.box:
        app_id, _, agreement_id = args.box.partition(":")
        results.append(check_box(int(app_id), int(agreement_id)))
    if args.discovery:
        # Accept the route with or without a leading slash. Beyond being
        # forgiving, the slashless form is the usable one under Git Bash,
        # which rewrites a leading-slash argument into a Windows path.
        route = args.route if args.route.startswith("/") else f"/{args.route}"
        results.append(check_discovery(args.discovery, route))

    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
