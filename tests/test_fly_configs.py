"""The deployment configs, checked offline.

Each carries several values twice -- once as a build argument compiled into
the client bundle, once as the server's environment -- and nothing at runtime
compares the two copies. A client built for one application id beside a server
serving another builds payment groups the server refuses, and it is found out
by a buyer. The MainNet file is also filled in by hand after its application is
deployed, which is exactly when one copy gets updated and the other does not.

Two of the files are TestNet deployments side by side, and each is its own
merchant. Nothing at runtime sees both, so nothing but this file notices one
of them copied from the other with an application id or a host left behind.
"""

import pathlib
import tomllib

import pytest
from x402.mechanisms.avm import ALGORAND_MAINNET_CAIP2, ALGORAND_TESTNET_CAIP2

from api.config import (
    DEFAULT_INDEX_DESCRIPTION,
    INDEX_DESCRIPTION_CLAUSES,
    load_settings,
    verify_startup,
)
from api.deps import build_deps
from api.routes import build_routes

ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIGS = {
    "fly.testnet.toml": ALGORAND_TESTNET_CAIP2,
    "fly.mainnet.toml": ALGORAND_MAINNET_CAIP2,
    "fly.travel-testnet.toml": ALGORAND_TESTNET_CAIP2,
}
TESTNET_CONFIGS = [
    name for name, network in CONFIGS.items() if network == ALGORAND_TESTNET_CAIP2
]
NON_MAINNET_CONFIGS = [
    name for name, network in CONFIGS.items() if network != ALGORAND_MAINNET_CAIP2
]

# (build argument, server variable) pairs that must hold the same value.
MIRRORED = [
    ("VITE_NETWORK", "NETWORK"),
    ("VITE_ASSET_ID", "USDC_ASA_ID"),
    ("VITE_APP_ID", "APP_ID"),
    ("VITE_FACILITATOR_URL", "FACILITATOR_URL"),
    ("VITE_RESOURCE_HOST", "PUBLIC_HOST"),
    ("VITE_ALGOD_URL", "ALGOD_URL"),
]


