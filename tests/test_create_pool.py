"""The operator create-pool script's offline-checkable refusals.

The three preconditions that touch the network -- the admin/verifier check,
the cross-edition open-pool scan, and the create itself -- need a live chain
and are exercised by hand, not here (`find_any_open_pool`'s own logic is
covered offline in tests/test_pool.py). What belongs here are the refusals
that fire *before* any of that -- the manifest, the seat ceiling and the
deadline -- and are load-bearing precisely because they must not need a live
algod, a real admin key, or a chain read to say no.
"""

from __future__ import annotations

import json

import pytest
from algosdk import account, mnemonic
from algosdk.error import WrongMnemonicLengthError
from x402.mechanisms.avm import ALGORAND_MAINNET_CAIP2

from agents import create_pool
from agents.create_pool import create_note

COMMIT = "7ff9c6cc101dc4c53d6638f802fb3cd3e976339164194e45c0d804136eb0ddd9"


def test_a_manifest_with_a_short_sha256_is_refused(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"edition": 1, "sha256": "ab"}), encoding="utf-8")

    with pytest.raises(SystemExit, match="not 32 bytes"):
        create_pool.main(["--manifest", str(manifest), "--dry-run"])


def test_a_seat_count_past_the_measured_ceiling_is_refused_locally(
    tmp_path, settings_factory, monkeypatch
):
    """The script must discover a mistyped INDEX_SEATS itself, rather than
    letting the contract reject the create with an error naming no assertion
    -- so this refusal has to fire before `load_settings`'s result is used for
    anything that touches the network.
    """
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"edition": 1, "sha256": "00" * 32}), encoding="utf-8"
    )
    monkeypatch.setattr(
        create_pool,
        "load_settings",
        lambda: settings_factory(index_seats=create_pool.MEASURED_SEAT_CEILING + 1),
    )

    with pytest.raises(SystemExit, match="above the measured ceiling"):
        create_pool.main(["--manifest", str(manifest), "--dry-run"])


def test_a_seat_count_at_the_ceiling_is_not_refused_by_this_guard(
    tmp_path, settings_factory, monkeypatch
):
    """The boundary: exactly the measured ceiling must pass this particular
    check.

    Proven by where `main` fails instead. The next thing it does is derive
    the admin key from `ADMIN_MNEMONIC`, which `settings_factory` leaves
    empty, so the boundary case gets past the seat guard and dies on the
    mnemonic. Asserting that specific failure rather than `Exception` is what
    keeps this honest: a bare `pytest.raises(Exception)` would go on passing
    if the seat guard started raising for the wrong reason, or if `main`
    began failing somewhere earlier entirely.
    """
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({"edition": 1, "sha256": "00" * 32}), encoding="utf-8"
    )
    monkeypatch.setattr(
        create_pool,
        "load_settings",
        lambda: settings_factory(index_seats=create_pool.MEASURED_SEAT_CEILING),
    )

    with pytest.raises(WrongMnemonicLengthError):
        create_pool.main(["--manifest", str(manifest), "--dry-run"])


def _manifest(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"edition": 1, "sha256": "00" * 32}), encoding="utf-8")
    return str(path)


def test_the_two_deadline_flags_are_mutually_exclusive(tmp_path, capsys):
    """Not a preference: the two would have to be reconciled into one
    deadline, and any rule for doing so silently discards the one the
    operator meant. argparse refuses with exit code 2 before anything is
    read."""
    with pytest.raises(SystemExit) as exit_info:
        create_pool.main(
            [
                "--manifest",
                _manifest(tmp_path),
                "--deadline-days",
                "7",
                "--deadline-minutes",
                "25",
                "--dry-run",
            ]
        )

    assert exit_info.value.code == 2
    assert "not allowed with argument" in capsys.readouterr().err


def test_deadline_minutes_is_refused_on_mainnet(
    tmp_path, settings_factory, monkeypatch
):
    """The refusal this flag exists to carry. A MainNet pool created with a
    minutes-long deadline would expire before it could fill, and the contract
    offers no way to move a deadline -- so the only repair is the refund pass
    on a pool that sold nothing. Offline, and before the admin key is read.
    """
    monkeypatch.setattr(
        create_pool,
        "load_settings",
        lambda: settings_factory(network=ALGORAND_MAINNET_CAIP2, usdc_asa_id=31566704),
    )

    with pytest.raises(SystemExit, match="refused on MainNet"):
        create_pool.main(
            ["--manifest", _manifest(tmp_path), "--deadline-minutes", "25", "--dry-run"]
        )


