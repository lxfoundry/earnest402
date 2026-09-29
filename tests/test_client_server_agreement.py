"""Values the client and the server must agree on, pinned rather than
documented.

The client refuses an oversized file *before* the unpaid pass, because that
pass commits an agreement on chain and parks its deposit -- quoting a file
that cannot complete loses that deposit for nothing. That refusal is only
correct while the client's limit equals the server's.

Mirroring the constant is a known-weak arrangement and the spec says so: the
durable home is the paid route's discovery input schema, which a buyer agent
already reads. Until that exists, this test is what stops the mirror drifting.
It is not hypothetical -- the two files disagreed the first time the client
template was written.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER_TEMPLATE = REPO_ROOT / ".env.example"
CLIENT_TEMPLATE = REPO_ROOT / "web" / ".env.example"


@cache
def _values(template: Path) -> dict[str, str | None]:
    """The template parsed as the file it is, rather than by loading the
    environment -- the templates are the thing under test, not the process.

    `dotenv_values` reads the file and never touches `os.environ`, and it is
    the same parser that reads the real `.env` at runtime, so a template this
    test accepts is one the application can actually load. A hand-rolled
    regex would part company with it over quoting, `export ` prefixes or a
    comment on the value line.
    """
    return dotenv_values(template)


def _read(template: Path, key: str) -> str:
    value = _values(template).get(key)
    assert value is not None, f"{key} is missing from {template.name}"
    return value.strip()


def test_the_two_templates_agree_on_the_maximum_file_size():
    server = _read(SERVER_TEMPLATE, "MAX_FILE_BYTES")
    client = _read(CLIENT_TEMPLATE, "VITE_MAX_FILE_BYTES")
    assert client == server, (
        f"web/.env.example advertises {client} bytes and .env.example enforces "
        f"{server}. A client limit above the server's lets a buyer commit an "
        "agreement, and its deposit, to a job the server will refuse."
    )


def test_the_two_templates_agree_on_the_payment_asset():
    assert _read(CLIENT_TEMPLATE, "VITE_ASSET_ID") == _read(
        SERVER_TEMPLATE, "USDC_ASA_ID"
    ), "the client would build a transfer in an asset the route does not accept"


def test_the_two_templates_agree_on_the_network():
    assert _read(CLIENT_TEMPLATE, "VITE_NETWORK") == _read(
        SERVER_TEMPLATE, "NETWORK"
    ), "the client would look for a requirement the 402 does not offer"