def _load(name: str) -> dict:
    return tomllib.loads((ROOT / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", CONFIGS)
@pytest.mark.parametrize(("build_arg", "server_var"), MIRRORED)
def test_the_client_and_the_server_are_built_for_the_same_thing(
    name, build_arg, server_var
):
    config = _load(name)
    assert config["build"]["args"][build_arg] == config["env"][server_var], (
        f"{name}: {build_arg} and {server_var} disagree"
    )


@pytest.mark.parametrize(("name", "network"), CONFIGS.items())
def test_each_config_names_its_own_network(name, network):
    assert _load(name)["env"]["NETWORK"] == network


@pytest.mark.parametrize("name", CONFIGS)
def test_no_config_serves_pin_or_holds_a_key_or_pins_a_pool(name):
    env = _load(name)["env"]
    assert env["PIN_ROUTE_ENABLED"] == "false"
    assert "ADMIN_MNEMONIC" not in env
    # A secret only: in [env], every deploy would restore a stale pool id.
    assert "INDEX_AGREEMENT_ID" not in env


def test_the_mainnet_config_names_its_application_as_production():
    env = _load("fly.mainnet.toml")["env"]
    assert env["PRODUCTION_APP_ID"] == env["APP_ID"]


@pytest.mark.parametrize("name", TESTNET_CONFIGS)
def test_the_testnet_config_passes_the_startup_guards(name):
    """Everything but the chain: the account read is faked as ready.

    The route table is built from this configuration by the assembly
    `api/server.py` runs, so the route guards (resource, tag, fee payer,
    timeouts) run as they do at a real boot. None of them makes a chain call:
    the price is sampled.

    The listing is checked on the built route rather than in the file: the
    catalogue reads the RouteConfig, so that is where an override has to land
    and where the clauses have to survive.
    """

    class ReadyAlgod:
        def account_info(self, address):
            return {
                "amount": 1_000_000,
                "assets": [{"asset-id": 10458941, "amount": 0, "is-frozen": False}],
            }

    algod = ReadyAlgod()
    env = _load(name)["env"]
    settings = load_settings({**env, "WEB_DIST_DIR": str(ROOT / "absent")})
    routes = build_routes(build_deps(settings, algod))

    assert set(routes) == {"POST /index"}
    verify_startup(settings, routes, algod)

    index = routes["POST /index"]
    for clause in INDEX_DESCRIPTION_CLAUSES:
        assert clause in index.description, f"{name}: listing lacks {clause!r}"
    assert index.description == env.get("INDEX_DESCRIPTION", DEFAULT_INDEX_DESCRIPTION)
    # The catalogue's example shows the size of pool a buyer would be joining.
    example = index.extensions["bazaar"]["info"]["output"]["example"]
    assert example["seatsTotal"] == int(env["INDEX_SEATS"])


def test_the_mainnet_config_serves_the_default_index_description():
    """The default is the listing for the product MainNet sells. An override
    in this file would be a second copy of it to keep in step, and the one a
    wording fix to the default would silently miss."""
    assert "INDEX_DESCRIPTION" not in _load("fly.mainnet.toml")["env"]


@pytest.mark.parametrize("name", CONFIGS)
def test_no_config_carries_an_offer(name):
    """The travel offer is supplied at deploy time as a build argument, so it
    changes without this file changing. A file carrying one as well would be
    a second source for it, with nothing at deploy time saying which one the
    bundle was built from; the image build already refuses a travel bundle
    deployed without one, so the file has no need of a fallback."""
    config = _load(name)
    for section in (config, config["build"]["args"], config["env"]):
        assert "VITE_OFFER_B64" not in section


@pytest.mark.parametrize("name", CONFIGS)
def test_only_the_travel_config_builds_the_travel_client(name):
    args = _load(name)["build"]["args"]
    if name == "fly.travel-testnet.toml":
        assert args["VITE_BRAND"] == "travel"
    else:
        # Absent means the image's default, the index client.
        assert args.get("VITE_BRAND", "index") == "index"


@pytest.mark.parametrize("key", ["APP_ID", "APP_ACCOUNT", "PUBLIC_HOST"])
def test_no_two_configs_share_an_application_or_a_host(key):
    """One payTo, one domain. Two deployments on one application would be one
    merchant behind two domains; two on one host would declare the same
    resource URL, which is the catalogue's identity for a listing, from two
    merchants."""
    seen: dict[str, str] = {}
    for name in CONFIGS:
        value = _load(name)["env"][key]
        assert value not in seen, f"{name} and {seen.get(value)} share {key}"
        seen[value] = name


@pytest.mark.parametrize(
    "name", [n for n in NON_MAINNET_CONFIGS if "PRODUCTION_APP_ID" in _load(n)["env"]]
)
def test_a_non_mainnet_config_names_the_mainnet_application_as_production(name):
    """PRODUCTION_APP_ID outside MainNet is a tripwire: the boot guard refuses
    a configuration whose APP_ID equals it. It trips only if it names the real
    production application, and a configuration naming its own APP_ID there
    would refuse every boot rather than just the wrong one."""
    env = _load(name)["env"]
    assert env["PRODUCTION_APP_ID"] != env["APP_ID"]
    assert env["PRODUCTION_APP_ID"] == _load("fly.mainnet.toml")["env"]["APP_ID"]


def test_the_travel_description_names_the_configured_seat_count():
    """The listing states its pool size in words, which nothing derives from
    INDEX_SEATS. Moving one without the other would sell a pool the catalogue
    misdescribes."""
    env = _load("fly.travel-testnet.toml")["env"]
    seats = env["INDEX_SEATS"]
    assert f"for {seats} travellers" in env["INDEX_DESCRIPTION"]
    assert f"if {seats} distinct buyers join" in env["INDEX_DESCRIPTION"]
