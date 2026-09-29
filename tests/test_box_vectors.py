"""The committed box vectors must be what the dumper produces today.

Same discipline as `test_golden_vectors.py`: without this the vectors are a
snapshot of a layout that may have moved, and the TypeScript decoder would
keep passing against them while the record the contract actually writes had
changed underneath.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest import mock

import pytest
from algosdk import encoding

from agents import dump_box_vectors
from agents.dump_box_vectors import OUTPUT, build_vectors
from api.escrow import AGREEMENT_BYTES, SEAT_BYTES


def test_committed_vectors_match_the_dumper():
    committed = json.loads(Path(OUTPUT).read_text(encoding="utf-8"))
    assert committed == build_vectors(), (
        "web/tests/golden/escrow-boxes.json is stale. Re-run "
        "`python -m agents.dump_box_vectors` and review the diff. If it is not "
        "the change you meant to make, the box layout moved."
    )


def test_the_dumper_is_deterministic():
    assert build_vectors() == build_vectors()


def test_every_agreement_is_the_width_the_decoder_assumes():
    """The whole point of the fixture: fixed offsets over a fixed width.

    A record of another size decodes as plausible garbage rather than
    failing, so the width is the first thing worth pinning.
    """
    for vector in build_vectors()["vectors"]:
        raw = base64.b64decode(vector["agreement"]["raw"])
        assert len(raw) == AGREEMENT_BYTES, vector["name"]


def test_rosters_are_whole_seats():
    for vector in build_vectors()["vectors"]:
        raw = base64.b64decode(vector["roster"]["raw"])
        assert len(raw) % SEAT_BYTES == 0, vector["name"]


def test_the_layout_comes_from_the_compiled_contract():
    """Not from a tuple restated beside the decoder it checks.

    `Escrow.arc56.json` is compiler output: it says what the contract's
    `arc4.Struct` actually emits. An encoder built from it cannot inherit a
    mistake from the decoder, which is the only reason the round trip below
    proves anything.
    """
    spec = json.loads(Path(dump_box_vectors.ARC56).read_text(encoding="utf-8"))
    declared = [(m["name"], m["type"]) for m in spec["structs"]["Agreement"]]
    assert dump_box_vectors.AGREEMENT_MEMBERS == declared
    assert dump_box_vectors.AGREEMENT_TUPLE.byte_len() == AGREEMENT_BYTES


def test_the_round_trip_catches_a_field_read_from_the_wrong_place():
    """The property that makes this fixture worth having, demonstrated.

    `beneficiary`, `verifier` and `creator` are three addresses in a row: all
    32 bytes, all valid in each other's slots. A decoder that read one where
    another sits produces a record of exactly the right length carrying
    perfectly plausible addresses, and every width check in this file passes.

    Swapping two of them in the *encoder* stands in for that mistake, since
    the round trip compares the two implementations against each other and
    does not care which side moved. If this test stops failing to build its
    vectors, the round-trip check has lost its teeth.
    """
    swapped = [
        (
            "verifier"
            if name == "beneficiary"
            else "beneficiary"
            if name == "verifier"
            else name,
            kind,
        )
        for name, kind in dump_box_vectors.AGREEMENT_MEMBERS
    ]
    with (
        mock.patch.object(dump_box_vectors, "AGREEMENT_MEMBERS", swapped),
        pytest.raises(AssertionError, match="offsets and the compiled struct"),
    ):
        build_vectors()


def test_every_state_the_client_may_index_is_named():
    """The client reads a byte and indexes an array of names with it, so the
    *order* is load-bearing in a way no offset check can see: swap RELEASED
    and EXPIRED and a buyer owed a refund is told the edition was delivered.
    This is the list its own array is checked against.
    """
    assert build_vectors()["states"] == [
        "OPEN",
        "FUNDED",
        "FILLED",
        "RELEASED",
        "EXPIRED",
        "REFUNDING",
        "REFUNDED",
    ]


def test_the_states_that_carry_opposite_news_both_appear():
    """RELEASED and EXPIRED differ by one byte and mean opposite things."""
    states = {v["agreement"]["expected"]["state"] for v in build_vectors()["vectors"]}
    assert {3, 4} <= states


def test_the_three_seat_states_are_all_present():
    """A decoder that reads a zeroed amount as an empty seat must fail here.

    An occupied seat whose amount is zero has been *paid*; an unoccupied one
    decodes to the zero address. Collapsing the two would report a refunded
    buyer as never having had a seat -- and, worse, would hide a seat that is
    still owed money behind a roster that looks empty.
    """
    zero = encoding.encode_address(bytes(32))
    seen = set()
    for vector in build_vectors()["vectors"]:
        for seat in vector["roster"]["expected"]:
            if seat["payer"] == zero:
                seen.add("empty")
            elif seat["amount"] == "0":
                seen.add("settled")
            else:
                seen.add("owed")
    assert seen == {"empty", "settled", "owed"}
