"""Create one edition's pool. Operator-only, and irreversible.

    python -m agents.create_pool --manifest editions/edition-1-manifest.json \
        --deadline-days 7

Reads the frozen edition's manifest, so the committed hash is the hash of the
file that was actually published rather than one retyped by hand. The contract
offers no way to bind or update `commit_hash` after creation, so this call
freezes the deliverable: no typo fix, no late addition, no re-probe after a
price moves.

The manifest's `file` names what kind of deliverable the pool sells --
`edition-N.json` for the index, `trip-N.json` for a trip file -- and the
creating transaction's note carries that kind's prefix (`create_note`). A
manifest with no `file` is the index's: the manifests this script was first
written against carried only `edition` and `sha256`, and still do in rehearsal.

The deadline is frozen with it. `--deadline-days` is what an edition pool
runs on; `--deadline-minutes` exists so a rehearsal can reach its refund path
in the same sitting rather than tomorrow, and is refused on MainNet, where a
pool that expired before it could fill would be a real one.

Three guards exist because every one of the mistakes they catch is
unrecoverable:

  --dry-run prints every parameter -- including the ALGO deposit this call
  actually spends, which scales with seat count -- and creates nothing. Run
  it first, every time: the alternative is composing the create transaction
  from a manifest that was never checked by eye, and discovering the hash,
  the seat count or the deposit was wrong only after it is already on chain.

  The script refuses to create a second pool while one is still open. Exactly
  one pool is open at a time -- never two, not even at the seam between
  editions -- because the second pool to fill would be funding an edition the
  first one already published: whichever pool fills first freezes and
  releases its own deliverable, and a second open pool at that point is
  selling seats to nothing. This scan is intentionally broader than the
  route's own pinned lookup (api/pool.py's `read_pinned_pool`): it has to
  catch a still-open pool from a previous edition even when this edition's
  `INDEX_SHARE_PRICE_MICRO` or `INDEX_SEATS` has since moved on, or the guard
  would look straight past exactly the pool it exists to catch.

  The verifier must be a key distinct from the admin running this script.
  `release_hash` compares `delivered_bytes_hash` against the `commit_hash`
  already public on the agreement, so the "proof" it checks is trivially
  satisfiable by anyone who can read the box -- the real guarantee is that
  only the `verifier` key can submit it. If that key were the one running
  this script, release would not be gated on anything at all. The most
  consequential of the three: a pool created this way holds real seats, and
  there is no way to add a real gate to it after the fact.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from algosdk import account, mnemonic
from algosdk.atomic_transaction_composer import AccountTransactionSigner
from algosdk.error import AlgodHTTPError
from algosdk.v2client import algod as algod_client
from dotenv import load_dotenv
from x402.mechanisms.avm import ALGORAND_MAINNET_CAIP2

from agents.product_kinds import INDEX, committed_kind
from api.config import load_settings
from api.escrow import EscrowClient, required_deposit
from api.pool import find_any_open_pool

# api/server.py is the only *server* module that loads the environment; this
# script is a separate entrypoint and has to do it for itself, exactly as
# agents/common.py does for the join-path spike scripts.
load_dotenv()

# Measured, not derived from any published limit: a `join` group's opcode
# budget runs out before a wider agreement can be created, so the reachable
# ceiling is lower than the contract's own storage layout would suggest.
# Checked locally so a mistyped INDEX_SEATS is named here, in terms of the
# variable that is wrong, rather than surfacing as an opaque rejection from a
# contract that never explains which of its assertions failed.
MEASURED_SEAT_CEILING = 20

# What an edition pool runs on when neither deadline flag is given. Long
# enough that a pool that is going to fill has filled, and short enough that a
# pool that is not going to fill reaches its refund pass while the edition it
# was selling is still current.
DEFAULT_DEADLINE_DAYS = 7

# Where the deployments' Fly configs live: each names its Fly app and, under
# `[env]`, the `APP_ID` its server answers for.
FLY_CONFIG_DIR = Path(__file__).resolve().parents[1]


def fly_app_serving(app_id: int) -> str | None:
    """The Fly app whose config in this repository serves application
    `app_id`, or None when no config here does.

    The configs are the one place an application is paired with the app that
    serves it, so this is read from them rather than from the environment.
    """
    for path in sorted(FLY_CONFIG_DIR.glob("fly*.toml")):
        config = tomllib.loads(path.read_text(encoding="utf-8"))
        if str(config.get("env", {}).get("APP_ID")) == str(app_id):
            return config.get("app")
    return None


def create_note(manifest: Mapping[str, Any]) -> bytes:
    """The note the creating transaction carries: the manifest's sha256 under
    its kind's prefix, e.g. `earnest:index:edition-1:sha256:<hex>`.

    The kind is the one `file` names. An absent `file` means the index, so a
    manifest carrying only `edition` and `sha256` gets the note it always got;
    a `file` that is present must name one kind's committed file for this
    `edition`, because a note under the wrong prefix would file the pool under
    a kind whose readers never look for it, and the note cannot be rewritten.
    Raises `ValueError` naming the field that is wrong.
    """
    edition = manifest.get("edition")
    # A bool is an int to isinstance, and `true` is not an edition number.
    if isinstance(edition, bool) or not isinstance(edition, int) or edition < 1:
        raise ValueError(f"`edition` is {edition!r}, not a positive integer.")
    kind = (
        INDEX if "file" not in manifest else committed_kind(manifest["file"], edition)
    )
    return f"{kind.note_prefix}{edition}:sha256:{manifest['sha256']}".encode()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="create_pool", description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="path to the frozen edition manifest (an object with `edition` "
        "and `sha256` fields, and `file` naming the committed file, "
        "edition-N.json or trip-N.json; absent means edition-N.json)",
    )
    # Mutually exclusive rather than one `--deadline` taking a unit, because
    # the two are not interchangeable: days are what an edition pool runs on,
    # and minutes exist only so a rehearsal can reach its refund path in the
    # same sitting. Keeping them as separate flags is what lets the MainNet
    # refusal below name the one that must not be used there.
    deadline = parser.add_mutually_exclusive_group()
    deadline.add_argument(
        "--deadline-days",
        type=int,
        default=None,
        help="seats must fill within this many days of creation, or the "
        f"pool is a candidate for a single re-open (default: {DEFAULT_DEADLINE_DAYS})",
    )
    deadline.add_argument(
        "--deadline-minutes",
        type=int,
        default=None,
        help="a deadline in minutes, for a test pool whose refund path has "
        "to be reached in one sitting; refused on MainNet",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print every parameter and create nothing",
    )
    args = parser.parse_args(argv)

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    commit_hex = manifest["sha256"]
    commit_hash = bytes.fromhex(commit_hex)
    if len(commit_hash) != 32:
        raise SystemExit(f"manifest sha256 is not 32 bytes: {commit_hex!r}")
    # Offline, like the sha256 check: the note is written once, on the
    # transaction that creates the pool, and a wrong kind or number in it is
    # as permanent as a wrong hash.
    try:
        note = create_note(manifest)
    except ValueError as error:
        raise SystemExit(f"{args.manifest}: {error} Nothing was created.") from error

    settings = load_settings()

    if settings.index_seats > MEASURED_SEAT_CEILING:
        raise SystemExit(
            f"INDEX_SEATS is {settings.index_seats}, above the measured "
            f"ceiling of {MEASURED_SEAT_CEILING} seats; a pool created that "
            "wide would be unjoinable, because a `join` group's opcode budget "
            "runs out before it could take a seat"
        )

    # Offline, and before the admin key is read, for the same reason as the
    # seat guard: the mistake it catches is one nobody would notice until the
    # pool was already on chain with the wrong deadline, and `create_agreement`
    # offers no way to move a deadline afterwards.
    if args.deadline_minutes is not None:
        if settings.network == ALGORAND_MAINNET_CAIP2:
            raise SystemExit(
                "--deadline-minutes is refused on MainNet: an edition pool's "
                "deadline is counted in days, and a pool created with a "
                "minutes-long one would expire before it could fill, with no "
                "way to extend it. Use --deadline-days."
            )
        if args.deadline_minutes < 1:
            raise SystemExit("--deadline-minutes must be at least 1")
        deadline_seconds = args.deadline_minutes * 60
        deadline_label: dict[str, int] = {"deadline_minutes": args.deadline_minutes}
    else:
        days = (
            DEFAULT_DEADLINE_DAYS if args.deadline_days is None else args.deadline_days
        )
        if days < 1:
            raise SystemExit("--deadline-days must be at least 1")
        deadline_seconds = days * 86_400
        deadline_label = {"deadline_days": days}

    algod = algod_client.AlgodClient(settings.algod_token, settings.algod_url)
    private_key = mnemonic.to_private_key(settings.admin_mnemonic)
    admin_address = account.address_from_private_key(private_key)
    admin_signer = AccountTransactionSigner(private_key)
    escrow = EscrowClient(
        algod,
        app_id=settings.app_id,
        app_account=settings.app_account,
        usdc_asa_id=settings.usdc_asa_id,
        admin_address=admin_address,
        admin_signer=admin_signer,
    )

    # The spec requires the operator to hold `verifier` on a key distinct
    # from `admin` (spec §11.6): `release_hash` compares `delivered_bytes_hash`
    # against the `commit_hash` already public on the agreement, so the
    # "proof" it checks is trivially satisfiable by anyone who can read the
    # box -- the real guarantee is that only the `verifier` key can submit
    # it. If that key were the same one running this script, release would
    # not be gated on anything at all.
    if settings.verifier == admin_address:
        raise SystemExit(
            "verifier must be a distinct key from the admin: the account "
            "that releases must not be the account that creates"
        )

    # A live read of the deployed application's global state and boxes.
    # Read-only, so it is safe to run even under --dry-run -- everything
    # after this point that touches the network is a read until the
    # dry-run check below returns. Deliberately the broad, cross-edition
    # scan (`find_any_open_pool`), not the route's pinned lookup: this guard
    # has to catch a still-open pool from a previous edition even when this
    # edition's share price or seat count has since moved on, and a check
    # scoped to the current configuration -- or to `INDEX_AGREEMENT_ID`,
    # which describes the pool already being served, not the one this call
    # is about to create -- would look straight past it.
    # `create_agreement` asserts the contract is not paused, so this would be
    # rejected on chain anyway -- but as an opaque contract error naming no
    # assertion, at the end of a command the operator had every reason to
    # think was the right one. The same reason the seat-ceiling guard above
    # runs locally: say which precondition failed, in its own terms.
    if escrow.is_paused():
        raise SystemExit(
            "the escrow contract is paused: `create_agreement` asserts it is "
            "not, so this create would be rejected on chain. Unpause before "
            "opening an edition."
        )

    try:
        open_pool = find_any_open_pool(escrow)
    except AlgodHTTPError as error:
        # The scan walks every agreement id, one request each, and that
        # population grows with ordinary `/pin` traffic -- so a shared node's
        # rate limit is a live possibility here, not a hypothetical. A read
        # that failed is not a pool that is absent: refuse rather than guess,
        # because guessing wrong in this direction is the unrecoverable one.
        raise SystemExit(
            f"could not confirm whether a pool is already open: {error}. "
            "Refusing to create. This guard is the only thing between a "
            "transient chain error and two live pools, and a second pool "
            "holding real seats against a second commit hash cannot be "
            "merged back into the first. Retry when algod answers."
        ) from error
    if open_pool is not None:
        raise SystemExit(
            f"agreement {open_pool.agreement_id} is still open "
            f"({open_pool.seats_taken}/{open_pool.seats_total} seats). One "
            "pool at a time: release or expire it before opening the next "
            "edition."
        )

    deadline = int(time.time()) + deadline_seconds
    deposit_micro_algo = required_deposit(settings.index_seats, escrow.min_fee())
    print(
        json.dumps(
            {
                "edition": manifest["edition"],
                "commit_sha256": commit_hex,
                "seats": settings.index_seats,
                "share_price_micro": settings.index_share_price_micro,
                "pool_value_micro": settings.index_seats
                * settings.index_share_price_micro,
                "deadline": deadline,
                **deadline_label,
                "beneficiary": settings.treasury,
                "verifier": settings.verifier,
                # What this call actually spends from the admin account, in
                # micro-ALGO -- the one number in this block that is not
                # already visible somewhere else, and the reason the dry run
                # exists: it scales with seat count and is not refundable
                # until `close`.
                "required_deposit_micro_algo": deposit_micro_algo,
            },
            indent=2,
            sort_keys=True,
        )
    )
    # Shown because it is the only place the kind is recorded: the contract
    # holds the hash, not what the hash is of. A line of its own rather than a
    # key in the report above, whose exact shape a rehearsal asserts.
    print(f"note {note.decode()}")

    # The app the refresh step below names, resolved before anything is
    # created so a config that cannot be read stops the run while it is still
    # free to. The fly config serving this application decides it when the
    # repository holds one: `FLY_APP` comes from whichever env file was
    # loaded, and a second deployment run from the same clone inherits the
    # first one's from `.env`. `FLY_APP` is flyctl's own variable for the
    # `-a/--app` flag, and the fallback for an application no config here
    # serves. It is not Fly's in-machine `FLY_APP_NAME`: this script holds the
    # admin key and runs from the operator's machine, never inside the
    # deployed app, so the variable Fly injects at runtime is never the one
    # present here.
    fly_app = fly_app_serving(settings.app_id) or os.environ.get("FLY_APP")

    if args.dry_run:
        print("dry run: nothing created")
        return 0

    agreement_id = escrow.create_hash_agreement(
        commit_hash=commit_hash,
        share_price=settings.index_share_price_micro,
        deadline=deadline,
        beneficiary=settings.treasury,
        verifier=settings.verifier,
        seats=settings.index_seats,
        # The sha256 in the note field: a timestamped commitment linked to
        # the agreement that carries it, not merely present in a box
        # somewhere.
        note=note,
    )
    print(f"created agreement {agreement_id}")
    # The one manual step between a created pool and a served one. The route
    # reads `INDEX_AGREEMENT_ID` and answers 503 `no_pool_open` until it names
    # this agreement, however correctly it was just created -- so the command
    # is printed in full, ready to paste, with the app resolved above.
    target = fly_app or "<app>"
    print(f"next: fly secrets set INDEX_AGREEMENT_ID={agreement_id} -a {target}")
    if not fly_app:
        print(
            "      (set FLY_APP=<app> in .env to have that filled in; flyctl "
            "reads it too)"
        )
    print("      then POST /index should quote this agreement id")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
