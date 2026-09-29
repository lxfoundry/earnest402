"""The artifacts committed under `editions/`, checked against their manifest.

These files are the published free tier. `editions/README.md` tells a reader
to hash them and compare against the manifest committed beside them, so the
pair has to stay in step -- and nothing else in the suite notices when it does
not.

Drift is easy to introduce and invisible once introduced. An edition's
renderings can be improved after it is frozen, so a re-rendered report changes
these bytes without touching `edition-N.json`; the commitment on chain
survives, but if the files and the manifest are not committed together the
digests published here stop matching the files beside them, and a buyer
following the README concludes they were handed tampered bytes.

`edition-N.json` itself is deliberately not committed -- it is the dataset a
seat buys -- so these tests check the public files and the manifest's
self-consistency, not the dataset digest. They use nothing but `hashlib` and
`json`, which is the whole of what the README asks a reader to use.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

EDITIONS = Path(__file__).resolve().parent.parent / "editions"
MANIFESTS = sorted(EDITIONS.glob("edition-*-manifest.json"))

# The columns a free extract row carries -- all three and no others, as
# `editions/README.md` states them: the catalogued price, the price the route
# actually quoted, and whether it answered. An allowlist rather than a list of
# names to strip, so a column added to an edition later is withheld from the
# free tier until someone decides otherwise.
PUBLIC_COLUMNS = frozenset({"catalogued_price_usdc", "live_price_usdc", "liveness"})


def test_there_is_at_least_one_published_edition() -> None:
    """Otherwise the tests below pass by having nothing to check."""
    assert MANIFESTS, f"no edition manifest committed under {EDITIONS}"


@pytest.mark.parametrize("manifest_path", MANIFESTS, ids=lambda p: p.name)
def test_every_committed_file_matches_the_digest_published_for_it(
    manifest_path: Path,
) -> None:
    """The check `editions/README.md` hands a buyer, run against our own tree."""
    manifest = json.loads(manifest_path.read_bytes())

    checked = 0
    for name, digest in manifest["files"].items():
        published = EDITIONS / name
        if not published.exists():
            # The dataset is sold, not committed. Its digest stays in the
            # manifest -- that is the value bound on chain -- but the file is
            # not in this directory to hash.
            continue
        assert hashlib.sha256(published.read_bytes()).hexdigest() == digest, name
        checked += 1

    assert checked, f"{manifest_path.name} inventories nothing committed here"


@pytest.mark.parametrize("manifest_path", MANIFESTS, ids=lambda p: p.name)
def test_the_full_edition_is_not_in_the_tree(manifest_path: Path) -> None:
    """The full edition is what a seat buys; it is delivered as a CID."""
    manifest = json.loads(manifest_path.read_bytes())

    assert not (EDITIONS / manifest["file"]).exists(), manifest["file"]


@pytest.mark.parametrize("manifest_path", MANIFESTS, ids=lambda p: p.name)
def test_the_free_sample_names_no_route_payee_or_merchant(
    manifest_path: Path,
) -> None:
    """The one-way door, asserted against the bytes that went through it.

    Publishing a named third party for free cannot be undone once the bundle
    is pinned, so the committed artifact is checked directly rather than
    trusting that whatever rendered it applied the rule.
    """
    number = json.loads(manifest_path.read_bytes())["edition"]
    sample = json.loads((EDITIONS / f"edition-{number}-sample.json").read_bytes())

    assert sample["rows"], "a teaser with no rows demonstrates nothing"
    for row in sample["rows"]:
        assert set(row) == PUBLIC_COLUMNS, row


@pytest.mark.parametrize("manifest_path", MANIFESTS, ids=lambda p: p.name)
def test_the_sample_rule_states_the_row_count_it_carries(manifest_path: Path) -> None:
    """A published rule a reader can falsify by counting is worse than none.

    The rule opens with its count -- `N rows taken from ...`, or `All N of ...`
    when the extract is the whole population -- so the count is read from that
    position. Searched for anywhere in the text, a count of 4 would be found
    inside the population size 450.
    """
    number = json.loads(manifest_path.read_bytes())["edition"]
    sample = json.loads((EDITIONS / f"edition-{number}-sample.json").read_bytes())
    rule = sample["extract_rule"]
    count = len(sample["rows"])

    assert rule.startswith((f"{count} rows ", f"All {count} of ")), rule
