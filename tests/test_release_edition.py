"""The parts of the release script that decide whether a release may happen.

Nothing here reaches a node, a gateway or a key: the gateway check runs
against stand-ins. A release cannot be taken back, so what is pinned down is
every refusal that has to fire before one -- and the note, which is the only
on-chain record of what the pool paid for. The network path is exercised on
TestNet by `tests/e2e/test_edition_release.py`.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from algosdk import account, mnemonic
from x402.mechanisms.avm import ALGORAND_MAINNET_CAIP2, ALGORAND_TESTNET_CAIP2

from agents import release_edition
from agents.release_edition import (
    NOTE_PATTERN,
    check_releasable,
    read_manifest,
    release_note,
    verifier_mnemonic,
)
from api.escrow import STATE_FUNDED, STATE_OPEN, STATE_RELEASED, Agreement

DIGEST = "7ff9c6cc101dc4c53d6638f802fb3cd3e976339164194e45c0d804136eb0ddd9"
VERIFIER = "KOKMHQYSETOYM6SNMG6O3VBMJ5ZHL76F2L6TPZS5WPZVYSVLNIDNB62L6M"
OTHER = "D4SAMDS7SDVOU5OTL5GEZX4RXM43JEKBM6NFIIX6CPJRCHEKGK7WMGIKRY"
CID = "bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi"
NOW = 1_790_000_000


def _manifest(**overrides) -> dict:
    manifest = {
        "edition": 1,
        "file": "edition-1.json",
        "sha256": DIGEST,
        "files": {"edition-1.json": DIGEST, "edition-1.csv": "ab" * 32},
    }
    manifest.update(overrides)
    return manifest


def _write(tmp_path: Path, manifest: dict) -> Path:
    path = tmp_path / "edition-1-manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _record(**overrides) -> Agreement:
    record = Agreement(
        condition=0,
        state=STATE_FUNDED,
        deadline=NOW + 3_600,
        share_price=100_000,
        min_seats=5,
        max_seats=5,
        seats=5,
        refund_cursor=0,
        unclaimed_seats=0,
        total_held=500_000,
        commit_hash=bytes.fromhex(DIGEST),
        beneficiary=OTHER,
        verifier=VERIFIER,
        creator=OTHER,
    )
    return dataclasses.replace(record, **overrides)


def _check(record: Agreement, **overrides) -> None:
    kwargs = {"verifier": VERIFIER, "chain_now": NOW, "runway_seconds": 900}
    kwargs.update(overrides)
    check_releasable(record, _manifest(), **kwargs)


# --- the note -------------------------------------------------------------


def test_the_note_is_the_published_format_exactly():
    note = release_note(1, 31, CID)
    assert note == f"earnest:index:edition-1:agreement:31:cid:{CID}".encode()
    assert NOTE_PATTERN.fullmatch(note.decode("ascii"))


@pytest.mark.parametrize(
    "edition, agreement_id, cid",
    [
        (0, 31, CID),  # editions start at 1
        (1, -1, CID),
        (1, 31, ""),
        (1, 31, "bafy/edition-1.html"),  # a path, not a CID
        (1, 31, "bafy:beef"),  # would split the note's own fields
        (1, 31, CID + "\n"),
    ],
)
def test_a_note_a_reader_would_ignore_is_never_built(edition, agreement_id, cid):
    """A reader drops a note that does not match the published pattern, and the
    release cannot be sent again -- so such a note would record a link nobody
    can ever find."""
    with pytest.raises(ValueError, match="not a release note"):
        release_note(edition, agreement_id, cid)


def test_a_note_over_the_protocol_ceiling_is_never_built():
    with pytest.raises(ValueError, match="over the protocol"):
        release_note(1, 31, "b" * 1_024)


# --- the manifest ---------------------------------------------------------


def test_a_manifest_that_names_its_committed_file_is_read(tmp_path):
    assert read_manifest(_write(tmp_path, _manifest()))["edition"] == 1


def test_a_manifest_whose_committed_file_disagrees_is_refused(tmp_path):
    """The gateway check hashes what `files` lists. If the committed file's
    entry were some other digest, that check could pass on the wrong bytes."""
    manifest = _manifest(files={"edition-1.json": "cd" * 32})
    with pytest.raises(SystemExit, match="not the manifest's sha256"):
        read_manifest(_write(tmp_path, manifest))


@pytest.mark.parametrize(
    "overrides, match",
    [
        ({"edition": True}, "`edition` is True, not a positive integer"),
        ({"edition": "1"}, "`edition` is '1', not a positive integer"),
        ({"edition": 0}, "`edition` is 0, not a positive integer"),
        ({"sha256": DIGEST.upper()}, "`sha256` is .*, not a lowercase sha256"),
        ({"sha256": "ab"}, "`sha256` is 'ab', not a lowercase sha256"),
        (
            {"file": "edition-2.json"},
            "`file` is 'edition-2.json', not 'edition-1.json'",
        ),
        (
            {
                "files": {
                    "edition-1.json": DIGEST,
                    "../bafyother/edition-1.json": DIGEST,
                }
            },
            "'../bafyother/edition-1.json' is not a single path segment",
        ),
        (
            {"files": {"edition-1.json": DIGEST, "edition-1.html?x": DIGEST}},
            r"'edition-1.html\?x' is not a single path segment",
        ),
        (
            {"files": {"edition-1.json": DIGEST, "..": DIGEST}},
            r"'\.\.' is not a single path segment",
        ),
        (
            {"files": {"edition-1.json": DIGEST, "edition-1.csv": None}},
            "the entry for 'edition-1.csv' is None, not a lowercase sha256",
        ),
        (
            {"files": {"edition-1.json": DIGEST, "edition-1.csv": "AB" * 32}},
            "the entry for 'edition-1.csv' is 'A.*, not a lowercase sha256",
        ),
        ({"files": [DIGEST]}, "`files` is .*, not an object"),
    ],
    ids=[
        "edition-bool",
        "edition-string",
        "edition-zero",
        "sha256-uppercase",
        "sha256-short",
        "file-another-edition",
        "name-climbs-to-another-cid",
        "name-with-a-query",
        "name-dot-dot",
        "digest-null",
        "digest-uppercase",
        "files-not-an-object",
    ],
)
def test_a_malformed_manifest_is_refused(tmp_path, overrides, match):
    """Every name is fetched as `<gateway>/ipfs/<cid>/<name>`. A name that
    climbs out of the CID, or carries a query or a fragment, would fetch some
    other resource, and the note would then record a CID those bytes were never
    fetched under."""
    path = _write(tmp_path, _manifest(**overrides))
    with pytest.raises(SystemExit, match=match) as refused:
        read_manifest(path)
    message = str(refused.value)
    assert message.startswith(f"{path}: ")
    assert message.endswith("Nothing was sent.")


@pytest.mark.parametrize(
    "text, match",
    [
        ('{"edition": 1,', "is not valid JSON"),
        (json.dumps([_manifest()]), "is a list, not a JSON object"),
    ],
    ids=["not-json", "not-an-object"],
)
def test_a_manifest_that_is_not_a_json_object_is_refused(tmp_path, text, match):
    path = tmp_path / "edition-1-manifest.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(SystemExit, match=match) as refused:
        read_manifest(path)
    message = str(refused.value)
    assert message.startswith(f"{path}: ")
    assert message.endswith("Nothing was sent.")


# --- whether the agreement can be released --------------------------------


def test_a_funded_agreement_with_its_verifier_and_time_left_passes():
    _check(_record())


@pytest.mark.parametrize("state", [STATE_OPEN, STATE_RELEASED])
def test_an_agreement_that_is_not_funded_is_refused(state):
    with pytest.raises(SystemExit, match="only FUNDED"):
        _check(_record(state=state))


def test_a_key_that_is_not_the_agreements_verifier_is_refused():
    with pytest.raises(SystemExit, match="accepts a release from nobody else"):
        _check(_record(), verifier=OTHER)


def test_an_agreement_committed_to_another_edition_is_refused():
    with pytest.raises(SystemExit, match="committed to"):
        _check(_record(commit_hash=bytes(32)))


def test_too_little_time_before_the_deadline_is_refused():
    with pytest.raises(SystemExit, match="needs at least 900s"):
        _check(_record(deadline=NOW + 899))


def test_every_reason_is_named_at_once():
    """An operator who fixes one refusal should not discover the next one on
    the following run."""
    with pytest.raises(SystemExit) as refused:
        _check(_record(state=STATE_OPEN, deadline=NOW), verifier=OTHER)
    message = str(refused.value)
    assert "only FUNDED" in message
    assert "nobody else" in message
    assert "needs at least" in message


# --- the verifier's key ---------------------------------------------------


def _key_files(tmp_path: Path, *, mainnet: str, testnet: str) -> dict[str, Path]:
    files = {
        ALGORAND_MAINNET_CAIP2: tmp_path / ".env.mainnet",
        ALGORAND_TESTNET_CAIP2: tmp_path / ".env.testnet",
    }
    files[ALGORAND_MAINNET_CAIP2].write_text(mainnet, encoding="utf-8")
    files[ALGORAND_TESTNET_CAIP2].write_text(testnet, encoding="utf-8")
    return files


def test_the_key_comes_from_the_file_for_the_configured_network(tmp_path):
    """A TestNet configuration must not be able to sign with the MainNet key."""
    files = _key_files(
        tmp_path,
        mainnet="VERIFIER_MNEMONIC=mainnet words\n",
        testnet="VERIFIER_MNEMONIC=testnet words\n",
    )
    assert verifier_mnemonic(ALGORAND_TESTNET_CAIP2, files) == "testnet words"
    assert verifier_mnemonic(ALGORAND_MAINNET_CAIP2, files) == "mainnet words"


def test_a_network_file_without_the_key_is_refused(tmp_path):
    files = _key_files(tmp_path, mainnet="DEPLOYER_MNEMONIC=x\n", testnet="")
    with pytest.raises(SystemExit, match="VERIFIER_MNEMONIC is not set"):
        verifier_mnemonic(ALGORAND_MAINNET_CAIP2, files)


def test_a_missing_network_file_is_refused(tmp_path):
    files = {ALGORAND_MAINNET_CAIP2: tmp_path / "absent"}
    with pytest.raises(SystemExit, match="VERIFIER_MNEMONIC is not set"):
        verifier_mnemonic(ALGORAND_MAINNET_CAIP2, files)


def test_a_network_with_no_key_file_is_refused():
    with pytest.raises(SystemExit, match="no verifier key file.*Nothing was sent"):
        verifier_mnemonic("algorand:unknown", {})


# --- the gateway check ----------------------------------------------------

BUNDLE = {"edition-1.json": b'{"edition": 1}', "edition-1.csv": b"route,price\n"}


def _bundle_manifest() -> dict:
    return {
        "files": {
            name: hashlib.sha256(body).hexdigest() for name, body in BUNDLE.items()
        }
    }


class _Served:
    def __init__(self, status_code: int, content: bytes = b"") -> None:
        self.status_code = status_code
        self.content = content


def _gateways(monkeypatch, serve) -> tuple[list[str], list[float]]:
    """Stand in for every gateway fetch and every wait. Returns the URLs asked
    for and the waits taken, in order."""
    asked: list[str] = []
    waits: list[float] = []

    def get(url, **kwargs):
        asked.append(url)
        return serve(url)

    monkeypatch.setattr(release_edition.httpx, "get", get)
    monkeypatch.setattr(release_edition.time, "sleep", waits.append)
    return asked, waits


TWO_GATEWAYS = ("https://first.example", "https://second.example")


def test_a_bundle_one_gateway_serves_intact_is_verified(monkeypatch):
    """Gateways disagree about propagation; one serving the right bytes is
    enough, and every file is asked for under the CID."""

    def serve(url):
        if url.startswith("https://first.example/"):
            return _Served(504)
        return _Served(200, BUNDLE[url.rsplit("/", 1)[1]])

    asked, _waits = _gateways(monkeypatch, serve)
    assert release_edition.verify_through_gateways(
        CID, _bundle_manifest(), gateways=TWO_GATEWAYS, timeout=1
    )
    for name in BUNDLE:
        assert f"https://second.example/ipfs/{CID}/{name}" in asked


def test_wrong_bytes_under_the_cid_fail_the_check(monkeypatch):
    """Served is not verified: bytes that do not hash to the manifest's digest
    are what a wrong CID or a wrong bundle looks like."""
    _gateways(monkeypatch, lambda url: _Served(200, b"some other bundle"))
    assert not release_edition.verify_through_gateways(
        CID, _bundle_manifest(), gateways=TWO_GATEWAYS, timeout=1
    )


def test_a_bundle_nobody_serves_fails_once_the_retries_run_out(monkeypatch):
    """A cold gateway gets a bounded number of retries with backoff, then the
    check fails -- it never waits indefinitely on a release's deadline."""

    def serve(url):
        raise httpx.ConnectError("unreachable")

    _asked, waits = _gateways(monkeypatch, serve)
    assert not release_edition.verify_through_gateways(
        CID, _bundle_manifest(), gateways=TWO_GATEWAYS, timeout=1
    )
    assert len(waits) == release_edition.VERIFY_ATTEMPTS - 1


