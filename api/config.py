"""Environment configuration and the startup guards.

The server refuses to boot rather than serve a misconfigured 402. That is a
deliberate choice of failure mode: the challenge tag is stamped at settlement
and never reclassified, so a first MainNet settlement served from a wrong
configuration cannot be repaired afterwards. A refusal at boot is the cheapest
point on that curve, and every guard below is one that is otherwise **silent** --
verification succeeds, settlement succeeds, funds arrive, and only something
downstream is quietly wrong.

This is the only module that reads `os.environ`. Everything else takes a
`Settings`, which makes the whole backend constructor-injected and testable
without a network or a `.env`.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from algosdk.encoding import is_valid_address
from algosdk.logic import get_application_address
from x402.http.types import HTTPRequestContext
from x402.mechanisms.avm import (
    ALGORAND_MAINNET_CAIP2,
    ALGORAND_TESTNET_CAIP2,
    USDC_MAINNET_ASA_ID,
    USDC_TESTNET_ASA_ID,
)

# The network/asset pairs that are allowed to exist. Anything else is a
# configuration that would price a MainNet route in a test asset, or the
# reverse -- neither of which announces itself at runtime.
KNOWN_ASSETS: dict[str, int] = {
    ALGORAND_MAINNET_CAIP2: USDC_MAINNET_ASA_ID,
    ALGORAND_TESTNET_CAIP2: USDC_TESTNET_ASA_ID,
}

CHALLENGE_TAG = "x402-global-challenge"

# Ordered so that truncation costs the least. This is the Bazaar listing,
# and the catalogue may show only its opening; the one sentence a buyer
# cannot afford to miss is the one about the join call, so it leads. The
# product description follows it, and the terms that only matter once a
# buyer is already interested come last.
DEFAULT_INDEX_DESCRIPTION = (
    "Your payment group must include a join call against the escrow "
    "application, naming the agreement given in extra.agreementId — a "
    "transfer with no join call is not a purchase and the USDC it sends "
    "cannot be recovered. Join a pool that buys one edition of the Algorand "
    "x402 Route Index — every Algorand USDC x402 route probed live, with its "
    "catalogued price beside the price it actually quotes. Your USDC is held "
    "by an Algorand escrow app until the edition whose sha256 was committed "
    "before you paid is delivered — released against matching bytes, "
    "refunded on-chain if the seats do not fill or the edition is not "
    "delivered by the deadline. One seat per address: a wallet that already "
    "holds a seat in this pool cannot buy a second one. Machine-verified "
    "only: no arbitration, no human in the loop."
)

# What any POST /index description must still say, default or override.
# Matched as case-sensitive substrings; see `_check_index_description`.
INDEX_DESCRIPTION_CLAUSES = (
    "join call",
    "sha256",
    "refunded",
    "Machine-verified",
    "no arbitration",
    "One seat per address",
)


class ConfigError(RuntimeError):
    """A configuration the server refuses to serve."""


@dataclass(frozen=True)
class Settings:
    """Read once at boot and never mutated.

    Frozen on purpose: the guards check this object, so a later reassignment
    would mean the running server is serving something nothing validated.
    """

    network: str
    algod_url: str
    algod_token: str
    facilitator_url: str
    usdc_asa_id: int
    app_id: int
    app_account: str
    fee_payer: str
    treasury: str
    verifier: str
    # The only secret on this object, and kept out of the generated repr.
    # Anything that stringifies a Settings -- a traceback frame, a debug
    # log, pytest rendering the locals of every frame on an unrelated
    # failure -- would otherwise print a live mnemonic. Reading the field
    # is unaffected; only the repr elides it.
    admin_mnemonic: str = field(repr=False)
    public_host: str
    pin_resource_url: str
    index_resource_url: str
    # Fixed at creation: min_seats == max_seats once an agreement exists, so
    # these describe a decision rather than a default to tune. See
    # load_settings for the shipped values.
    index_share_price_micro: int
    index_seats: int
    # Which agreement this deployment serves as the open pool. 0 means none
    # configured, and the route refuses rather than falling back to a scan of
    # recent agreements -- that scan is still correct for the operator's
    # create-pool script, which has no configured id to trust, but a flood of
    # unrelated unpaid /pin quotes can push a real open pool outside any
    # bounded window, and a route that fell back to the same scan would go
    # dark exactly when it matters.
    index_agreement_id: int
    price_micro_usdc: int
    funding_window_seconds: int
    delivery_deadline_seconds: int
    max_file_bytes: int
    max_leg2_micro_usdc: int
    max_unfunded_agreements: int
    quotes_per_source_per_minute: int
    price_hook_timeout_seconds: int
    worker_url: str
    gateway_url: str
    job_db_path: str
    production_app_id: int
    # Whether `POST /pin` is served. Off by default: its delivery half -- the
    # upload, leg 2 and the release -- is not built, so a deployment serving it
    # would make the admin key sign a create for every unpaid quote and take
    # payments that can only ever be refunded. Refused on MainNet at boot.
    pin_route_enabled: bool
    # The built browser client, served under /app/ when this directory exists.
    web_dist_dir: str
    # POST /index's catalogue listing. Last and defaulted so a Settings built
    # field by field need not restate the listing; the boot guard holds any
    # override to INDEX_DESCRIPTION_CLAUSES.
    index_description: str = DEFAULT_INDEX_DESCRIPTION

    @property
    def resource_url(self) -> str:
        """The delivery-escrow route's resource.

        Kept as an alias so existing readers -- the pin route's RouteConfig and
        its tests -- are untouched by the move to two products.
        """
        return self.pin_resource_url

    @property
    def resource_urls(self) -> tuple[str, ...]:
        """Every resource this deployment serves, for the guards to iterate.

        `/pin`'s is left out while that route is off, so a `/pin` route that
        reaches the route table anyway is refused at boot as unconfigured.
        """
        if self.pin_route_enabled:
            return (self.pin_resource_url, self.index_resource_url)
        return (self.index_resource_url,)


def _int(env: Mapping[str, str], name: str, default: int | None = None) -> int:
    raw = env.get(name, "")
    if not raw:
        if default is None:
            raise ConfigError(f"{name} is required")
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _str(env: Mapping[str, str], name: str, default: str = "") -> str:
    """Read a string, treating an empty value as absent.

    `.env.example` ships every optional key present and empty, and
    `load_dotenv()` copies those empty strings into the environment -- so a
    plain `env.get(name, default)` finds the key, returns `""`, and the
    documented default never applies. `_int` already treats empty as absent;
    this makes the string reads agree with it, so "the template's default is
    what you get" is true of both.
    """
    return env.get(name, "") or default


def _bool(env: Mapping[str, str], name: str) -> bool:
    """Read a switch that defaults to off.

    Strict about spelling: `yes` or `on` refuse rather than read as off,
    because a switch deciding whether a paid route exists should not be
    decided by a typo in either direction.
    """
    raw = env.get(name, "").strip().lower()
    if raw in ("", "false", "0"):
        return False
    if raw in ("true", "1"):
        return True
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build settings from a mapping, defaulting to the process environment.

    Taking the environment as an argument rather than reaching for os.environ
    is what lets the guards be tested exhaustively without a `.env` file and
    without mutating global state.
    """
    env = os.environ if env is None else env
    public_host = _str(env, "PUBLIC_HOST").rstrip("/")
    pin_route_enabled = _bool(env, "PIN_ROUTE_ENABLED")
    return Settings(
        pin_route_enabled=pin_route_enabled,
        network=_str(env, "NETWORK", ALGORAND_TESTNET_CAIP2),
        algod_url=_str(env, "ALGOD_URL"),
        algod_token=_str(env, "ALGOD_TOKEN"),
        facilitator_url=_str(
            env, "FACILITATOR_URL", "https://facilitator.goplausible.xyz"
        ),
        usdc_asa_id=_int(env, "USDC_ASA_ID"),
        app_id=_int(env, "APP_ID"),
        app_account=_str(env, "APP_ACCOUNT"),
        fee_payer=_str(env, "FEE_PAYER"),
        treasury=_str(env, "TREASURY"),
        verifier=_str(env, "VERIFIER"),
        admin_mnemonic=_str(env, "ADMIN_MNEMONIC"),
        public_host=public_host,
        pin_resource_url=_str(env, "RESOURCE_URL", f"{public_host}/pin"),
        index_resource_url=_str(env, "INDEX_RESOURCE_URL", f"{public_host}/index"),
        # Five seats at $5.00. The seat count is fixed at creation and cannot
        # change afterwards, so this is a decision, not a default to tune.
        index_share_price_micro=_int(env, "INDEX_SHARE_PRICE_MICRO", 5_000_000),
        index_seats=_int(env, "INDEX_SEATS", 5),
        # Empty means unconfigured, exactly like RESOURCE_URL: the route
        # refuses to serve rather than guess which agreement is the open pool.
        index_agreement_id=_int(env, "INDEX_AGREEMENT_ID", 0),
        # Prices `/pin` and nothing else, so it is required only while that
        # route is served.
        price_micro_usdc=_int(
            env, "PRICE_MICRO_USDC", None if pin_route_enabled else 0
        ),
        funding_window_seconds=_int(env, "FUNDING_WINDOW_SECONDS", 120),
        delivery_deadline_seconds=_int(env, "DELIVERY_DEADLINE_SECONDS", 900),
        max_file_bytes=_int(env, "MAX_FILE_BYTES", 16 * 1024 * 1024),
        max_leg2_micro_usdc=_int(env, "MAX_LEG2_MICRO_USDC", 500_000),
        max_unfunded_agreements=_int(env, "MAX_UNFUNDED_AGREEMENTS", 50),
        quotes_per_source_per_minute=_int(env, "QUOTES_PER_SOURCE_PER_MINUTE", 20),
        price_hook_timeout_seconds=_int(env, "PRICE_HOOK_TIMEOUT_SECONDS", 60),
        worker_url=_str(env, "WORKER_URL"),
        gateway_url=_str(env, "GATEWAY_URL").rstrip("/"),
        job_db_path=_str(env, "JOB_DB_PATH", "jobs.sqlite3"),
        production_app_id=_int(env, "PRODUCTION_APP_ID", 0),
        web_dist_dir=_str(env, "WEB_DIST_DIR", "web/dist"),
        # Empty means the default, as for every `_str` read. The required
        # clauses are checked on whichever description is in effect by
        # `_check_index_description` at boot, not here, alongside the other
        # guards on what this deployment serves.
        index_description=_str(env, "INDEX_DESCRIPTION", DEFAULT_INDEX_DESCRIPTION),
    )


