"""ARC-28 event decoding for the calls this suite submits.

Boxes are working state and `close` deletes them, so the events are the
permanent record and the suite reads both. An event is logged as a 4-byte
selector -- the sha512/256 prefix of its signature -- followed by the ARC-4
encoding of its arguments.

The catalogue is read out of the committed ARC-56 artifact. Reading a compiled
artifact as a *file* is the sanctioned way across the `contracts/` dependency
boundary, and the same one `api/escrow.py` uses to pin the ABI; importing the
generated client would pull algokit-utils onto this surface.
"""

from __future__ import annotations

import base64
import json
from functools import cache
from pathlib import Path
from typing import Any

from algosdk import abi
from algosdk.encoding import checksum

ARC56_PATH = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "smart_contracts"
    / "artifacts"
    / "escrow"
    / "Escrow.arc56.json"
)


@cache
def _catalogue() -> dict[bytes, tuple[str, list[str], Any]]:
    spec = json.loads(ARC56_PATH.read_text(encoding="utf-8"))
    catalogue: dict[bytes, tuple[str, list[str], Any]] = {}
    for method in spec["methods"]:
        for event in method.get("events", []):
            types = ",".join(arg["type"] for arg in event["args"])
            selector = checksum(f"{event['name']}({types})".encode())[:4]
            catalogue[selector] = (
                event["name"],
                [arg["name"] for arg in event["args"]],
                abi.ABIType.from_string(f"({types})"),
            )
    return catalogue


def _normalise(value: Any) -> Any:
    """ARC-4 `byte[]` decodes to a list of ints; hashes are more useful as bytes.

    Done here rather than at each assertion so a scenario can compare a
    commit hash against `bytes.fromhex(...)` and mean it. This contract's
    events carry no other array type, so there is nothing else to catch.
    """
    if isinstance(value, list) and all(isinstance(item, int) for item in value):
        return bytes(value)
    return value


def decode_logs(logs: list[str | bytes]) -> list[tuple[str, dict]]:
    """Every recognised ARC-28 event in a confirmation's logs, in order."""
    catalogue = _catalogue()
    events: list[tuple[str, dict]] = []
    for raw in logs:
        log = base64.b64decode(raw) if isinstance(raw, str) else bytes(raw)
        entry = catalogue.get(log[:4])
        if entry is None:
            # ARC-4 return values share the log channel and are not events.
            continue
        name, arg_names, codec = entry
        decoded = [_normalise(value) for value in codec.decode(log[4:])]
        events.append((name, dict(zip(arg_names, decoded, strict=True))))
    return events


def events_for(algod: Any, txid: str) -> list[tuple[str, dict]]:
    """The events one confirmed transaction emitted."""
    info = algod.pending_transaction_info(txid)
    return decode_logs(info.get("logs") or [])


def txid_str(raw: bytes) -> str:
    """A 32-byte transaction id, as the base32 string algod and explorers use.

    The contract emits `payment_txn_id` as raw bytes; algosdk's `get_txid()`
    hands back the encoded form. Comparing them means encoding one of the two,
    and the string is the one a failure message can be read against an
    explorer.
    """
    return base64.b32encode(raw).decode().rstrip("=")


def one_event(events: list[tuple[str, dict]], name: str) -> dict:
    """The single event of that name, asserting there is exactly one.

    Exactly one rather than at least one: a path that emitted an event twice
    would be double-counting something, and `RefundComplete`'s own counts are
    read off the record precisely so that cannot happen quietly.
    """
    matches = [payload for event, payload in events if event == name]
    assert len(matches) == 1, (
        f"expected exactly one {name} event, got {len(matches)} "
        f"in {[e for e, _ in events]}"
    )
    return matches[0]
