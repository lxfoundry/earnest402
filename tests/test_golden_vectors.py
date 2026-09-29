"""The committed vectors must be what the dumper produces today.

Without this the vectors are a snapshot of a builder that no longer exists:
the TypeScript suite would keep passing against them while the Python
builder -- the one that settles real money -- had moved on.
"""

from __future__ import annotations

import json
from pathlib import Path

from agents.dump_golden_vectors import OUTPUT, build_vectors


def test_committed_vectors_match_the_builder():
    committed = json.loads(Path(OUTPUT).read_text(encoding="utf-8"))
    assert committed == build_vectors(), (
        "web/tests/golden/join-group.json is stale. Re-run "
        "`python -m agents.dump_golden_vectors` and review the diff. If it is "
        "not the change you meant to make, the builder regressed."
    )


def test_the_dumper_is_deterministic():
    assert build_vectors() == build_vectors()


def test_every_vector_pins_three_legs_and_the_axfer_index():
    """The signing indexes are the builder's own, not a literal restated here.

    The dumper emits what `build_join_group` computed, so this assertion fails
    if the Python side ever moves them -- which a vector that spelled them out
    on both sides could not have caught.
    """
    for vector in build_vectors()["vectors"]:
        assert len(vector["expected"]["unsigned"]) == 3, vector["name"]
        assert vector["expected"]["paymentIndex"] == 1, vector["name"]
        assert vector["expected"]["indexesToSign"] == [1, 2], vector["name"]


def test_agreement_ids_survive_a_javascript_json_parse():
    """Serialised as strings on purpose: the max-uint64 case is beyond
    Number.MAX_SAFE_INTEGER, and a JSON number would come back rounded."""
    for vector in build_vectors()["vectors"]:
        assert isinstance(vector["input"]["agreementId"], str), vector["name"]