def test_deadline_minutes_is_allowed_off_mainnet(
    tmp_path, settings_factory, monkeypatch
):
    """The boundary, proven the same way as the seat ceiling's: on TestNet the
    flag gets past this guard and dies on the empty ADMIN_MNEMONIC, which is
    the next thing `main` reads. Asserting that specific failure is what keeps
    the test honest if the guard later started refusing everywhere.
    """
    monkeypatch.setattr(create_pool, "load_settings", lambda: settings_factory())

    with pytest.raises(WrongMnemonicLengthError):
        create_pool.main(
            ["--manifest", _manifest(tmp_path), "--deadline-minutes", "25", "--dry-run"]
        )


@pytest.mark.parametrize("flag", ["--deadline-minutes", "--deadline-days"])
def test_a_deadline_at_or_below_zero_is_refused(
    tmp_path, settings_factory, monkeypatch, flag
):
    """A deadline already in the past creates a pool that is expired the
    moment it exists: nobody can join it, and the only thing left to do with
    it is the refund pass. The contract accepts any deadline, including that
    one, so the refusal has to be here."""
    monkeypatch.setattr(create_pool, "load_settings", lambda: settings_factory())

    with pytest.raises(SystemExit, match="must be at least 1"):
        create_pool.main(["--manifest", _manifest(tmp_path), flag, "0", "--dry-run"])


# --- the creating transaction's note --------------------------------------


@pytest.mark.parametrize(
    "committed, note",
    [
        ("edition-3.json", f"earnest:index:edition-3:sha256:{COMMIT}"),
        ("trip-3.json", f"earnest:travel:trip-3:sha256:{COMMIT}"),
    ],
    ids=["index", "travel"],
)
def test_the_create_note_takes_the_kind_the_manifest_names(committed, note):
    manifest = {"edition": 3, "file": committed, "sha256": COMMIT}
    assert create_note(manifest) == note.encode()


def test_a_manifest_without_a_file_gets_the_index_note():
    """The rehearsal writes manifests carrying only `edition` and `sha256`,
    and the note they get must be byte for byte the one they always got."""
    assert create_note({"edition": 1, "sha256": COMMIT}) == (
        f"earnest:index:edition-1:sha256:{COMMIT}".encode()
    )


def _unreachable(*args, **kwargs):
    raise AssertionError("nothing past the offline manifest checks may run")


@pytest.mark.parametrize(
    "committed, match",
    [
        ("trip-2.json", "`file` is 'trip-2.json', not 'trip-1.json'"),
        ("edition-2.json", "`file` is 'edition-2.json', not 'edition-1.json'"),
        (
            "catalogue-1.json",
            "`file` is 'catalogue-1.json', not 'edition-1.json' or 'trip-1.json'",
        ),
        ("trip-01.json", "not 'edition-1.json' or 'trip-1.json'"),
        (None, "`file` is None, not 'edition-1.json' or 'trip-1.json'"),
    ],
    ids=["trip-number", "edition-number", "unknown-stem", "leading-zero", "null"],
)
def test_a_file_that_names_no_kind_for_this_edition_is_refused_offline(
    tmp_path, monkeypatch, committed, match
):
    """A note under the wrong kind or number would file the pool where no
    reader of its kind looks, and it cannot be rewritten -- so the refusal
    fires before the settings, the key or the chain are touched."""
    monkeypatch.setattr(create_pool, "load_settings", _unreachable)
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps({"edition": 1, "file": committed, "sha256": COMMIT}),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match=match) as refused:
        create_pool.main(["--manifest", str(path), "--dry-run"])
    message = str(refused.value)
    assert message.startswith(f"{path}: ")
    assert message.endswith("Nothing was created.")


@pytest.mark.parametrize("edition", [0, True, "1", None])
def test_an_edition_that_is_not_a_positive_integer_is_refused_offline(
    tmp_path, monkeypatch, edition
):
    monkeypatch.setattr(create_pool, "load_settings", _unreachable)
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps({"edition": edition, "file": "trip-1.json", "sha256": COMMIT}),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit, match="`edition` is .*, not a positive integer"):
        create_pool.main(["--manifest", str(path), "--dry-run"])


class _DryRunEscrow:
    """Stands in for both the `EscrowClient` class and the instance it
    builds, answering the reads a dry run makes and refusing the one call a
    dry run must never reach."""

    def __call__(self, *args, **kwargs) -> _DryRunEscrow:
        return self

    def is_paused(self) -> bool:
        return False

    def min_fee(self) -> int:
        return 1_000

    def create_hash_agreement(self, **kwargs) -> int:
        raise AssertionError("a dry run must create nothing")


