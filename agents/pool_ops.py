"""Drive one agreement's post-funding lifecycle from the command line.

`agents/create_pool.py` opens a pool. Nothing closed one. Release, expire,
refund and close are reachable only through `api/escrow.py::EscrowClient`,
which meant that until now the only way to release a pool -- including the
first real one -- was an interactive interpreter typed under pressure. This is
that, written down.

Signing identity is per subcommand and always explicit. `release` signs as the
agreement's verifier because the contract accepts nobody else; everything else
signs as whoever `--as` names, because `expire`, `refund_next`, `claim_refund`
and `close` are permissionless and demonstrating that is half of what the
escrow promises. A buyer whose counterparty walked away must have a way out
without the operator's cooperation.

`drain` is the recovery primitive: it walks an agreement in any state through
to deletion, resuming from the refund cursor rather than counting from zero,
because a run that died halfway through a refund pass has already paid some
seats and must not pay them twice.

Usage:
    python -m agents.pool_ops show    --agreement-id 5
    python -m agents.pool_ops release --agreement-id 5 --sha256 <64 hex> [--no-edition-note]
    python -m agents.pool_ops expire  --agreement-id 5 --as buyer --wait
    python -m agents.pool_ops refund  --agreement-id 5 --count 4 --as buyer
    python -m agents.pool_ops claim   --agreement-id 5 --seat 2 --as admin
    python -m agents.pool_ops close   --agreement-id 5 --as buyer
    python -m agents.pool_ops drain   --agreement-id 5 --as buyer --wait
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

from algosdk import account, encoding, mnemonic
from algosdk.atomic_transaction_composer import AccountTransactionSigner
from dotenv import dotenv_values

from api.config import ConfigError, Settings, load_settings
from api.escrow import (
    CONDITION_HASH,
    REFUND_BATCH_CEILING,
    STATE_EXPIRED,
    STATE_FILLED,
    STATE_FUNDED,
    STATE_OPEN,
    STATE_REFUNDED,
    STATE_REFUNDING,
    STATE_RELEASED,
    Agreement,
    EscrowClient,
    box_mbr,
    fee_reserve,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"

# What an unoccupied roster seat decodes to.
ZERO_ADDRESS = encoding.encode_address(bytes(32))

# Which .env key signs for which role. The verifier is here because `release`
# needs it; it is never a valid `--as` for anything else, since the payee on a
# refund always comes off the roster and the verifier holds no USDC.
ROLE_TO_MNEMONIC_ENV = {
    "admin": "ADMIN_MNEMONIC",
    "verifier": "E2E_VERIFIER_MNEMONIC",
    "buyer": "E2E_BUYER_MNEMONIC",
    "treasury": "E2E_TREASURY_MNEMONIC",
}

STATE_NAMES = {
    STATE_OPEN: "OPEN",
    STATE_FUNDED: "FUNDED",
    STATE_FILLED: "FILLED",
    STATE_RELEASED: "RELEASED",
    STATE_EXPIRED: "EXPIRED",
    STATE_REFUNDING: "REFUNDING",
    STATE_REFUNDED: "REFUNDED",
}


def _values() -> dict[str, str]:
    return {k: v for k, v in dotenv_values(ENV_PATH).items() if v}


def _private_key_for(role: str, values: dict[str, str]) -> str:
    env_key = ROLE_TO_MNEMONIC_ENV[role]
    phrase = values.get(env_key)
    if not phrase:
        raise SystemExit(f"{env_key} is not set in .env, so `--as {role}` cannot sign")
    return mnemonic.to_private_key(phrase)


def _address_for(role: str, values: dict[str, str]) -> str:
    return account.address_from_private_key(_private_key_for(role, values))


def _settings() -> Settings:
    """The application and asset this CLI will act on, cross-checked.

    Read through `load_settings` rather than off `.env` directly, because it
    is what enforces that the configured asset and application belong to the
    configured network. `release` is the one call here that moves a pool's
    whole balance, and day 3 sends it against MainNet: a TestNet asset id left
    in `.env` should stop the command, not be defaulted past.
    """
    try:
        return load_settings()
    except ConfigError as exc:
        raise SystemExit(f"configuration is unusable: {exc}") from exc


def _client_for(
    role: str, values: dict[str, str], settings: Settings, algod: Any
) -> EscrowClient:
    private_key = _private_key_for(role, values)
    return EscrowClient(
        algod,
        app_id=settings.app_id,
        app_account=settings.app_account,
        usdc_asa_id=settings.usdc_asa_id,
        admin_address=account.address_from_private_key(private_key),
        admin_signer=AccountTransactionSigner(private_key),
    )


def _require(client: EscrowClient, agreement_id: int) -> Agreement:
    record = client.read_agreement(agreement_id)
    if record is None:
        raise SystemExit(
            f"agreement {agreement_id} has no box. `close` deletes both boxes, "
            "so this means it is already closed -- or never existed."
        )
    return record


def show(client: EscrowClient, agreement_id: int) -> None:
    """Print the decoded record and roster. Reads only."""
    record = _require(client, agreement_id)
    min_fee = client.min_fee()
    print(f"agreement {agreement_id}")
    print(f"  state           {STATE_NAMES.get(record.state, record.state)}")
    print(f"  condition       {'hash' if record.condition == 0 else 'quorum'}")
    print(f"  seats           {record.seats}/{record.max_seats}")
    print(f"  share_price     {record.share_price}")
    print(f"  total_held      {record.total_held}")
    print(f"  refund_cursor   {record.refund_cursor}")
    print(f"  unclaimed_seats {record.unclaimed_seats}")
    remaining = record.deadline - int(time.time())
    print(f"  deadline        {record.deadline} ({remaining:+d}s from now)")
    print(f"  commit_sha256   {record.commit_hash.hex()}")
    print(f"  beneficiary     {record.beneficiary}")
    print(f"  verifier        {record.verifier}")
    print(f"  creator         {record.creator}")
    print(f"  box_mbr         {box_mbr(record.max_seats)}")
    print(f"  fee_reserve     {fee_reserve(record.max_seats, min_fee)}")
    # The roster box is pre-sized to `max_seats` at creation, so this always
    # prints `max_seats` rows however many seats were actually taken. The tail
    # is the zero address, not an absence.
    print("  roster")
    for seat, (payer, amount) in enumerate(client.read_roster(agreement_id)):
        if payer == ZERO_ADDRESS:
            marker = "  <- empty"
        elif amount == 0:
            marker = "  <- paid out"
        else:
            marker = ""
        print(f"    {seat}  {payer}  {amount}{marker}")


def _chain_timestamp(algod: Any) -> int:
    """The timestamp the contract will compare against.

    `Global.latest_timestamp` is the previous block's, so the wall clock is not
    the authority here and reading it instead can be optimistic by a block.
    """
    return int(algod.block_info(algod.status()["last-round"])["block"]["ts"])


def _wait_past_deadline(algod: Any, deadline: int, *, slack: int = 5) -> None:
    """Block until the chain's own clock is past `deadline`."""
    while True:
        block_ts = _chain_timestamp(algod)
        if block_ts >= deadline + slack:
            return
        print(f"  waiting: chain clock {block_ts}, need {deadline + slack}")
        time.sleep(10)


