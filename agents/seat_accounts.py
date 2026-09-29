"""Generate and fund the extra payers a multi-seat pool needs.

`join` refuses a payer already on the roster, so a five-seat pool cannot be
filled by one buyer five times -- it needs five keys. The suite has one. This
makes up the difference: throwaway accounts, recorded in `.env`, funded with
ALGO by the admin and seeded with USDC by the buyer they belong to.

Every step is a **top-up rather than a transfer**. An account already holding
`algo_target` is not paid again, one already opted in is not opted in again,
and one already holding `usdc_each` receives nothing. So the second run of an
afternoon costs one `account_info` read per seat and no money, which is what
makes it safe to call at the top of a scenario that may be run ten times
before it passes -- and a rehearsal spends its float on every pass.

Mnemonics are written straight to `.env`, gitignored, and never printed, for
the reason `agents/e2e_bootstrap.py` gives: key material that passes through a
terminal is key material in a scrollback buffer.

**Nothing here may import `agents.common` at module scope, and nothing here
may call `load_dotenv()`.** `agents/common.py` calls it on import, and
`tests/e2e/harness.Account.avm_signer` documents at length why that must not
happen while pytest is collecting an offline run. `dotenv_values` and
`set_key` both read and write the file without touching `os.environ`, so this
module uses those and imports the algod client lazily inside `main`.

Usage:
    python -m agents.seat_accounts status
    python -m agents.seat_accounts provision --count 4 --usdc-each 100000
    python -m agents.seat_accounts return [--close]
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from algosdk import account, mnemonic, transaction
from dotenv import dotenv_values, set_key

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"

USDC_TESTNET_ASA_ID = 10458941

# 0.1 ALGO base minimum balance, 0.1 more for the USDC opt-in, and the rest is
# fee headroom. A seat account signs only zero-fee legs inside the settlement
# group -- `agents/build_join_group.py` prices the buyer's two legs at 0 and
# pools every fee onto the facilitator's sponsor -- so the one fee it ever
# pays for itself is its own opt-in.
SEAT_ALGO_TARGET = 300_000

# Top up only below this floor, and then all the way to the target. The gap
# between the two is what makes re-running free: a seat pays its own opt-in
# fee, so it settles one fee *below* the target and stays there. Topping up to
# an exact target would see that 1,000 microALGO gap on every later run and
# close it again, at a fee each time -- a provisioner that bleeds a little
# every time it is asked whether anything needs doing.
#
# The floor itself is the minimum balance for an account holding one ASA
# (0.2 ALGO) plus room for the opt-in fee, so a seat above it can always still
# receive USDC.
SEAT_ALGO_FLOOR = 210_000


def seat_env_key(index: int) -> str:
    """The `.env` key for seat `index`, 1-based: `E2E_SEAT_3_MNEMONIC`."""
    return f"E2E_SEAT_{index}_MNEMONIC"


def _address_of(phrase: str) -> str:
    return account.address_from_private_key(mnemonic.to_private_key(phrase))


def read_seat_mnemonics(*, env_path: Path = ENV_PATH) -> list[str]:
    """Every recorded seat mnemonic, in index order, stopping at the first gap.

    Stopping rather than skipping: seat numbering is positional everywhere
    else -- the roster, the refund cursor, the scenarios' payer lists -- and a
    list with a hole in it would silently renumber every seat after it.
    """
    values = dotenv_values(env_path) if env_path.exists() else {}
    phrases: list[str] = []
    index = 1
    while phrase := values.get(seat_env_key(index)):
        phrases.append(phrase)
        index += 1
    return phrases


def ensure_seat_mnemonics(count: int, *, env_path: Path = ENV_PATH) -> list[str]:
    """Generate and record whatever is missing so that `count` seats exist.

    An existing key is reused, never replaced -- the rule
    `agents/e2e_bootstrap._account_for` lives by, and for the same reason: a
    replaced key strands the ALGO and the USDC already sitting on it, and on
    TestNet that USDC took a captcha to obtain.
    """
    env_path.touch(exist_ok=True)
    phrases = read_seat_mnemonics(env_path=env_path)
    for index in range(len(phrases) + 1, count + 1):
        private_key, _ = account.generate_account()
        phrase = mnemonic.from_private_key(private_key)
        set_key(str(env_path), seat_env_key(index), phrase, quote_mode="auto")
        phrases.append(phrase)
    return phrases[:count]


@dataclass(frozen=True)
class SeatState:
    """What one seat account currently is, read from the chain."""

    index: int
    address: str
    algo: int
    usdc: int
    opted_in: bool


def _state_of(algod: Any, index: int, address: str, asset_id: int) -> SeatState:
    try:
        info = algod.account_info(address)
    except Exception as exc:  # an unfunded account is not yet known to algod
        if "no accounts found" not in str(exc).lower():
            raise
        return SeatState(index, address, 0, 0, False)
    holdings = {a["asset-id"]: a for a in info.get("assets", [])}
    holding = holdings.get(asset_id)
    return SeatState(
        index=index,
        address=address,
        algo=int(info.get("amount", 0)),
        usdc=int(holding["amount"]) if holding else 0,
        opted_in=holding is not None,
    )


def seat_states(
    algod: Any, *, asset_id: int = USDC_TESTNET_ASA_ID, env_path: Path = ENV_PATH
) -> list[SeatState]:
    """One `account_info` read per recorded seat. Reads only, safe anywhere."""
    return [
        _state_of(algod, index, _address_of(phrase), asset_id)
        for index, phrase in enumerate(read_seat_mnemonics(env_path=env_path), start=1)
    ]


def _send_algo(algod: Any, sender_key: str, receiver: str, amount: int) -> str:
    txn = transaction.PaymentTxn(
        sender=account.address_from_private_key(sender_key),
        sp=algod.suggested_params(),
        receiver=receiver,
        amt=amount,
    )
    txid = algod.send_transaction(txn.sign(sender_key))
    transaction.wait_for_confirmation(algod, txid, 4)
    return txid


def _send_usdc(
    algod: Any,
    sender_key: str,
    receiver: str,
    asset_id: int,
    amount: int,
    close_to: str | None = None,
) -> str:
    """Move `amount`, and with `close_to` set, opt out in the same transaction.

    The opt-out is folded in rather than sent after, because an account that
    still holds an asset cannot be closed: `close_remainder_to` is rejected
    outright with `cannot close: 1 outstanding assets`. So for the close path
    this is a precondition of the payment that follows, not a tidy-up, and
    doing it in one transaction costs one fee instead of two and cannot leave
    the account half-wound-down if the second call fails.
    """
    txn = transaction.AssetTransferTxn(
        sender=account.address_from_private_key(sender_key),
        sp=algod.suggested_params(),
        receiver=receiver,
        amt=amount,
        index=asset_id,
        close_assets_to=close_to,
    )
    txid = algod.send_transaction(txn.sign(sender_key))
    transaction.wait_for_confirmation(algod, txid, 4)
    return txid


def _opt_in(algod: Any, seat_key: str, asset_id: int) -> str:
    address = account.address_from_private_key(seat_key)
    txn = transaction.AssetTransferTxn(
        sender=address,
        sp=algod.suggested_params(),
        receiver=address,
        amt=0,
        index=asset_id,
    )
    txid = algod.send_transaction(txn.sign(seat_key))
    transaction.wait_for_confirmation(algod, txid, 4)
    return txid


def provision_seats(
    algod: Any,
    *,
    count: int,
    asset_id: int,
    usdc_each: int,
    algo_funder_key: str,
    usdc_funder_key: str,
    algo_target: int = SEAT_ALGO_TARGET,
    env_path: Path = ENV_PATH,
) -> list[tuple[str, str]]:
    """Make `count` seats exist, funded, opted in and holding `usdc_each`.

    Returns `[(mnemonic, address)]` in seat order. Takes raw algosdk private
    keys rather than mnemonics or `Account` objects, because
    `tests/e2e/harness.Account.private_key` is already in that form and the
    CLI reaches it with one `mnemonic.to_private_key` -- so one signature
    serves both callers without `agents/` having to import from `tests/`.
    """
    phrases = ensure_seat_mnemonics(count, env_path=env_path)
    funder_usdc_needed = 0

    for index, phrase in enumerate(phrases, start=1):
        seat_key = mnemonic.to_private_key(phrase)
        address = _address_of(phrase)
        state = _state_of(algod, index, address, asset_id)

        if state.algo < SEAT_ALGO_FLOOR:
            _send_algo(algod, algo_funder_key, address, algo_target - state.algo)
        if not state.opted_in:
            _opt_in(algod, seat_key, asset_id)
        if state.usdc < usdc_each:
            funder_usdc_needed += usdc_each - state.usdc

    funder = account.address_from_private_key(usdc_funder_key)
    held = _state_of(algod, 0, funder, asset_id).usdc
    if funder_usdc_needed and held < funder_usdc_needed:
        raise SystemExit(
            f"the USDC funder {funder} holds {held} but seeding {count} seats "
            f"to {usdc_each} each needs {funder_usdc_needed}. TestNet USDC has "
            "no programmatic dispenser; top up at https://faucet.circle.com"
        )

    # Seeded in a second pass so the balance check above is made once against
    # the whole requirement, rather than failing partway through having seeded
    # some seats and not others. It bounds the USDC leg only: the ALGO funding
    # and the opt-ins happen in the first loop, so a run that stops here has
    # already spent those.
    for index, phrase in enumerate(phrases, start=1):
        address = _address_of(phrase)
        state = _state_of(algod, index, address, asset_id)
        if state.usdc < usdc_each:
            _send_usdc(
                algod, usdc_funder_key, address, asset_id, usdc_each - state.usdc
            )

    return [(phrase, _address_of(phrase)) for phrase in phrases]


def return_seats(
    algod: Any,
    *,
    asset_id: int,
    usdc_to: str,
    algo_to: str | None = None,
    close: bool = False,
    env_path: Path = ENV_PATH,
) -> list[tuple[str, int, int]]:
    """Send each seat's USDC back, and optionally close the account out.

    `close=False` is the default and almost always the right choice. Closing
    recovers the 0.2 ALGO minimum balance but destroys the USDC opt-in, and an
    opt-in is exactly what the contract tests before paying a refund: a seat
    that was closed out between runs is *skipped* on `refund_next`, which
    raises `unclaimed_seats` and blocks `close` on the agreement entirely.
    Four accounts left standing cost 0.8 ALGO of parked minimum balance and
    make the next rehearsal a no-op to set up.

    Returns `[(address, usdc_returned, algo_returned)]`, both figures being
    what the receiver actually gained -- `algo_returned` is net of the
    close-out's own fee, not the balance that was swept.
    """
    moved: list[tuple[str, int, int]] = []
    for index, phrase in enumerate(read_seat_mnemonics(env_path=env_path), start=1):
        seat_key = mnemonic.to_private_key(phrase)
        address = _address_of(phrase)
        state = _state_of(algod, index, address, asset_id)
        usdc_returned = 0
        algo_returned = 0
        if close and state.opted_in:
            # Opt out on the way past, even at a zero balance: the close-out
            # below cannot land while this holding exists.
            _send_usdc(algod, seat_key, usdc_to, asset_id, state.usdc, close_to=usdc_to)
            usdc_returned = state.usdc
        elif state.usdc:
            _send_usdc(algod, seat_key, usdc_to, asset_id, state.usdc)
            usdc_returned = state.usdc
        if close:
            # Re-read rather than reusing the snapshot above: the USDC transfer
            # just spent a fee from this same account, so `state.algo` is
            # already one fee stale by the time the close-out is built.
            balance = _state_of(algod, index, address, asset_id).algo
            txn = transaction.PaymentTxn(
                sender=address,
                sp=algod.suggested_params(),
                receiver=algo_to or usdc_to,
                amt=0,
                close_remainder_to=algo_to or usdc_to,
            )
            # A balance that cannot cover the close-out's own fee has nothing
            # to sweep, and sending it would fail rather than return dust.
            if balance > txn.fee:
                txid = algod.send_transaction(txn.sign(seat_key))
                transaction.wait_for_confirmation(algod, txid, 4)
                # `close_remainder_to` sweeps what is left *after* the fee, so
                # the receiver gets the balance less this transaction's own
                # fee -- reporting the balance would overstate every row.
                algo_returned = balance - txn.fee
        moved.append((address, usdc_returned, algo_returned))
    return moved


def _report(states: list[SeatState], *, asset_id: int) -> None:
    """Addresses and balances. Never prints a mnemonic."""
    if not states:
        print("no seat accounts recorded; run `provision --count N`")
        return
    header = f"{'seat':>4}  {'address':58} {'ALGO':>9} {'USDC':>9}  opted in"
    print(header)
    print("-" * len(header))
    for state in states:
        print(
            f"{state.index:>4}  {state.address:58} {state.algo / 1e6:9.3f} "
            f"{state.usdc / 1e6:9.3f}  {'yes' if state.opted_in else 'NO'}"
        )
    short = [s.index for s in states if s.algo < SEAT_ALGO_FLOOR]
    if short:
        print(
            f"\nseats {short} hold less than {SEAT_ALGO_FLOOR} microALGO, which is "
            f"below the minimum balance for an ASA holding. Run `provision`."
        )
    print(f"\nasset {asset_id}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="seat_accounts", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="print each seat's address and balances")

    provision = sub.add_parser(
        "provision", help="generate, fund, opt in and seed seat accounts"
    )
    provision.add_argument("--count", type=int, default=4)
    provision.add_argument(
        "--usdc-each",
        type=int,
        default=100_000,
        help="micro-USDC each seat should hold (default: one seat at 0.1 USDC)",
    )

    give_back = sub.add_parser("return", help="send each seat's USDC back to the buyer")
    give_back.add_argument(
        "--close",
        action="store_true",
        help="also close each account out, destroying its USDC opt-in",
    )

    args = parser.parse_args(argv)

    # Lazy: `agents.common` calls load_dotenv() at import time.
    from agents.common import get_algod_client

    values = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}
    asset_id = int(values.get("USDC_ASA_ID") or USDC_TESTNET_ASA_ID)
    algod = get_algod_client()

    if args.command == "status":
        _report(seat_states(algod, asset_id=asset_id), asset_id=asset_id)
        return 0

    if args.command == "provision":
        admin = values.get("ADMIN_MNEMONIC")
        buyer = values.get("E2E_BUYER_MNEMONIC")
        if not admin or not buyer:
            raise SystemExit(
                "ADMIN_MNEMONIC and E2E_BUYER_MNEMONIC must both be set in "
                ".env; run `python -m agents.e2e_bootstrap`"
            )
        provision_seats(
            algod,
            count=args.count,
            asset_id=asset_id,
            usdc_each=args.usdc_each,
            algo_funder_key=mnemonic.to_private_key(admin),
            usdc_funder_key=mnemonic.to_private_key(buyer),
        )
        _report(seat_states(algod, asset_id=asset_id), asset_id=asset_id)
        return 0

    buyer = values.get("E2E_BUYER_MNEMONIC")
    if not buyer:
        raise SystemExit("E2E_BUYER_MNEMONIC is not set; nowhere to return the float")
    for address, usdc, algo in return_seats(
        algod,
        asset_id=asset_id,
        usdc_to=_address_of(buyer),
        close=args.close,
    ):
        print(f"{address}  returned {usdc} micro-USDC, {algo} microALGO")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
