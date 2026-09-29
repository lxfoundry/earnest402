"""What the deployment image takes from `editions/`, built with real Docker.

The host serves each edition's free tier under `/app/editions/`, so the
report can be opened on the same origin that sells the edition. That
directory is also where `editions/README.md` tells a seat holder to put the
dataset they bought before hashing it. So on a machine that deploys,
`editions/` can hold a sold file that was never committed, and a deploy sends
the working tree, not the commit.

Two filters keep a sold file out, and each is tested on its own:

- `.dockerignore` keeps it out of the build context, so it never leaves the
  machine that deploys;
- the Dockerfile's `editions` stage copies only the published names, so an
  image built from a context that does hold a sold file still carries none.

Only small stages are built: they start from scratch and run nothing, so
these tests take seconds and pull no image. A machine with no reachable
Docker daemon skips them; CI never does.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Hand-written stand-ins: what is checked is which names make it through, and
# that their bytes arrive unchanged.
PUBLISHED = {
    "edition-7-sample.html": b"<!doctype html><title>free report</title>\n",
    "edition-7-sample.json": b'{"sample_of": "stand-in"}\n',
    "edition-7-manifest.json": b'{"edition": 7}\n',
}
SOLD = {
    "edition-7.json": b'{"rows": "sold"}\n',
    "edition-7.csv": b"route,price\nsold,1\n",
    "edition-7.html": b"<!doctype html><title>sold report</title>\n",
}


def _docker_reachable() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        probe = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=60, check=False
        )
    except subprocess.TimeoutExpired:
        return False
    return probe.returncode == 0


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        not os.environ.get("CI") and not _docker_reachable(),
        reason="no reachable Docker daemon",
    ),
]


def _editions_dir(context: Path) -> None:
    editions = context / "editions"
    editions.mkdir(parents=True)
    for name, data in {**PUBLISHED, **SOLD}.items():
        (editions / name).write_bytes(data)


def _export(context: Path, dockerfile: Path, out: Path, target: str | None = None):
    """Build and write the resulting filesystem to `out`, never to an image."""
    command = ["docker", "build", "--file", str(dockerfile)]
    if target is not None:
        command += ["--target", target]
    command += ["--output", f"type=local,dest={out}", str(context)]
    built = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env={**os.environ, "DOCKER_BUILDKIT": "1"},
    )
    assert built.returncode == 0, built.stderr
    return {path.name: path.read_bytes() for path in out.iterdir() if path.is_file()}


def test_the_build_context_carries_the_published_files_and_no_sold_one(tmp_path):
    context = tmp_path / "context"
    _editions_dir(context)
    shutil.copyfile(ROOT / ".dockerignore", context / ".dockerignore")
    # Outside the context, so the context's own .dockerignore is the one
    # applied: BuildKit would prefer a `Dockerfile.dockerignore` beside it.
    probe = tmp_path / "probe" / "Dockerfile"
    probe.parent.mkdir()
    probe.write_text("FROM scratch\nCOPY editions/ /\n", encoding="utf-8")

    received = _export(context, probe, tmp_path / "out")

    assert received == PUBLISHED


def test_the_editions_stage_copies_the_published_files_byte_for_byte_and_no_sold_one(
    tmp_path,
):
    context = tmp_path / "context"
    _editions_dir(context)
    # No .dockerignore: this is the Dockerfile's filter alone.
    shutil.copyfile(ROOT / "Dockerfile", context / "Dockerfile")

    stage = _export(
        context, context / "Dockerfile", tmp_path / "out", target="editions"
    )

    assert stage == PUBLISHED