def _check_network_asset(settings: Settings) -> None:
    expected = KNOWN_ASSETS.get(settings.network)
    if expected is None:
        raise ConfigError(f"unknown network {settings.network!r}")
    if settings.usdc_asa_id != expected:
        raise ConfigError(
            f"network {settings.network} expects asset {expected}, "
            f"configured {settings.usdc_asa_id}"
        )


def _check_pin_route(settings: Settings) -> None:
    """`POST /pin` is not served on MainNet.

    Its delivery half -- the upload, leg 2 and the release -- is not built, so
    on MainNet it would take real USDC for a job that can only ever end in a
    refund. This guard goes when that half is built.
    """
    if settings.pin_route_enabled and settings.network == ALGORAND_MAINNET_CAIP2:
        raise ConfigError(
            "PIN_ROUTE_ENABLED is on in a MainNet configuration; POST /pin has "
            "no delivery path yet, so every payment it took could only be "
            "refunded"
        )


def _check_signing_key(settings: Settings) -> None:
    """With `POST /pin` off the server signs nothing, so it holds no key.

    Never decoding the key is not enough on its own: a mnemonic left in the
    environment -- a platform secret set by mistake, a shared `.env` -- still
    sits in the process and in these settings. Refusing to boot is what makes
    "this host holds no signing key" true rather than merely intended.
    """
    if settings.admin_mnemonic and not settings.pin_route_enabled:
        raise ConfigError(
            "ADMIN_MNEMONIC is set but PIN_ROUTE_ENABLED is off; this server "
            "signs nothing and must not hold the key -- remove it from the "
            "server's environment"
        )