# --- which gateways -------------------------------------------------------


@pytest.mark.parametrize(
    "env", [{}, {"RECHECK_GATEWAYS": ""}, {"RECHECK_GATEWAYS": "  "}]
)
def test_with_no_gateway_configured_the_check_uses_filebase(env):
    assert release_edition.recheck_gateways(env) == ("https://ipfs.filebase.io",)


def test_the_configured_gateways_are_read_in_order_as_https_urls():
    """Comma-separated, with room for the spacing and trailing slashes people
    type. A bare host means https, and an empty entry is not a gateway."""
    env = {"RECHECK_GATEWAYS": " ipfs.filebase.io, https://dweb.link/ ,,ipfs.io"}
    assert release_edition.recheck_gateways(env) == (
        "https://ipfs.filebase.io",
        "https://dweb.link",
        "https://ipfs.io",
    )


@pytest.mark.parametrize(
    "entry",
    [
        "http://ipfs.io",
        "ipfs.io/ipfs",
        "https://ipfs.io/ipfs",
        "https://ipfs.io?",
        "https://ipfs.io#",
        "https://user:secret@ipfs.io",
        "https://ipfs.io:port",
        "https://",
    ],
)
def test_a_gateway_that_is_not_an_https_origin_is_refused(entry):
    """Each file is fetched as `<gateway>/ipfs/<cid>/<name>`. Anything but an
    https origin there fetches some other URL, or the same one in the clear,
    so it is refused rather than fetched."""
    env = {"RECHECK_GATEWAYS": f"ipfs.filebase.io, {entry}"}
    with pytest.raises(SystemExit, match="RECHECK_GATEWAYS.*Nothing was sent"):
        release_edition.recheck_gateways(env)