def test_the_dry_run_shows_the_create_note(
    tmp_path, monkeypatch, settings_factory, capsys
):
    """The note is the only place a pool's kind is recorded -- the contract
    holds the hash, not what it is a hash of -- so the operator reads it
    before anything is created. It is printed beside the report rather than
    inside it: the TestNet rehearsal asserts the report's exact keys."""
    private_key, _address = account.generate_account()
    monkeypatch.setattr(
        create_pool,
        "load_settings",
        lambda: settings_factory(admin_mnemonic=mnemonic.from_private_key(private_key)),
    )
    monkeypatch.setattr(create_pool, "EscrowClient", _DryRunEscrow())
    monkeypatch.setattr(create_pool, "find_any_open_pool", lambda escrow: None)
    path = tmp_path / "trip-4-manifest.json"
    path.write_text(
        json.dumps({"edition": 4, "file": "trip-4.json", "sha256": COMMIT}),
        encoding="utf-8",
    )

    assert create_pool.main(["--manifest", str(path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    report = json.loads(out[out.index("{") : out.rindex("}") + 1])
    assert "create_note" not in report
    assert f"note earnest:travel:trip-4:sha256:{COMMIT}\n" in out
    assert "dry run: nothing created" in out


class _RecordingEscrow(_DryRunEscrow):
    """A create that records what it was asked to send instead of sending it."""

    def __init__(self) -> None:
        self.created: dict = {}

    def create_hash_agreement(self, **kwargs) -> int:
        self.created = kwargs
        return 77


@pytest.mark.parametrize(
    "manifest, note",
    [
        (
            {"edition": 4, "file": "trip-4.json", "sha256": COMMIT},
            f"earnest:travel:trip-4:sha256:{COMMIT}",
        ),
        (
            {"edition": 1, "sha256": COMMIT},
            f"earnest:index:edition-1:sha256:{COMMIT}",
        ),
    ],
    ids=["travel", "index-without-file"],
)
def test_the_create_sends_the_note_it_showed(
    tmp_path, monkeypatch, settings_factory, capsys, manifest, note
):
    """The bytes on the creating transaction are the ones the dry run printed:
    a trip manifest's under the travel prefix, and a manifest with no `file`
    under the index prefix, exactly as before there was a second kind."""
    private_key, _address = account.generate_account()
    monkeypatch.setattr(
        create_pool,
        "load_settings",
        lambda: settings_factory(admin_mnemonic=mnemonic.from_private_key(private_key)),
    )
    escrow = _RecordingEscrow()
    monkeypatch.setattr(create_pool, "EscrowClient", escrow)
    monkeypatch.setattr(create_pool, "find_any_open_pool", lambda escrow: None)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    assert create_pool.main(["--manifest", str(path)]) == 0
    assert escrow.created["note"] == note.encode()
    assert escrow.created["commit_hash"] == bytes.fromhex(COMMIT)
    out = capsys.readouterr().out
    assert f"note {note}\n" in out
    assert "created agreement 77" in out


@pytest.mark.parametrize(
    "app_id, fly_app, app_flag",
    [
        # The bug this guards: run under a second deployment's env file,
        # `FLY_APP` still comes from `.env` and names the first deployment's
        # app. The fly config that serves the application wins.
        (772795100, "earnest-testnet", "-a earnest-travel"),
        (771795120, None, "-a earnest-testnet"),
        # An application no fly config in the repository serves: `FLY_APP`,
        # or the placeholder, exactly as before.
        (769608941, "some-app", "-a some-app"),
        (769608941, None, "-a <app>"),
    ],
    ids=["config-over-fly-app", "config-alone", "fly-app-fallback", "placeholder"],
)
def test_the_refresh_step_names_the_app_serving_the_application(
    tmp_path, monkeypatch, settings_factory, capsys, app_id, fly_app, app_flag
):
    """The printed `fly secrets set` line is pasted as it stands, so the app
    it names must be the one serving the application the pool was just
    created on -- never one carried in from a file written for another."""
    private_key, _address = account.generate_account()
    monkeypatch.setattr(
        create_pool,
        "load_settings",
        lambda: settings_factory(
            app_id=app_id, admin_mnemonic=mnemonic.from_private_key(private_key)
        ),
    )
    monkeypatch.setattr(create_pool, "EscrowClient", _RecordingEscrow())
    monkeypatch.setattr(create_pool, "find_any_open_pool", lambda escrow: None)
    if fly_app is None:
        monkeypatch.delenv("FLY_APP", raising=False)
    else:
        monkeypatch.setenv("FLY_APP", fly_app)

    assert create_pool.main(["--manifest", _manifest(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert f"next: fly secrets set INDEX_AGREEMENT_ID=77 {app_flag}\n" in out