def verify_before_assembly(settings: Settings) -> None:
    """The guards deciding what this process may hold.

    Run before any collaborator is built, so a configuration that will be
    refused never gets as far as decoding a key or opening a store, and the
    operator reads the guard's reason rather than a symptom of it.
    """
    _check_network_asset(settings)
    _check_pin_route(settings)
    _check_signing_key(settings)


def _check_network_application(settings: Settings) -> None:
    if settings.app_id <= 0:
        raise ConfigError("APP_ID is required")
    derived = get_application_address(settings.app_id)
    if derived != settings.app_account:
        raise ConfigError(
            f"APP_ACCOUNT is not the application address of APP_ID "
            f"{settings.app_id}: derived {derived}, configured {settings.app_account}"
        )
    if (
        settings.production_app_id
        and settings.app_id == settings.production_app_id
        and settings.network != ALGORAND_MAINNET_CAIP2
    ):
        raise ConfigError(
            "the production application id appears in a non-MainNet "
            "configuration; development and production are different "
            "applications, and therefore different merchant identities"
        )


def _check_addresses(settings: Settings) -> None:
    """Every configured address must be a real Algorand address.

    Without this the first thing to notice a typo'd TREASURY is algosdk, deep
    inside a create -- and `_sample_this_price` reports that as "sampling the
    price callable raised ...", which sends an operator looking at the price
    rather than at the one environment variable that is wrong. `verifier` is
    the sharpest case: the design requires it to be non-zero, and `_str`
    returns "" for an absent value, so an unset VERIFIER would otherwise reach
    an agreement create as an empty string.
    """
    for name, address in (
        ("APP_ACCOUNT", settings.app_account),
        ("FEE_PAYER", settings.fee_payer),
        ("TREASURY", settings.treasury),
        ("VERIFIER", settings.verifier),
    ):
        if not address:
            raise ConfigError(f"{name} is required")
        if not is_valid_address(address):
            raise ConfigError(f"{name} is not a valid Algorand address: {address!r}")