def test_a_gateway_on_its_own_port_is_an_origin():
    env = {"RECHECK_GATEWAYS": "https://gateway.example:8443/"}
    assert release_edition.recheck_gateways(env) == ("https://gateway.example:8443",)


def test_a_gateway_list_that_names_no_gateway_is_refused():
    """Set but empty of gateways is a typo, not a request for the default."""
    with pytest.raises(SystemExit, match="RECHECK_GATEWAYS.*Nothing was sent"):
        release_edition.recheck_gateways({"RECHECK_GATEWAYS": " , ,"})


# --- the order of main's refusals -----------------------------------------


def _unreachable(*args, **kwargs):
    raise AssertionError("nothing past the local checks may run")


def test_a_bad_cid_is_refused_before_any_key_or_setting_is_read(tmp_path, monkeypatch):
    monkeypatch.setattr(release_edition, "load_settings", _unreachable)
    monkeypatch.setattr(release_edition, "verifier_mnemonic", _unreachable)
    manifest = _write(tmp_path, _manifest())
    with pytest.raises(
        SystemExit, match="cannot build the release note.*Nothing was sent"
    ):
        release_edition.main(
            ["--agreement-id", "31", "--manifest", str(manifest), "--cid", "a/b"]
        )


def test_a_missing_verifier_key_is_refused_before_the_network(
    tmp_path, monkeypatch, settings_factory
):
    monkeypatch.setattr(release_edition, "load_dotenv", lambda: None)
    monkeypatch.delenv("RECHECK_GATEWAYS", raising=False)
    monkeypatch.setattr(release_edition, "load_settings", lambda: settings_factory())
    monkeypatch.setattr(
        release_edition,
        "NETWORK_ENV_FILES",
        _key_files(tmp_path, mainnet="", testnet=""),
    )
    monkeypatch.setattr(release_edition.algod_client, "AlgodClient", _unreachable)
    manifest = _write(tmp_path, _manifest())
    with pytest.raises(SystemExit, match="VERIFIER_MNEMONIC is not set"):
        release_edition.main(
            ["--agreement-id", "31", "--manifest", str(manifest), "--cid", CID]
        )


