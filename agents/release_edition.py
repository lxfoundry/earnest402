"""Release one edition's pool, with the edition's CID in the release note.

    python -m agents.release_edition --agreement-id 31 --cid bafybei... \
        --manifest editions/edition-1-manifest.json --dry-run
    python -m agents.release_edition --agreement-id 31 --cid bafybei... \
        --manifest editions/edition-1-manifest.json

Operator-only, run by hand, and irreversible: `release_hash` pays the whole
pool to its beneficiary, and the contract accepts it once per agreement. The
specification is `docs/specs/edition-release.md`. Nothing is sent until

  - the agreement is FUNDED, the signing key is its verifier, and the chain's
    clock leaves time before the deadline;
  - the manifest's sha256 is the agreement's `commit_hash`, and the manifest's
    entry for the committed file carries that same digest;
  - every file the manifest inventories has been fetched through public
    gateways under `--cid` and hashed against its published digest. The
    gateways are `RECHECK_GATEWAYS`, below.

`--skip-gateway-check` leaves out the last of these, on any network, for an
operator whose machine cannot reach the gateways. The release then records a
CID nobody fetched, and only the operator's own check stands behind it.

The manifest's `file` says what kind of deliverable the pool sold --
`edition-N.json` for the index, `trip-N.json` for a trip file -- and the note
carries that kind's prefix. The kinds are `agents/product_kinds.py`.

The CID is the one the pinning provider reported for the bundle. It is passed
in, never derived: a CID depends on chunker, codec and layout, and an edition
is identified by its sha256, not by where it is served from. When the bundle
was pinned with more than one provider and they built different CIDs over the
same bytes, one is recorded here and the others stay off chain as further
locators.

The gateway check below is this module's own. The tooling that produces and
pins a bundle is not part of this repository, and a release depends on
nothing from it but the manifest and the CID.

The signing key is `VERIFIER_MNEMONIC`, read from the operator's file for the
network the settings name -- `contracts/.env.mainnet` or
`contracts/.env.testnet`, both gitignored -- and never from `.env`, which the
server loads too. Choosing the file by network means a TestNet configuration
cannot pick up the MainNet key.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from algosdk import account, mnemonic
from algosdk.atomic_transaction_composer import AccountTransactionSigner
from algosdk.v2client import algod as algod_client
from dotenv import dotenv_values, load_dotenv
from x402.mechanisms.avm import ALGORAND_MAINNET_CAIP2, ALGORAND_TESTNET_CAIP2

from agents.product_kinds import KINDS, KINDS_BY_NAME, committed_kind
from api.config import load_settings
from api.escrow import NOTE_MAX_BYTES, STATE_FUNDED, Agreement, EscrowClient

REPO_ROOT = Path(__file__).resolve().parents[1]

VERIFIER_MNEMONIC = "VERIFIER_MNEMONIC"

# The operator's per-network files, beside the deployer key AlgoKit reads from
# the same place. Neither is committed.
NETWORK_ENV_FILES = {
    ALGORAND_MAINNET_CAIP2: REPO_ROOT / "contracts" / ".env.mainnet",
    ALGORAND_TESTNET_CAIP2: REPO_ROOT / "contracts" / ".env.testnet",
}

# One pattern for writing and for reading, so the two cannot drift. Published
# in docs/specs/edition-release.md §1; a reader that finds a note not matching
# it in full ignores the transaction. Every kind's prefix is one alternative
# as a whole, so a note that mixes one kind's name with another's stem matches
# neither, and the groups are numbered the same for every kind: the edition,
# the agreement id, the CID.
_KIND_PREFIXES = "|".join(re.escape(kind.note_prefix) for kind in KINDS)
NOTE_PATTERN = re.compile(
    rf"^(?:{_KIND_PREFIXES})"
    r"([1-9][0-9]*)"
    r":agreement:(0|[1-9][0-9]*)"
    r":cid:([A-Za-z0-9]+)$"
)

# A digest as a manifest publishes it: lowercase sha256 hex.
SHA256_HEX = re.compile(r"[0-9a-f]{64}")

# A file name a manifest may inventory: one path segment, starting with a letter
# or a digit. Each name is fetched as `<gateway>/ipfs/<cid>/<name>`, so this
# rules out `.`, `..`, `/`, `?`, `#`, `%` and the empty name -- anything that
# would fetch some other resource than that file under that CID.
FILE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

# Gateway verification can take minutes -- a gateway that has never been asked
# for a CID has to go and find it -- so the first check asks for this much
# time before the deadline, and the check just before sending asks only for
# the last margin below.
DEFAULT_MIN_RUNWAY_SECONDS = 900
FINAL_MARGIN_SECONDS = 60

# The gateways the bundle is fetched back through: `RECHECK_GATEWAYS`, from
# the environment or `.env`, comma-separated, tried in order for each file. A
# bare host means https. Unset or blank, it is the one gateway below.
#
# The question is not whether the pinning provider kept the bytes -- it was
# handed them -- but whether a stranger, the seat holder, can fetch them. A
# gateway run by that same provider answers that question least well: it
# proves the provider serves what it was handed. List a gateway that has
# nothing to do with the pin too, for a check closer to a stranger's.
RECHECK_GATEWAYS = "RECHECK_GATEWAYS"
DEFAULT_RECHECK_GATEWAYS = "https://ipfs.filebase.io"

# Bounded with backoff and a hard cap: a gateway that has never been asked for
# a CID has to go and find it, which is neither instant nor helped by
# hammering. Five waits of at most a minute, then an honest failure.
VERIFY_ATTEMPTS = 6
VERIFY_BASE_SECONDS = 5
VERIFY_MAX_SECONDS = 60


def recheck_gateways(env: Mapping[str, str]) -> tuple[str, ...]:
    """The gateways `RECHECK_GATEWAYS` names in `env`, as https origins.

    Unset or blank means the default. Set to nothing but commas and spaces, it
    is refused: that is a typo in a list, not a request for the default. Every
    entry must be an https origin -- no path, query, fragment or credentials --
    because each file is fetched as `<gateway>/ipfs/<cid>/<name>`.
    """
    value = env.get(RECHECK_GATEWAYS) or ""
    if not value.strip():
        value = DEFAULT_RECHECK_GATEWAYS
    gateways = []
    for entry in value.split(","):
        entry = entry.strip()
        if not entry:
            continue
        gateway = (entry if "://" in entry else f"https://{entry}").rstrip("/")
        if not _is_https_origin(gateway):
            raise SystemExit(
                f"{RECHECK_GATEWAYS} lists {entry!r}, which is not an https "
                "origin: a gateway is a bare host or https://host, with no "
                "path, query, fragment or credentials. Nothing was sent."
            )
        gateways.append(gateway)
    if not gateways:
        raise SystemExit(
            f"{RECHECK_GATEWAYS} is {value!r}, which names no gateway. Set it to "
            "one or more gateways separated by commas, or leave it unset for "
            f"{DEFAULT_RECHECK_GATEWAYS}. Nothing was sent."
        )
    return tuple(gateways)


def _is_https_origin(url: str) -> bool:
    """`https://host` or `https://host:port`, and nothing after it.

    Compared against the URL rebuilt from its own host and port, which also
    catches a bare `?` or `#` that parses to an empty query or fragment.
    """
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:  # a port that is not a number
        return False
    return (
        parts.scheme == "https"
        and bool(parts.hostname)
        and "@" not in parts.netloc
        and (port is None or port > 0)
        and url == f"https://{parts.netloc}"
    )


def release_note(
    edition: int, agreement_id: int, cid: str, *, kind: str = "index"
) -> bytes:
    """The note a release of `agreement_id` carries, as bytes to send.

    `kind` names the product kind whose prefix the note takes, `index` or
    `travel`: the kind the manifest's `file` names. It defaults to the index,
    so a caller written before there was a second kind gets the note it
    always got.

    Raises `ValueError` for anything that would not read back through
    `NOTE_PATTERN`: a reader ignores such a note, so sending it would record a
    link nobody can find, on a transaction that cannot be sent again.
    """
    product = KINDS_BY_NAME.get(kind)
    if product is None:
        raise ValueError(
            f"{kind!r} is not a product kind: the kinds are "
            + ", ".join(repr(name) for name in KINDS_BY_NAME)
        )
    text = f"{product.note_prefix}{edition}:agreement:{agreement_id}:cid:{cid}"
    if not NOTE_PATTERN.fullmatch(text):
        raise ValueError(
            f"{text!r} is not a release note: the edition must be a positive "
            "whole number, the agreement id a whole number, and the CID "
            "letters and digits only"
        )
    note = text.encode("ascii")
    if len(note) > NOTE_MAX_BYTES:
        raise ValueError(
            f"the release note is {len(note)} bytes, over the protocol's "
            f"{NOTE_MAX_BYTES}"
        )
    return note


def read_manifest(path: Path) -> dict[str, Any]:
    """The edition manifest, refused unless it names its committed file.

    `sha256` is what the pool committed to, and `files` is what the gateway
    check hashes. If the entry for `file` disagreed with `sha256`, the gateway
    check could pass on bytes that are not the committed ones. `file` is one
    kind's committed file for the manifest's `edition` -- `edition-N.json` or
    `trip-N.json` -- and that is the kind the release note takes. Every name in
    `files` is a single path segment, so each file is fetched under the CID and
    nowhere else.
    """
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as error:
        raise SystemExit(
            f"{path}: the manifest is not valid JSON ({error}). Nothing was sent."
        ) from error
    if not isinstance(manifest, dict):
        raise SystemExit(
            f"{path}: the manifest is a {type(manifest).__name__}, not a JSON "
            "object. Nothing was sent."
        )
    edition = manifest.get("edition")
    digest = manifest.get("sha256")
    committed = manifest.get("file")
    files = manifest.get("files")
    # A bool is an int to isinstance, and `true` is not an edition number.
    if isinstance(edition, bool) or not isinstance(edition, int) or edition < 1:
        raise SystemExit(
            f"{path}: `edition` is {edition!r}, not a positive integer. "
            "Nothing was sent."
        )
    if not isinstance(digest, str) or not SHA256_HEX.fullmatch(digest):
        raise SystemExit(
            f"{path}: `sha256` is {digest!r}, not a lowercase sha256. Nothing was sent."
        )
    try:
        committed_kind(committed, edition)
    except ValueError as error:
        raise SystemExit(f"{path}: {error} Nothing was sent.") from error
    if not isinstance(files, dict):
        raise SystemExit(
            f"{path}: `files` is {files!r}, not an object mapping each file "
            "name to its sha256. Nothing was sent."
        )
    for name, listed in files.items():
        if not FILE_NAME.fullmatch(name):
            raise SystemExit(
                f"{path}: the file name {name!r} is not a single path segment "
                "of letters, digits, '.', '_' and '-' that starts with a letter "
                "or a digit. Every file is fetched under the CID and nowhere "
                "else. Nothing was sent."
            )
        if not isinstance(listed, str) or not SHA256_HEX.fullmatch(listed):
            raise SystemExit(
                f"{path}: the entry for {name!r} is {listed!r}, not a lowercase "
                "sha256. Nothing was sent."
            )
    listed = files.get(committed)
    if listed != digest:
        raise SystemExit(
            f"{path}: the entry for the committed file {committed!r} is "
            f"{listed!r}, not the manifest's sha256 {digest}. The gateway check "
            "would not be checking the bytes the pool committed to. Nothing was "
            "sent."
        )
    return manifest


def check_releasable(
    record: Agreement,
    manifest: dict[str, Any],
    *,
    verifier: str,
    chain_now: int,
    runway_seconds: int,
) -> None:
    """Refuse, naming every reason at once, unless this release can land.

    Each check mirrors something the contract asserts or something a release
    cannot take back. A refusal here sends nothing; a refusal on chain would
    come after the gateway fetch, and a wrong release cannot be refused at all.
    """
    problems = []
    if record.state != STATE_FUNDED:
        problems.append(
            f"its state is {record.state}; only FUNDED ({STATE_FUNDED}) -- "
            "every seat sold, not yet released -- can be released"
        )
    if record.verifier != verifier:
        problems.append(
            f"its verifier is {record.verifier}, and this key signs for "
            f"{verifier}. The contract accepts a release from nobody else"
        )
    if record.commit_hash.hex() != manifest["sha256"]:
        problems.append(
            f"it committed to {record.commit_hash.hex()}, and this manifest is "
            f"edition {manifest['edition']} at {manifest['sha256']}"
        )
    remaining = record.deadline - chain_now
    if remaining < runway_seconds:
        problems.append(
            f"its deadline is {remaining}s away by the chain's clock, and this "
            f"step needs at least {runway_seconds}s"
        )
    if problems:
        raise SystemExit(
            "refusing to release, and nothing was sent:\n  - " + "\n  - ".join(problems)
        )


def verifier_mnemonic(network: str, files: dict[str, Path]) -> str:
    """The verifier's key for `network`, from that network's own file.

    Read with `dotenv_values`, never loaded into the environment: nothing else
    in this process has any business with it.
    """
    path = files.get(network)
    if path is None:
        raise SystemExit(
            f"no verifier key file is configured for network {network}. "
            "Nothing was sent."
        )
    phrase = dotenv_values(path).get(VERIFIER_MNEMONIC) if path.exists() else None
    if not phrase:
        raise SystemExit(
            f"{VERIFIER_MNEMONIC} is not set in {path}. A release is signed by "
            "the agreement's verifier, and this script reads that key from the "
            "file for the network the settings name. Nothing was sent."
        )
    return phrase


def chain_timestamp(algod: Any) -> int:
    """The last block's timestamp: what the contract compares the deadline to."""
    return int(algod.block_info(algod.status()["last-round"])["block"]["ts"])