def _check_app_account_ready(settings: Settings, algod: Any) -> None:
    """The application account must be able to receive before we quote a price.

    An endpoint that answers a 402 its payTo cannot honour wastes the buyer's
    signature: the settle fails at the facilitator layer, after they have
    already built and signed a group.
    """
    info = algod.account_info(settings.app_account)
    if int(info.get("amount", 0)) <= 0:
        raise ConfigError(
            f"application account {settings.app_account} is not funded; it "
            "cannot pay the inner-transaction fees a release or refund charges it"
        )
    holdings = {int(a["asset-id"]): a for a in info.get("assets", [])}
    holding = holdings.get(settings.usdc_asa_id)
    if holding is None:
        raise ConfigError(
            f"application account is not opted in to asset {settings.usdc_asa_id}; "
            "it cannot receive the payment the 402 is about to ask for"
        )
    # Opt-in is only half of it: a frozen holding is an opted-in holding that
    # still rejects every transfer, and MainNet USDC declares a freeze address,
    # so this is reachable in production rather than theoretical.
    if holding.get("is-frozen"):
        raise ConfigError(
            f"the application account's holding of asset {settings.usdc_asa_id} "
            "is frozen, so it cannot receive a payment"
        )


def _check_resource(settings: Settings) -> None:
    """Every configured resource is HTTPS and on the configured host.

    One root domain per merchant account: endpoints differentiate resources,
    never domains, so the same payTo must not serve endpoints on two hosts.
    Loops over every configured resource -- both products share this
    settings object, so a boot that only checked the first would let a
    second, misconfigured resource through silently.
    """
    for resource in settings.resource_urls:
        if not resource.startswith("https://"):
            raise ConfigError(
                f"resource must be https://, got {resource!r}; a "
                "resource that is not https is not catalogued at all"
            )
        if not resource.startswith(settings.public_host + "/"):
            raise ConfigError(
                f"resource {resource!r} is not on the configured "
                f"public host {settings.public_host!r}; one root domain per "
                "merchant account"
            )