def test_the_environment_is_loaded_before_the_settings_are_read(
    tmp_path, monkeypatch, settings_factory
):
    """An operator's ALGOD_URL and APP_ID live in .env. Loaded after the
    settings were read, they would be ignored, and the release would run
    against whatever the defaults point at."""
    calls = []
    monkeypatch.setattr(release_edition, "load_dotenv", lambda: calls.append("env"))

    def settings():
        calls.append("settings")
        return settings_factory()

    monkeypatch.setattr(release_edition, "load_settings", settings)
    monkeypatch.setattr(
        release_edition,
        "NETWORK_ENV_FILES",
        _key_files(tmp_path, mainnet="", testnet=""),
    )
    manifest = _write(tmp_path, _manifest())
    with pytest.raises(SystemExit, match="VERIFIER_MNEMONIC is not set"):
        release_edition.main(
            ["--agreement-id", "31", "--manifest", str(manifest), "--cid", CID]
        )
    assert calls == ["env", "settings"]


# --- main, past the checks that need no network ---------------------------


class _FakeEscrow:
    """Stands in for both the `EscrowClient` class and the instance it
    builds: main() constructs one from the settings and the verifier's key,
    and every construction here returns this same fake, whose
    `read_agreement` answers with a fixed record and whose `release_hash`
    records what it would have sent rather than sending it."""

    def __init__(self, record: Agreement, *, txid: str = "TXID") -> None:
        self.record = record
        self.txid = txid
        self.release_calls: list[tuple[int, bytes, bytes | None]] = []

    def __call__(self, *args, **kwargs) -> _FakeEscrow:
        return self

    def read_agreement(self, agreement_id: int) -> Agreement:
        return self.record

    def release_hash(self, agreement_id, delivered_bytes_hash, *, note=None) -> str:
        self.release_calls.append((agreement_id, delivered_bytes_hash, note))
        return self.txid


