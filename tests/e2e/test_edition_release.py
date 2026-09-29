"""S8 -- an edition pool, released with its CID in the release note.

The multi-seat rehearsal releases a pool against a throwaway digest, straight
through the client. This releases one the way an edition is released: a pool
committed to a pinned bundle's sha256, filled through the real facilitator,
refused by the generic CLI, then released by `agents.release_edition` -- which
fetches the pinned bundle through public gateways before it signs anything.

The pool is committed to a rehearsal bundle's sha256, never a real edition's.
A release note publishes its CID on a public chain, and until an edition's
MainNet pool is released, that CID is what its seat holders pay for.

What it proves, and nothing short of a real release can: the note reaches the
chain on the release transaction itself, in the published format, and the
pool is released only after the bytes behind the CID were hashed.

Gated three times over, like the rehearsal -- the marker, `E2E_ENABLED`, and
`E2E_REHEARSAL` through `seat_buyers` -- and once more on
`E2E_EDITION_MANIFEST` and `E2E_EDITION_CID`: the rehearsal bundle's manifest,
and the CID the pinning provider reported for that bundle. The script signs
with `VERIFIER_MNEMONIC` from `contracts/.env.testnet`, which has to be this
suite's verifier. The pool it releases stays on TestNet, and its note is a
release note for the rehearsal bundle.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from algosdk import account, mnemonic
from dotenv import dotenv_values

from agents.release_edition import read_manifest, release_note
from api.escrow import STATE_FUNDED, STATE_RELEASED
from api.pool import find_any_open_pool
from tests.e2e.events import events_for, one_event
from tests.e2e.harness import REPO_ROOT, algod_url_from
from tests.e2e.pool import POOL_SHARE_PRICE, fill, pool_server

pytestmark = pytest.mark.testnet

EDITIONS = Path(REPO_ROOT) / "editions"
TESTNET_KEY_FILE = Path(REPO_ROOT) / "contracts" / ".env.testnet"
SEATS = 2

# Filling two seats takes under a minute. The release then fetches the whole
# bundle through public gateways twice -- once for the dry run, once for real
# -- and a gateway that has never been asked for the CID can take minutes to
# find it. The script's own runway check is lowered to fit this deadline.
DEADLINE_SECONDS = 1_800
RUNWAY_SECONDS = 300


def _run(e2e_env, argv: list[str]) -> subprocess.CompletedProcess:
    """An operator script as the operator runs it, against this suite's node.

    A subprocess, as `test_multi_seat_rehearsal._run_create_pool` runs its
    script: `agents.pool_ops` loads .env through `agents.common`, and the
    release script loads it in `main` -- either would copy .env into this
    process. Both algod variable names are set, because `load_settings`
    reads ALGOD_URL and `agents.common` reads ALGOD_TESTNET_URL.
    """
    url = algod_url_from(e2e_env)
    return subprocess.run(
        [sys.executable, "-m", *argv],
        check=False,  # the assertions report stdout and stderr
        cwd=REPO_ROOT,
        env={**os.environ, "ALGOD_URL": url, "ALGOD_TESTNET_URL": url},
        capture_output=True,
        text=True,
    )


def _edition_digest(path: Path) -> str | None:
    """A manifest's `sha256`, the digest of the edition it commits to -- or
    None for a file that is not a manifest at all, which `read_manifest`
    then refuses with its own message."""
    try:
        digest = json.loads(path.read_text(encoding="utf-8")).get("sha256")
    except (ValueError, AttributeError):
        return None
    return digest if isinstance(digest, str) else None


def _release_edition(
    e2e_env, agreement_id: int, cid: str, path: Path, *, dry_run: bool
) -> str:
    argv = [
        "agents.release_edition",
        "--agreement-id",
        str(agreement_id),
        "--manifest",
        str(path),
        "--cid",
        cid,
        "--min-runway-seconds",
        str(RUNWAY_SECONDS),
    ]
    if dry_run:
        argv.append("--dry-run")
    result = _run(e2e_env, argv)
    assert result.returncode == 0, (
        f"release_edition exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result.stdout


def test_an_edition_pool_is_released_with_its_cid_in_the_note(
    e2e_env,
    settings,
    algod,
    as_admin,
    as_buyer,
    admin,
    buyer,
    treasury,
    verifier,
    job_db_path,
    seat_buyers,
):
    setting = e2e_env.get("E2E_EDITION_MANIFEST")
    cid = e2e_env.get("E2E_EDITION_CID")
    if not setting or not cid:
        pytest.skip(
            "releasing an edition pool needs a rehearsal bundle: a small "
            "edition-<N>.json that is not any real edition, with its manifest, "
            "pinned with the pinning provider. Set E2E_EDITION_MANIFEST and "
            "E2E_EDITION_CID in .env to that manifest and the CID the pinning "
            "provider reported. Never point them at a real edition."
        )
    # Relative to the repository root, where the script is run from.
    path = (Path(REPO_ROOT) / setting).resolve()
    assert path.is_file(), (
        f"E2E_EDITION_MANIFEST is {setting!r}, and {path} is not a file"
    )

    # -- never a real edition ------------------------------------------------
    real_edition = (
        f"{path} is, or commits to the same edition as, a real edition's "
        "manifest. A release note publishes its CID on a public chain, and a "
        "real edition's CID is what seat holders pay for until its MainNet pool "
        "is released, so the rehearsal runs on a rehearsal bundle only."
    )
    assert not path.is_relative_to(EDITIONS.resolve()), real_edition
    committed = {
        _edition_digest(published)
        for published in EDITIONS.glob("edition-*-manifest.json")
    }
    committed.discard(None)
    assert _edition_digest(path) not in committed, real_edition

    # The script's own checks, run here before anything is spent: a manifest
    # or a CID it would refuse must not fill a pool first.
    manifest = read_manifest(path)
    try:
        release_note(manifest["edition"], 0, cid)
    except ValueError as error:
        pytest.fail(f"E2E_EDITION_CID cannot go in a release note: {error}")
    commit_hash = bytes.fromhex(manifest["sha256"])
    edition = manifest["edition"]

    # Checked before a pool exists: a key that is not this suite's verifier
    # would fill a pool the script then refuses to release, and leave it open
    # until its deadline.
    phrase = dotenv_values(TESTNET_KEY_FILE).get("VERIFIER_MNEMONIC")
    assert phrase, (
        f"VERIFIER_MNEMONIC is not set in {TESTNET_KEY_FILE}. Re-run "
        "`python -m agents.e2e_bootstrap`, which writes it there."
    )
    signs_for = account.address_from_private_key(mnemonic.to_private_key(phrase))
    assert signs_for == verifier.address, (
        f"VERIFIER_MNEMONIC in {TESTNET_KEY_FILE} signs for {signs_for}, and "
        f"this suite's verifier is {verifier.address}"
    )

    assert find_any_open_pool(as_admin) is None, (
        "a multi-seat pool is already open on this application. Drain it "
        "first: `python -m agents.pool_ops drain --agreement-id <id> --wait`"
    )

    # -- a pool committed to the rehearsal bundle ---------------------------
    agreement_id = as_admin.create_hash_agreement(
        commit_hash=commit_hash,
        share_price=POOL_SHARE_PRICE,
        deadline=int(time.time()) + DEADLINE_SECONDS,
        beneficiary=treasury.address,
        verifier=verifier.address,
        seats=SEATS,
        note=f"earnest:index:edition-{edition}:sha256:{manifest['sha256']}".encode(),
    )

    payers = [buyer, *seat_buyers(SEATS - 1, POOL_SHARE_PRICE)]
    with (
        pool_server(
            e2e_env, algod, admin, job_db_path, agreement_id, seats=SEATS
        ) as base_url,
        httpx.Client(timeout=120.0) as http,
    ):
        fill(
            http,
            f"{base_url}/index",
            payers=payers,
            agreement_id=agreement_id,
            commit_hash=commit_hash,
            settings=settings,
            algod=algod,
            reader=as_admin,
            seats_total=SEATS,
        )
    assert as_admin.read_agreement(agreement_id).state == STATE_FUNDED

    # -- the generic CLI refuses it, and sends nothing ----------------------
    refused = _run(
        e2e_env,
        [
            "agents.pool_ops",
            "release",
            "--agreement-id",
            str(agreement_id),
            "--sha256",
            manifest["sha256"],
        ],
    )
    assert refused.returncode != 0
    assert "agents.release_edition" in refused.stderr, refused.stderr
    assert as_admin.read_agreement(agreement_id).state == STATE_FUNDED

    # -- the dry run fetches the bundle and sends nothing --------------------
    dry = _release_edition(e2e_env, agreement_id, cid, path, dry_run=True)
    assert "nothing was sent" in dry
    assert as_admin.read_agreement(agreement_id).state == STATE_FUNDED

    # -- the release ---------------------------------------------------------
    stdout = _release_edition(e2e_env, agreement_id, cid, path, dry_run=False)
    released_line = next(
        line for line in stdout.splitlines() if line.startswith("released agreement")
    )
    txid = released_line.rsplit(" ", 1)[1]

    expected = f"earnest:index:edition-{edition}:agreement:{agreement_id}:cid:{cid}"
    confirmed = algod.pending_transaction_info(txid)
    assert base64.b64decode(confirmed["txn"]["txn"]["note"]) == expected.encode(), (
        "the edition's link must be on the release transaction itself"
    )
    released = one_event(events_for(algod, txid), "Released")
    assert bytes(released["proof"]) == commit_hash
    assert as_admin.read_agreement(agreement_id).state == STATE_RELEASED

    # -- close, sent by a buyer: the boxes go, the note does not -------------
    as_buyer.close(agreement_id)
    assert as_admin.read_agreement(agreement_id) is None
    print(
        f"\n  agreement {agreement_id} released in {txid} with note {expected}"
        "\n  confirm the indexer search of docs/specs/edition-release.md §5 finds it"
    )