def _check_web_bundle(settings: Settings) -> None:
    """A client bundle that is present must be whole.

    The client is served only when `WEB_DIST_DIR` exists. A directory holding
    assets but no `index.html` -- a half-copied build -- would serve every
    asset and answer every page, the deep link a buyer recovers with included,
    with a 404.
    """
    dist = Path(settings.web_dist_dir)
    if dist.is_dir() and not (dist / "index.html").is_file():
        raise ConfigError(
            f"WEB_DIST_DIR {settings.web_dist_dir!r} exists but holds no "
            "index.html, so every client page would answer 404"
        )


def _check_index_description(settings: Settings) -> None:
    """The pool route's listing keeps the clauses that make it safe to buy from.

    The description is public catalogue copy: it is what a discovering agent
    reads before it pays, and nothing else in the payment path says these
    things. The join-call clause is the load-bearing one -- a transfer sent
    without a join call is not a purchase, and the USDC it sends cannot be
    recovered, so a listing that drops it lets a buyer lose money in a way
    nothing downstream can undo. The sha256, refund, one-seat and
    machine-verification clauses are the product's terms: a listing without
    them describes something other than what the escrow enforces. So an
    override may reword and reorder, but not lose any of
    `INDEX_DESCRIPTION_CLAUSES`.

    Runs for whatever description is in effect; the default carries every
    clause by construction.
    """
    missing = [
        c for c in INDEX_DESCRIPTION_CLAUSES if c not in settings.index_description
    ]
    if missing:
        raise ConfigError(
            f"INDEX_DESCRIPTION lacks {', '.join(repr(c) for c in missing)}; it "
            "is the pool route's public catalogue listing, and an override must "
            "keep every clause the default carries"
        )


SAMPLE_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_SAMPLE_QUERY = {"sha256": SAMPLE_SHA256, "size": "1024"}


def _sample_price(price: Any) -> Any:
    """Get the AssetAmount a dynamic price would serve, without side effects.

    A callable that exposes `.sample()` is asked for that instead of being
    run: api/pricing.py builds it from the same `_quote` the live callable
    returns, so the tag and fee payer under test are the real ones, while
    nothing goes to chain and nothing is written to the store.

    That matters because the alternative is not free. Running the callable for
    real creates an agreement, which parks minimum balance and writes a QUOTED
    row -- on *every* cold start outside the funding window of the last one.
    `count_unfunded()` has no window filter, so those rows keep counting, and a
    restart loop walks the unfunded ceiling until boot refuses outright with a
    capacity error that restarting cannot clear.

    The synthetic-request path below remains for a callable with no `.sample()`
    -- a plain function, as the guard tests use -- and still samples with one
    fixed sha256 so that even then repeated boots reuse a single quote while it
    is in window rather than parking a fresh deposit per restart.
    """
    sample = getattr(price, "sample", None)
    if sample is not None:
        return sample()

    class _Adapter:
        def get_query_param(self, name: str) -> str | None:
            return _SAMPLE_QUERY.get(name)

        def get_query_params(self) -> dict[str, str]:
            return dict(_SAMPLE_QUERY)

        def get_header(self, name: str) -> str | None:
            return None

        def get_method(self) -> str:
            return "POST"

        def get_path(self) -> str:
            return "/pin"

        def get_url(self) -> str:
            return "/pin"

        def get_body(self) -> None:
            return None

    ctx = HTTPRequestContext(
        adapter=_Adapter(), path="/pin", method="POST", payment_header=None
    )
    result = price(ctx)
    if asyncio.iscoroutine(result):
        result = asyncio.run(result)
    return result