class _FakeAlgod:
    """Stands in for the algod client. `chain_timestamp` is scripted
    separately, so the only method main() reaches on this is the read-back
    after `release_hash`."""

    def __init__(self, note: bytes) -> None:
        self._note = note

    def pending_transaction_info(self, txid: str) -> dict:
        return {"txn": {"txn": {"note": base64.b64encode(self._note).decode()}}}


def _verifier_account() -> tuple[str, str]:
    """A fresh, real key pair. main() derives an address from the mnemonic it
    reads, so the tests need one that round-trips through algosdk rather than
    a stand-in string."""
    private_key, address = account.generate_account()
    return mnemonic.from_private_key(private_key), address


def _wire_main(
    monkeypatch,
    tmp_path: Path,
    settings_factory,
    *,
    phrase: str,
    record: Agreement,
    confirmed_note: bytes,
    chain_now,
    verify_ok: bool,
) -> _FakeEscrow:
    """Stand in for everything main() reaches past the manifest and the note:
    no `.env`, no real key file outside tmp_path, no chain, no gateway.

    `chain_now` is either one timestamp every `chain_timestamp()` call
    answers, or a list consumed one call at a time -- the only way to make
    the chain's clock move between main()'s two releasability checks.
    """
    monkeypatch.setattr(release_edition, "load_dotenv", lambda: None)
    monkeypatch.delenv("RECHECK_GATEWAYS", raising=False)
    monkeypatch.setattr(release_edition, "load_settings", lambda: settings_factory())
    monkeypatch.setattr(
        release_edition,
        "NETWORK_ENV_FILES",
        _key_files(
            tmp_path,
            mainnet=f"VERIFIER_MNEMONIC={phrase}\n",
            testnet=f"VERIFIER_MNEMONIC={phrase}\n",
        ),
    )
    monkeypatch.setattr(
        release_edition.algod_client,
        "AlgodClient",
        lambda token, url: _FakeAlgod(confirmed_note),
    )
    escrow = _FakeEscrow(record)
    monkeypatch.setattr(release_edition, "EscrowClient", escrow)
    values = iter(chain_now) if isinstance(chain_now, list) else None
    monkeypatch.setattr(
        release_edition,
        "chain_timestamp",
        lambda algod: next(values) if values is not None else chain_now,
    )
    monkeypatch.setattr(
        release_edition,
        "verify_through_gateways",
        lambda cid, manifest, *, gateways, timeout: verify_ok,
    )
    return escrow


