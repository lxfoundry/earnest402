"""`pool_ops release` refuses a pool that could be an edition's.

An edition pool is released with its CID in the release note, which this
command cannot write, and a release cannot be sent twice. Offline: the key,
the settings, the node and the client are all stood in for, so what runs is
the command's own decision.
"""

from __future__ import annotations

import dataclasses

import pytest
from algosdk import account, mnemonic

import agents.common
from agents import pool_ops
from api.escrow import CONDITION_HASH, CONDITION_QUORUM, STATE_FUNDED, Agreement

DIGEST = "00" * 32


def _release(
    monkeypatch,
    settings_factory,
    *,
    max_seats: int,
    extra: list[str],
    condition: int = CONDITION_HASH,
):
    private_key, address = account.generate_account()
    record = Agreement(
        condition=condition,
        state=STATE_FUNDED,
        deadline=2_000_000_000,
        share_price=100_000,
        min_seats=max_seats,
        max_seats=max_seats,
        seats=max_seats,
        refund_cursor=0,
        unclaimed_seats=0,
        total_held=100_000 * max_seats,
        commit_hash=bytes.fromhex(DIGEST),
        beneficiary=address,
        # Not the signing key, so a release that gets past the edition guard
        # stops at the verifier check instead -- before anything is sent.
        verifier="NOT-THE-SIGNING-KEY",
        creator=address,
    )

    class _Client:
        def read_agreement(self, agreement_id):
            return dataclasses.replace(record)

    phrase = mnemonic.from_private_key(private_key)
    monkeypatch.setattr(pool_ops, "_values", lambda: {"E2E_VERIFIER_MNEMONIC": phrase})
    monkeypatch.setattr(pool_ops, "_settings", lambda: settings_factory())
    monkeypatch.setattr(agents.common, "get_algod_client", lambda: None)
    monkeypatch.setattr(pool_ops, "_client_for", lambda *args: _Client())
    pool_ops.main(["release", "--agreement-id", "7", "--sha256", DIGEST, *extra])


def test_a_multi_seat_pool_is_refused_without_the_explicit_flag(
    monkeypatch, settings_factory
):
    with pytest.raises(SystemExit, match="agents.release_edition"):
        _release(monkeypatch, settings_factory, max_seats=5, extra=[])


def test_a_multi_seat_test_pool_passes_the_guard_with_the_flag(
    monkeypatch, settings_factory
):
    with pytest.raises(SystemExit, match="verifier is NOT-THE-SIGNING-KEY"):
        _release(
            monkeypatch, settings_factory, max_seats=5, extra=["--no-edition-note"]
        )


def test_a_single_seat_agreement_is_not_an_edition_pool(monkeypatch, settings_factory):
    """The delivery escrow releases single-seat agreements, and nothing about
    editions applies to them."""
    with pytest.raises(SystemExit, match="verifier is NOT-THE-SIGNING-KEY"):
        _release(monkeypatch, settings_factory, max_seats=1, extra=[])


def test_a_multi_seat_quorum_agreement_is_not_an_edition_pool(
    monkeypatch, settings_factory
):
    """An edition pool is a `hash` agreement. A quorum pool has no edition to
    link, and pointing its operator at the edition release would be wrong."""
    with pytest.raises(SystemExit, match="verifier is NOT-THE-SIGNING-KEY"):
        _release(
            monkeypatch,
            settings_factory,
            max_seats=5,
            extra=[],
            condition=CONDITION_QUORUM,
        )