def verify_through_gateways(
    cid: str, manifest: dict[str, Any], *, gateways: tuple[str, ...], timeout: float
) -> bool:
    """Fetch every inventoried file under `cid` and hash it: the buyer's check.

    A file counts as verified as soon as one gateway serves the right bytes;
    gateways disagree about propagation, and one being slow is not a finding.
    A gateway serving the wrong bytes is, and is reported as WRONG BYTES.
    """
    print(f"\nverifying {cid}")
    print(f"  {len(manifest['files'])} files, gateways: {', '.join(gateways)}")

    pending = dict(sorted(manifest["files"].items()))
    errors: dict[str, str] = {}
    delay = VERIFY_BASE_SECONDS

    for attempt in range(1, VERIFY_ATTEMPTS + 1):
        for name, digest in list(pending.items()):
            for gateway in gateways:
                url = f"{gateway}/ipfs/{cid}/{name}"
                try:
                    response = httpx.get(url, timeout=timeout, follow_redirects=True)
                except httpx.HTTPError as error:
                    errors[name] = f"{type(error).__name__}: {error}"
                    continue
                if response.status_code != 200:
                    errors[name] = f"HTTP {response.status_code}"
                    continue
                found = hashlib.sha256(response.content).hexdigest()
                if found == digest:
                    print(f"  ok       {name}  via {gateway}")
                    pending.pop(name)
                    errors.pop(name, None)
                    break
                # Not propagation, and a retry will not fix it: the gateway
                # served bytes that are not the ones published for this name.
                # Kept pending so the check fails, and reported as what it is.
                errors[name] = f"WRONG BYTES: got {found[:12]}, published {digest[:12]}"
        if not pending:
            return True
        if attempt < VERIFY_ATTEMPTS:
            print(
                f"  {len(pending)} not served yet, waiting {delay}s "
                f"({attempt}/{VERIFY_ATTEMPTS})"
            )
            time.sleep(delay)
            delay = min(delay * 2, VERIFY_MAX_SECONDS)

    for name in pending:
        print(f"  FAILED   {name}  ({errors.get(name, 'no response')})")
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agents.release_edition")
    parser.add_argument("--agreement-id", type=int, required=True)
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="the edition's manifest, e.g. editions/edition-1-manifest.json",
    )
    parser.add_argument(
        "--cid",
        required=True,
        help="the CID the pinning provider reported for the bundle",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run every check, including the gateway fetch, and send nothing",
    )
    parser.add_argument(
        "--skip-gateway-check",
        action="store_true",
        help="release without fetching the bundle through public gateways, "
        "when this machine cannot reach them",
    )
    parser.add_argument(
        "--min-runway-seconds",
        type=int,
        default=DEFAULT_MIN_RUNWAY_SECONDS,
        help="refuse to start with less time than this before the deadline",
    )
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args(argv)

    # Everything that needs no key and no network, first.
    manifest = read_manifest(args.manifest)
    # Cannot raise: `read_manifest` has already refused any other `file`.
    kind = committed_kind(manifest["file"], manifest["edition"])
    try:
        note = release_note(
            manifest["edition"], args.agreement_id, args.cid, kind=kind.name
        )
    except ValueError as error:
        raise SystemExit(
            f"cannot build the release note: {error}. Nothing was sent."
        ) from error
    # Loaded here rather than at import: importing this module -- as its
    # tests do -- must not copy a developer's .env into the process.
    load_dotenv()
    gateways = recheck_gateways(os.environ)
    settings = load_settings()
    phrase = verifier_mnemonic(settings.network, NETWORK_ENV_FILES)

    algod = algod_client.AlgodClient(settings.algod_token, settings.algod_url)
    private_key = mnemonic.to_private_key(phrase)
    verifier = account.address_from_private_key(private_key)
    escrow = EscrowClient(
        algod,
        app_id=settings.app_id,
        app_account=settings.app_account,
        usdc_asa_id=settings.usdc_asa_id,
        admin_address=verifier,
        admin_signer=AccountTransactionSigner(private_key),
    )

    def releasable(runway_seconds: int) -> Agreement:
        record = escrow.read_agreement(args.agreement_id)
        if record is None:
            raise SystemExit(
                f"agreement {args.agreement_id} has no record: it was closed, "
                "or never existed. Nothing was sent."
            )
        check_releasable(
            record,
            manifest,
            verifier=verifier,
            chain_now=chain_timestamp(algod),
            runway_seconds=runway_seconds,
        )
        return record

    releasable(args.min_runway_seconds)
    print(f"network    {settings.network}, application {settings.app_id}")
    print(f"agreement  {args.agreement_id}, FUNDED, verifier {verifier}")
    # Labelled by the committed file's stem, so the index kind's line reads
    # exactly as it did before there was a second kind.
    print(f"{kind.stem:<10} {manifest['edition']}, sha256 {manifest['sha256']}")
    print(f"note       {note.decode('ascii')}")

    if args.skip_gateway_check:
        print(
            "\ngateway check skipped (--skip-gateway-check): the note records "
            "a CID nobody fetched"
        )
    elif not verify_through_gateways(
        args.cid, manifest, gateways=gateways, timeout=args.timeout
    ):
        raise SystemExit(
            "the bundle was not served correctly under that CID, so nothing "
            "was sent. A cold gateway is not a bad pin: wait a few minutes, "
            "then run this again with --dry-run. WRONG BYTES above is not a "
            "wait -- it is the wrong CID or the wrong bundle."
        )

    # The fetch can take minutes. Read the pool and the clock again, right
    # before the one call that cannot be taken back.
    record = releasable(FINAL_MARGIN_SECONDS)

    if args.dry_run:
        print("\ndry run: every check passed, nothing was sent")
        return 0

    txid = escrow.release_hash(args.agreement_id, record.commit_hash, note=note)
    confirmed = algod.pending_transaction_info(txid)
    carried = base64.b64decode(confirmed["txn"]["txn"].get("note", ""))
    if carried != note:
        raise SystemExit(
            f"released in {txid}, but the confirmed transaction carries the note "
            f"{carried!r}, not {note!r}. The release cannot be sent again: "
            "record the CID by hand and report this."
        )
    print(f"\nreleased agreement {args.agreement_id} in {txid}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