def test_a_release_that_passes_every_check_is_sent_once_with_its_note(
    tmp_path, monkeypatch, settings_factory, capsys
):
    """Proves the fakes below actually reach `release_hash` -- otherwise the
    four refusal tests that follow would pass vacuously."""
    phrase, address = _verifier_account()
    record = _record(verifier=address)
    note = release_note(1, 31, CID)
    escrow = _wire_main(
        monkeypatch,
        tmp_path,
        settings_factory,
        phrase=phrase,
        record=record,
        confirmed_note=note,
        chain_now=NOW,
        verify_ok=True,
    )
    manifest = _write(tmp_path, _manifest())
    assert (
        release_edition.main(
            ["--agreement-id", "31", "--manifest", str(manifest), "--cid", CID]
        )
        == 0
    )
    assert escrow.release_calls == [(31, record.commit_hash, note)]
    assert "released agreement 31 in TXID" in capsys.readouterr().out


def test_a_bundle_the_gateways_do_not_serve_is_never_released(
    tmp_path, monkeypatch, settings_factory
):
    phrase, address = _verifier_account()
    record = _record(verifier=address)
    escrow = _wire_main(
        monkeypatch,
        tmp_path,
        settings_factory,
        phrase=phrase,
        record=record,
        confirmed_note=release_note(1, 31, CID),
        chain_now=NOW,
        verify_ok=False,
    )
    manifest = _write(tmp_path, _manifest())
    with pytest.raises(SystemExit, match="not served"):
        release_edition.main(
            ["--agreement-id", "31", "--manifest", str(manifest), "--cid", CID]
        )
    assert escrow.release_calls == []


@pytest.mark.parametrize("network", [ALGORAND_TESTNET_CAIP2, ALGORAND_MAINNET_CAIP2])
def test_a_release_can_skip_the_gateway_check(
    tmp_path, monkeypatch, settings_factory, capsys, network
):
    """For an operator whose machine cannot reach the gateways. Every other
    check still runs, and the release carries the same note."""
    phrase, address = _verifier_account()
    record = _record(verifier=address)
    note = release_note(1, 31, CID)
    escrow = _wire_main(
        monkeypatch,
        tmp_path,
        settings_factory,
        phrase=phrase,
        record=record,
        confirmed_note=note,
        chain_now=NOW,
        verify_ok=True,
    )
    monkeypatch.setattr(
        release_edition, "load_settings", lambda: settings_factory(network=network)
    )
    monkeypatch.setattr(release_edition, "verify_through_gateways", _unreachable)
    manifest = _write(tmp_path, _manifest())
    argv = ["--agreement-id", "31", "--manifest", str(manifest), "--cid", CID]
    assert release_edition.main([*argv, "--skip-gateway-check"]) == 0
    assert escrow.release_calls == [(31, record.commit_hash, note)]
    assert "gateway check skipped" in capsys.readouterr().out