def _sample_this_price(pattern: str, price: Any) -> Any:
    """Run `_sample_price`, turning any failure into a boot refusal.

    A crash-restart loop samples the same callable on every boot, walking the
    same unfunded-agreement ceiling and rate limiter a real caller would --
    so the callable raising here is very likely restart pressure (a capacity
    problem) rather than a misconfiguration. Boot refuses either way, since it
    cannot serve a 402 it cannot sample, but the message should not send an
    operator hunting through environment variables for what is actually a
    capacity problem.

    The distinction below is drawn on the exception's own base class
    (ValueError-ish vs RuntimeError-ish) rather than on api.pricing's concrete
    QuoteCeilingReached / RateLimited / BadQuoteRequest types: importing those
    here would cycle back through api.deps, which this module's Settings
    feeds -- api.config -> api.pricing -> api.deps -> api.config.
    """
    try:
        return _sample_price(price)
    except RuntimeError as exc:
        raise ConfigError(
            f"{pattern}: sampling the price callable hit a capacity limit "
            f"({exc}); this looks like restart pressure -- e.g. a crash-"
            "restart loop re-parking the same sample quote -- rather than a "
            "configuration error"
        ) from exc
    except Exception as exc:
        raise ConfigError(
            f"{pattern}: sampling the price callable raised {exc!r}"
        ) from exc


def _check_routes(settings: Settings, routes: Mapping[str, Any]) -> None:
    configured = set(settings.resource_urls)
    for pattern, config in routes.items():
        if config.resource not in configured:
            raise ConfigError(
                f"{pattern}: resource {config.resource!r} is not one of the "
                f"configured resources {sorted(configured)}; unpinned, the "
                "catalogue identity is whatever URL the request happened to "
                "carry, so a query-bearing route registers one near-duplicate "
                "directory entry per distinct query"
            )
        accepts = config.accepts
        options = accepts if isinstance(accepts, list) else [accepts]
        for option in options:
            if not option.pay_to:
                raise ConfigError(f"{pattern}: payTo is empty")
            if not config.hook_timeout_seconds:
                raise ConfigError(
                    f"{pattern}: hook_timeout_seconds is unset, so x402-avm "
                    "waits on the price callable with no timeout at all; a "
                    "chain that stops answering would hold the request open "
                    "indefinitely"
                )
            if config.hook_timeout_seconds >= settings.funding_window_seconds:
                raise ConfigError(
                    f"{pattern}: hook_timeout_seconds "
                    f"({config.hook_timeout_seconds}) is not inside the funding "
                    f"window ({settings.funding_window_seconds}); one slow quote "
                    "would consume the whole window the buyer is promised to "
                    "pay in"
                )
            if option.max_timeout_seconds != settings.funding_window_seconds:
                raise ConfigError(
                    f"{pattern}: max_timeout_seconds is "
                    f"{option.max_timeout_seconds}, funding window is "
                    f"{settings.funding_window_seconds}; advertising a window we "
                    "do not honour promises the buyer time the sweeper takes back"
                )
            price = option.price
            if callable(price):
                # The price is dynamic, so the tag and the fee payer are only
                # observable by running it. Boot is the right moment: it costs
                # one synthetic quote and it is the last point at which a
                # missing tag is still repairable.
                price = _sample_this_price(pattern, price)
            if isinstance(price, str):
                raise ConfigError(
                    f"{pattern}: a string price ({price!r}) silently discards "
                    "extra, and with it feePayer and the challenge tag -- "
                    "payments still settle, untagged and unattributable"
                )
            extra = getattr(price, "extra", None) or {}
            if extra.get("tag") != CHALLENGE_TAG:
                raise ConfigError(
                    f"{pattern}: price.extra.tag is {extra.get('tag')!r}, expected "
                    f"{CHALLENGE_TAG!r}; attribution is stamped at settlement and "
                    "never reclassified"
                )
            if not extra.get("feePayer"):
                raise ConfigError(f"{pattern}: price.extra.feePayer is missing")


def verify_startup(settings: Settings, routes: Mapping[str, Any], algod: Any) -> None:
    """Run every guard. Raises `ConfigError` on the first failure.

    Ordered cheapest-and-most-fundamental first, so the message a
    misconfiguration produces names the root problem rather than a symptom of
    it: a wrong network makes every later check meaningless.
    """
    verify_before_assembly(settings)
    _check_addresses(settings)
    _check_network_application(settings)
    _check_app_account_ready(settings, algod)
    _check_resource(settings)
    _check_web_bundle(settings)
    _check_index_description(settings)
    _check_routes(settings, routes)
