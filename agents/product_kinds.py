"""The kinds of deliverable an edition pool can commit to, and how each is named.

The contract holds a `commit_hash` and nothing else about what a pool sold, so
the kind of file behind that hash is recorded only off the contract: in the
manifest's `file`, and in the notes the operator scripts write on the creating
and the releasing transaction. Both scripts take the kind from this table, so
the name a manifest gives its committed file and the prefix a reader searches
the chain for cannot drift apart.

    kind     committed file    note prefix
    index    edition-N.json    earnest:index:edition-
    travel   trip-N.json       earnest:travel:trip-

`N` is the manifest's `edition` field for both kinds: it is the manifest's
numbering key, whatever its committed file is called. The specification is
`docs/specs/edition-release.md`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ProductKind:
    """One kind of deliverable: the note's second field and the file's stem."""

    name: str
    stem: str

    @property
    def note_prefix(self) -> str:
        """What every note of this kind starts with, on creation and on
        release alike: the prefix a reader searches an application's history
        for. No kind's prefix is a prefix of another's, so a search for one
        kind never returns the other's notes."""
        return f"earnest:{self.name}:{self.stem}-"

    def committed_file(self, edition: int) -> str:
        """The file a pool of this kind commits to the sha256 of."""
        return f"{self.stem}-{edition}.json"


INDEX = ProductKind(name="index", stem="edition")
TRAVEL = ProductKind(name="travel", stem="trip")
KINDS = (INDEX, TRAVEL)
KINDS_BY_NAME = {kind.name: kind for kind in KINDS}

# `edition-N.json` or `trip-N.json`, N a positive whole number with no leading
# zero. Built from the table rather than typed out, so a kind cannot be added
# to one and forgotten in the other.
COMMITTED_FILE = re.compile(
    "(" + "|".join(re.escape(kind.stem) for kind in KINDS) + r")-([1-9][0-9]*)\.json"
)


def committed_kind(file: object, edition: int) -> ProductKind:
    """The kind of a manifest whose `file` and `edition` these are.

    `edition` must already be a positive integer; each script checks it in
    its own terms first. Raises `ValueError` unless `file` is one kind's
    committed file for exactly that edition. The message is a sentence each
    script prefixes with the manifest's path and closes with what it did not
    do, which keeps the index kind's refusal word for word what it was when
    the index was the only kind.
    """
    match = COMMITTED_FILE.fullmatch(file) if isinstance(file, str) else None
    if match is None:
        accepted = " or ".join(repr(kind.committed_file(edition)) for kind in KINDS)
        raise ValueError(
            f"`file` is {file!r}, not {accepted}. A reader looks for an "
            "edition's committed file under one of those names."
        )
    kind = next(kind for kind in KINDS if kind.stem == match.group(1))
    expected = kind.committed_file(edition)
    if file != expected:
        raise ValueError(
            f"`file` is {file!r}, not {expected!r}. A reader looks for an "
            "edition's committed file under that name."
        )
    return kind