def test_the_check_goes_through_the_gateways_the_environment_names(
    tmp_path, monkeypatch, settings_factory
):
    phrase, address = _verifier_account()
    _wire_main(
        monkeypatch,
        tmp_path,
        settings_factory,
        phrase=phrase,
        record=_record(verifier=address),
        confirmed_note=release_note(1, 31, CID),
        chain_now=NOW,
        verify_ok=True,
    )
    checked = []

    def verify(cid, manifest, *, gateways, timeout):
        checked.append(gateways)
        return True

    monkeypatch.setattr(release_edition, "verify_through_gateways", verify)
    monkeypatch.setenv("RECHECK_GATEWAYS", "gateway.example, https://other.example")
    manifest = _write(tmp_path, _manifest())
    argv = ["--agreement-id", "31", "--manifest", str(manifest), "--cid", CID]
    assert release_edition.main([*argv, "--dry-run"]) == 0
    assert checked == [("https://gateway.example", "https://other.example")]


def test_a_dry_run_never_releases(tmp_path, monkeypatch, settings_factory, capsys):
    phrase, address = _verifier_account()
    record = _record(verifier=address)
    escrow = _wire_main(
        monkeypatch,
        tmp_path,
        settings_factory,
        phrase=phrase,
        record=record,
        confirmed_note=release_note(1, 31, CID),
        chain_now=NOW,
        verify_ok=True,
    )
    manifest = _write(tmp_path, _manifest())
    assert (
        release_edition.main(
            [
                "--agreement-id",
                "31",
                "--manifest",
                str(manifest),
                "--cid",
                CID,
                "--dry-run",
            ]
        )
        == 0
    )
    assert escrow.release_calls == []
    out = capsys.readouterr().out
    assert "nothing was sent" in out
    settings = settings_factory()
    assert f"network    {settings.network}, application {settings.app_id}" in out


def test_a_pool_that_stops_being_releasable_during_the_fetch_is_not_released(
    tmp_path, monkeypatch, settings_factory
):
    """The gateway fetch can take minutes, so main() checks releasability a
    second time right before the call that cannot be taken back -- with a
    smaller margin, since the first check already spent most of the runway
    it asked for."""
    phrase, address = _verifier_account()
    record = _record(verifier=address)
    escrow = _wire_main(
        monkeypatch,
        tmp_path,
        settings_factory,
        phrase=phrase,
        record=record,
        confirmed_note=release_note(1, 31, CID),
        chain_now=[NOW, record.deadline - (release_edition.FINAL_MARGIN_SECONDS - 1)],
        verify_ok=True,
    )
    manifest = _write(tmp_path, _manifest())
    with pytest.raises(SystemExit, match="needs at least"):
        release_edition.main(
            ["--agreement-id", "31", "--manifest", str(manifest), "--cid", CID]
        )
    assert escrow.release_calls == []


def test_a_confirmed_release_without_its_note_is_reported(
    tmp_path, monkeypatch, settings_factory
):
    phrase, address = _verifier_account()
    record = _record(verifier=address)
    escrow = _wire_main(
        monkeypatch,
        tmp_path,
        settings_factory,
        phrase=phrase,
        record=record,
        confirmed_note=b"some other note",
        chain_now=NOW,
        verify_ok=True,
    )
    manifest = _write(tmp_path, _manifest())
    with pytest.raises(SystemExit, match="cannot be sent again"):
        release_edition.main(
            ["--agreement-id", "31", "--manifest", str(manifest), "--cid", CID]
        )
    assert len(escrow.release_calls) == 1


# --- the second kind: a trip file -----------------------------------------


# Stands for a key left out of the manifest altogether, rather than set to null.
_ABSENT = object()


def _trip_manifest(**overrides) -> dict:
    """A manifest for a trip file, as the tooling that freezes one writes it.
    `edition` stays the numbering key; only the committed file's name says
    which kind of deliverable this is."""
    manifest = {
        "edition": 1,
        "file": "trip-1.json",
        "sha256": DIGEST,
        "files": {"trip-1.json": DIGEST, "trip-1.html": "ab" * 32},
        "rows": 1,
        "probed_at": "2000-01-01T00:00:00Z",
    }
    manifest.update(overrides)
    return manifest