def _can_receive(algod: Any, address: str, asset_id: int) -> bool:
    """Whether an inner transfer of the asset into `address` can succeed.

    Mirrors the contract's own `_can_receive`, both halves of it: a frozen
    holding is an opted-in holding and still rejects every transfer.
    """
    for holding in algod.account_info(address).get("assets", []):
        if holding.get("asset-id") == asset_id:
            return not holding.get("is-frozen", False)
    return False


def _guard_expire(
    record: Agreement,
    agreement_id: int,
    *,
    algod: Any,
    sender: str,
    asset_id: int,
    wait: bool,
) -> None:
    """Refuse, or sit out the deadline for, an `expire` the contract will reject.

    `expire` takes its immediate path on exactly one shape -- no seat sold and
    the creator asking -- and requires the deadline on every other, except
    FILLED, which the deadline does not govern at all. Mirrored here rather
    than left to the chain because a rejected call, though never committed and
    so never charged a fee, is still a wasted round trip that comes back as a
    program counter rather than the reason it was refused.
    """
    if record.state not in (STATE_OPEN, STATE_FUNDED, STATE_FILLED):
        raise SystemExit(
            f"agreement {agreement_id} is "
            f"{STATE_NAMES.get(record.state, record.state)} and the contract "
            "expires only from OPEN, FUNDED or FILLED. This call would be "
            "rejected on chain."
        )

    # FILLED is the one state the deadline does not govern. It expires only as
    # a stranded-beneficiary rescue -- when the beneficiary can no longer
    # receive -- because otherwise `release_quorum` and `expire` would both be
    # callable and the outcome would turn on transaction ordering. Waiting does
    # not make this one reachable, so it is refused outright rather than slept
    # on.
    if record.state == STATE_FILLED and _can_receive(
        algod, record.beneficiary, asset_id
    ):
        raise SystemExit(
            f"agreement {agreement_id} is FILLED and its beneficiary "
            f"{record.beneficiary} can still receive the asset, so the "
            "contract will reject `expire` however long it waits: a "
            "filled pool is released with `release_quorum`, which this "
            "CLI does not send. Sending the call anyway would only be "
            "rejected."
        )

    if record.seats == 0 and sender == record.creator:
        return

    remaining = record.deadline - _chain_timestamp(algod)
    if remaining <= 0:
        return
    if wait:
        _wait_past_deadline(algod, record.deadline)
        return
    why = (
        f"{record.seats} seat(s) are sold"
        if record.seats
        else f"{sender} is not the creator ({record.creator})"
    )
    raise SystemExit(
        f"agreement {agreement_id} cannot be expired yet: {why}, so "
        f"the deadline governs and it is {remaining}s away. Re-run "
        "with --wait to sit it out. Sending the call now would be "
        "rejected on chain."
    )