@pytest.mark.parametrize(
    "kind, text",
    [
        ("index", f"earnest:index:edition-1:agreement:31:cid:{CID}"),
        ("travel", f"earnest:travel:trip-1:agreement:31:cid:{CID}"),
    ],
)
def test_each_kind_has_its_note_and_reads_back_into_the_same_groups(kind, text):
    """One pattern reads both kinds, so a reader's groups mean the same thing
    whichever kind it found: the edition, the agreement id, the CID."""
    note = release_note(1, 31, CID, kind=kind)
    assert note == text.encode()
    assert NOTE_PATTERN.fullmatch(text).groups() == ("1", "31", CID)


def test_the_default_kind_is_the_index():
    assert release_note(1, 31, CID) == release_note(1, 31, CID, kind="index")


@pytest.mark.parametrize(
    "text",
    [
        f"earnest:index:trip-1:agreement:31:cid:{CID}",
        f"earnest:travel:edition-1:agreement:31:cid:{CID}",
        f"earnest:travel:trip-0:agreement:31:cid:{CID}",
        f"earnest:catalogue:edition-1:agreement:31:cid:{CID}",
    ],
    ids=["index-name-trip-stem", "travel-name-edition-stem", "trip-zero", "no-kind"],
)
def test_a_note_that_mixes_or_invents_a_kind_is_not_read(text):
    """A reader searches by one kind's prefix. A note carrying one kind's name
    and another's stem belongs to neither, and must not be read as either."""
    assert NOTE_PATTERN.fullmatch(text) is None


def test_a_kind_that_does_not_exist_is_never_written():
    with pytest.raises(ValueError, match="not a product kind"):
        release_note(1, 31, CID, kind="catalogue")


def test_a_trip_manifest_is_read(tmp_path):
    manifest = read_manifest(_write(tmp_path, _trip_manifest()))
    assert manifest["file"] == "trip-1.json"


@pytest.mark.parametrize(
    "overrides, match",
    [
        (
            {"edition": 2, "files": {"trip-2.json": DIGEST}},
            "`file` is 'trip-1.json', not 'trip-2.json'",
        ),
        (
            {"file": "catalogue-1.json", "files": {"catalogue-1.json": DIGEST}},
            "`file` is 'catalogue-1.json', not 'edition-1.json' or 'trip-1.json'",
        ),
        ({"file": None}, "`file` is None, not 'edition-1.json' or 'trip-1.json'"),
        ({"file": _ABSENT}, "`file` is None, not 'edition-1.json' or 'trip-1.json'"),
    ],
    ids=["trip-number-disagrees", "unknown-stem", "file-null", "file-absent"],
)
def test_a_manifest_whose_file_names_no_kind_for_its_edition_is_refused(
    tmp_path, overrides, match
):
    manifest = _trip_manifest(**overrides)
    manifest = {key: value for key, value in manifest.items() if value is not _ABSENT}
    path = _write(tmp_path, manifest)
    with pytest.raises(SystemExit, match=match) as refused:
        read_manifest(path)
    message = str(refused.value)
    assert message.startswith(f"{path}: ")
    assert message.endswith("Nothing was sent.")


def test_a_trip_pool_is_released_with_the_travel_note(
    tmp_path, monkeypatch, settings_factory, capsys
):
    """main() takes the kind from the manifest it read, not from a flag: the
    note's prefix and the committed file's name cannot disagree."""
    phrase, address = _verifier_account()
    record = _record(verifier=address)
    note = release_note(1, 31, CID, kind="travel")
    escrow = _wire_main(
        monkeypatch,
        tmp_path,
        settings_factory,
        phrase=phrase,
        record=record,
        confirmed_note=note,
        chain_now=NOW,
        verify_ok=True,
    )
    manifest = _write(tmp_path, _trip_manifest())
    argv = ["--agreement-id", "31", "--manifest", str(manifest), "--cid", CID]
    assert release_edition.main(argv) == 0
    assert escrow.release_calls == [(31, record.commit_hash, note)]
    out = capsys.readouterr().out
    assert f"trip       1, sha256 {DIGEST}" in out
    assert f"note       earnest:travel:trip-1:agreement:31:cid:{CID}" in out