def drain(
    client: EscrowClient,
    agreement_id: int,
    *,
    algod: Any,
    sender: str,
    asset_id: int,
    wait: bool,
) -> None:
    """Walk an agreement to deletion from whatever state it is in.

    `tests/e2e/harness.reclaim` is the same idea for the single-seat delivery
    escrow and stops after one refund batch, which is right there and wrong
    here: a five-seat roster needs two passes, and a roster a previous run
    half-drained needs however many are left. The cursor on the record says
    how many, so this reads it rather than counting from zero.

    Every `expire` it sends goes through `_guard_expire` first, so a call the
    contract was always going to reject is refused here, with its reason,
    rather than sent to come back as a program counter.
    """
    record = client.read_agreement(agreement_id)
    if record is None:
        print(f"agreement {agreement_id} is already closed")
        return

    if record.state in (STATE_OPEN, STATE_FUNDED, STATE_FILLED):
        _guard_expire(
            record,
            agreement_id,
            algod=algod,
            sender=sender,
            asset_id=asset_id,
            wait=wait,
        )
        print(f"  expire {agreement_id}")
        client.expire(agreement_id)
        record = _require(client, agreement_id)

    # EXPIRED is not a terminal state and `close` will not accept it, even on
    # an agreement nobody joined: the cursor has to finish a pass before the
    # record reads REFUNDED. So the batch is never smaller than one, and a
    # zero-seat agreement takes exactly one call that pays nobody --
    # `tests/e2e/harness.reclaim` does the same thing for the same reason.
    while record.state in (STATE_EXPIRED, STATE_REFUNDING):
        outstanding = record.seats - record.refund_cursor
        count = max(1, min(REFUND_BATCH_CEILING, outstanding))
        print(f"  refund_next {agreement_id} count={count}")
        client.refund_next(agreement_id, count)
        advanced = _require(client, agreement_id)
        if (advanced.state, advanced.refund_cursor) == (
            record.state,
            record.refund_cursor,
        ):
            raise SystemExit(
                f"refund_next left agreement {agreement_id} at cursor "
                f"{advanced.refund_cursor}/{advanced.seats} in state "
                f"{STATE_NAMES.get(advanced.state, advanced.state)}. Stopping "
                "rather than looping: something is refusing to advance and "
                "another call would only spend another fee."
            )
        record = advanced

    if record.unclaimed_seats:
        raise SystemExit(
            f"agreement {agreement_id} has {record.unclaimed_seats} seat(s) the "
            "refund pass skipped, and `close` is blocked until every one is "
            "claimed. A seat is skipped when its payer cannot receive the asset "
            "-- usually because the account was closed out or opted out. Re-opt "
            "that account in, then run `pool_ops claim --agreement-id "
            f"{agreement_id} --seat <k>` for each. claim_refund has no sender "
            "check, so any funded account can send it."
        )

    if record.state in (STATE_RELEASED, STATE_REFUNDED):
        print(f"  close {agreement_id}")
        client.close(agreement_id)
        print(f"agreement {agreement_id} closed")
        return

    raise SystemExit(
        f"agreement {agreement_id} is {STATE_NAMES.get(record.state, record.state)} "
        "and drain does not know how to advance it"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pool_ops", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def with_common(p, *, role_default="admin"):
        p.add_argument("--agreement-id", type=int, required=True)
        p.add_argument(
            "--as",
            dest="role",
            choices=sorted(ROLE_TO_MNEMONIC_ENV),
            default=role_default,
            help=f"which .env key signs the call (default: {role_default})",
        )
        return p

    with_common(sub.add_parser("show", help="print the record and roster"))
    release = sub.add_parser("release", help="release_hash, signed by the verifier")
    release.add_argument("--agreement-id", type=int, required=True)
    release.add_argument("--sha256", required=True, help="the delivered bytes' digest")
    release.add_argument(
        "--no-edition-note",
        action="store_true",
        help="release a multi-seat test pool, which has no edition to link",
    )

    expire = with_common(sub.add_parser("expire", help="expire the agreement"))
    expire.add_argument("--wait", action="store_true", help="block past the deadline")

    refund = with_common(sub.add_parser("refund", help="refund the next seats"))
    refund.add_argument("--count", type=int, default=1)

    claim = with_common(sub.add_parser("claim", help="claim one skipped seat"))
    claim.add_argument("--seat", type=int, required=True)

    with_common(sub.add_parser("close", help="close and return the deposit"))

    drain_p = with_common(sub.add_parser("drain", help="expire, refund, close"))
    drain_p.add_argument("--wait", action="store_true", help="block past the deadline")

    args = parser.parse_args(argv)

    # Lazy: `agents.common` calls load_dotenv() at import time.
    from agents.common import get_algod_client

    values = _values()
    settings = _settings()
    algod = get_algod_client()

    if args.command == "release":
        client = _client_for("verifier", values, settings, algod)
        signs_for = _address_for("verifier", values)
        digest = bytes.fromhex(args.sha256)
        if len(digest) != 32:
            raise SystemExit(f"--sha256 must be 32 bytes, got {len(digest)}")
        record = _require(client, args.agreement_id)
        # A multi-seat `hash` agreement is what an edition pool is, and an
        # edition pool is released with its CID in the release note
        # (docs/specs/edition-release.md). This command sends no note, and the
        # contract accepts one release per agreement, so a pool released here
        # by mistake has lost its link for good. Refused unless the operator
        # says outright that no edition is involved -- a test pool.
        if (
            record.condition == CONDITION_HASH
            and record.max_seats > 1
            and not args.no_edition_note
        ):
            raise SystemExit(
                f"agreement {args.agreement_id} is a {record.max_seats}-seat pool. "
                "An edition pool is released by `python -m agents.release_edition`, "
                "which writes the edition's CID into the release note; this command "
                "sends no note, and a release cannot be sent twice. For a test pool "
                "with no edition, re-run with --no-edition-note."
            )
        # Checked here rather than left to the chain: a release rejected on
        # chain comes back as a program counter, and the reason it failed --
        # that this key is not the agreement's verifier -- is exactly the
        # thing the opcodes cannot say.
        if record.verifier != signs_for:
            raise SystemExit(
                f"agreement {args.agreement_id}'s verifier is {record.verifier}, "
                f"but E2E_VERIFIER_MNEMONIC signs for {signs_for}. Only the "
                "agreement's own verifier can release it, and that address is "
                "fixed at creation with no method to rebind it."
            )
        if record.commit_hash != digest:
            raise SystemExit(
                f"agreement {args.agreement_id} commits to "
                f"{record.commit_hash.hex()}, not {digest.hex()}. The hash is "
                "fixed at creation with no method to rebind it, so releasing "
                "against a different digest is rejected on chain."
            )
        print(client.release_hash(args.agreement_id, digest))
        return 0

    client = _client_for(args.role, values, settings, algod)

    if args.command == "show":
        show(client, args.agreement_id)
    elif args.command == "expire":
        _guard_expire(
            _require(client, args.agreement_id),
            args.agreement_id,
            algod=algod,
            sender=_address_for(args.role, values),
            asset_id=settings.usdc_asa_id,
            wait=args.wait,
        )
        print(client.expire(args.agreement_id))
    elif args.command == "refund":
        print(client.refund_next(args.agreement_id, args.count))
    elif args.command == "claim":
        # The payee always comes off the roster -- `claim_refund` pays
        # `roster[seat]`'s recorded payer whoever sends it -- so `--as` here
        # chooses who pays the fee, never who gets the money.
        private_key = _private_key_for(args.role, values)
        print(
            client.claim_refund(
                args.agreement_id,
                args.seat,
                sender=account.address_from_private_key(private_key),
                signer=AccountTransactionSigner(private_key),
            )
        )
    elif args.command == "close":
        print(client.close(args.agreement_id))
    elif args.command == "drain":
        drain(
            client,
            args.agreement_id,
            algod=algod,
            sender=_address_for(args.role, values),
            asset_id=settings.usdc_asa_id,
            wait=args.wait,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
