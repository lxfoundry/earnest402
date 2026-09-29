"""Price-callable behaviour, spec section 12.2.

The callable is the only place an agreement is created, so its idempotency is
what stands between a 402 probe -- which is exactly what a discovering agent
does -- and an unbounded parking of deposits."""

import pytest

from api.pricing import (
    TRUSTED_CLIENT_IP_HEADER,
    BadQuoteRequest,
    QuoteCeilingReached,
    RateLimited,
    SourceLimiter,
    make_price_callable,
)
from tests.conftest import await_price

SHA = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_two_unpaid_passes_in_window_create_one_agreement(deps, ctx_factory):
    price = make_price_callable(deps)
    first = await_price(price, ctx_factory(sha256=SHA, size=1024))
    second = await_price(price, ctx_factory(sha256=SHA, size=1024))
    assert first.extra["agreementId"] == second.extra["agreementId"]
    assert deps.escrow.created == 1


def test_a_paid_pass_creates_nothing(deps, ctx_factory):
    price = make_price_callable(deps)
    await_price(price, ctx_factory(sha256=SHA, size=1024))
    await_price(price, ctx_factory(sha256=SHA, size=1024, payment_header="PAYLOAD"))
    assert deps.escrow.created == 1


def test_a_paid_pass_for_an_unknown_quote_is_refused(deps, ctx_factory):
    price = make_price_callable(deps)
    with pytest.raises(BadQuoteRequest):
        await_price(price, ctx_factory(sha256=SHA, size=1024, payment_header="PAYLOAD"))


def test_a_funded_agreement_is_never_handed_to_a_second_buyer(deps, ctx_factory):
    price = make_price_callable(deps)
    first = await_price(price, ctx_factory(sha256=SHA, size=1024))
    agreement_id = first.extra["agreementId"]
    deps.jobs.mint_job_id(agreement_id=agreement_id, job_id="tok")

    second = await_price(price, ctx_factory(sha256=SHA, size=1024))
    assert second.extra["agreementId"] != agreement_id
    assert deps.escrow.created == 2


def test_an_elapsed_quote_is_not_reused(deps, ctx_factory):
    price = make_price_callable(deps)
    first = await_price(price, ctx_factory(sha256=SHA, size=1024))
    deps.clock.advance(deps.settings.funding_window_seconds + 1)
    second = await_price(price, ctx_factory(sha256=SHA, size=1024))
    assert second.extra["agreementId"] != first.extra["agreementId"]


def test_the_price_carries_the_tag_the_fee_payer_and_the_agreement_id(
    deps, ctx_factory
):
    price = make_price_callable(deps)
    quote = await_price(price, ctx_factory(sha256=SHA, size=1024))
    assert quote.amount == str(deps.settings.price_micro_usdc)
    assert quote.asset == str(deps.settings.usdc_asa_id)
    assert quote.extra["tag"] == "x402-global-challenge"
    assert quote.extra["feePayer"] == deps.settings.fee_payer
    assert quote.extra["decimals"] == 6
    assert quote.extra["agreementId"] >= 1


def test_a_malformed_query_is_refused_before_anything_is_created(deps, ctx_factory):
    price = make_price_callable(deps)
    for bad in (
        {"sha256": "not-hex", "size": 1024},
        {"sha256": SHA.upper(), "size": 1024},  # the schema says lowercase hex
        {"sha256": SHA, "size": 0},
        {"sha256": SHA, "size": -1},
        {"sha256": SHA[:-1], "size": 1024},
        {"sha256": None, "size": 1024},
        {"sha256": SHA, "size": None},
    ):
        with pytest.raises(BadQuoteRequest):
            await_price(price, ctx_factory(**bad))
    assert deps.escrow.created == 0


def test_a_file_over_the_maximum_is_refused(deps, ctx_factory):
    price = make_price_callable(deps)
    with pytest.raises(BadQuoteRequest):
        await_price(
            price, ctx_factory(sha256=SHA, size=deps.settings.max_file_bytes + 1)
        )
    assert deps.escrow.created == 0


def test_a_source_is_rate_limited_independently_of_the_ceiling(deps, ctx_factory):
    """Section 6 names three controls and requires all three: idempotency, a
    ceiling on concurrently unfunded agreements, and a rate limit per source.
    The ceiling bounds the total; only the rate limit stops one caller reaching
    it on its own."""
    price = make_price_callable(deps)
    for n in range(deps.settings.quotes_per_source_per_minute):
        await_price(
            price, ctx_factory(sha256=f"{n:064x}", size=1024, source="10.0.0.1")
        )
    with pytest.raises(RateLimited):
        await_price(price, ctx_factory(sha256="a" * 64, size=1024, source="10.0.0.1"))
    # A different source is unaffected: the limit is per caller, not global.
    await_price(price, ctx_factory(sha256="b" * 64, size=1024, source="10.0.0.2"))


def test_the_rate_limit_window_rolls_forward(deps, ctx_factory):
    price = make_price_callable(deps)
    for n in range(deps.settings.quotes_per_source_per_minute):
        await_price(
            price, ctx_factory(sha256=f"{n:064x}", size=1024, source="10.0.0.1")
        )
    deps.clock.advance(61)
    await_price(price, ctx_factory(sha256="c" * 64, size=1024, source="10.0.0.1"))


def test_the_unfunded_ceiling_stops_the_quoting(deps, ctx_factory):
    """The ceiling is a global count of unfunded agreements, independent of
    which caller opened each one. Each call below therefore comes from its
    own source: sharing one source here would additionally trip the
    per-source rate limiter (a different, per-caller control asserted by
    test_a_source_is_rate_limited_independently_of_the_ceiling), and the two
    controls have no fixed relative size that satisfies both tests from a
    single shared caller, since one test needs the rate limit to bind first
    and the other needs the ceiling to. Distinct sources isolate this test to
    the ceiling alone, which is what it is named for.
    """
    price = make_price_callable(deps)
    for n in range(deps.settings.max_unfunded_agreements):
        await_price(
            price,
            ctx_factory(sha256=f"{n:064x}", size=1024, source=f"10.0.{n}.1"),
        )
    with pytest.raises(QuoteCeilingReached):
        await_price(
            price,
            ctx_factory(
                sha256="f" * 64,
                size=1024,
                source=f"10.0.{deps.settings.max_unfunded_agreements}.1",
            ),
        )


def test_the_deposit_is_read_from_the_live_minimum_fee(deps, ctx_factory):
    deps.algod.min_fee = 2_000
    price = make_price_callable(deps)
    await_price(price, ctx_factory(sha256=SHA, size=1024))
    assert deps.escrow.last_deposit == 97_800


def test_the_limiter_evicts_a_source_once_its_window_elapses():
    """_seen is keyed on client-supplied input (a forwarded-header value), so
    it must not grow by one permanent entry per source ever seen: a caller
    that spoofs a fresh source on every request, or that simply never comes
    back, would otherwise leave a dead key behind forever."""
    limiter = SourceLimiter(per_minute=5)
    limiter.check("10.0.0.1", now=1_000_000)
    assert "10.0.0.1" in limiter._seen

    # 61 seconds later, a call from a different source sweeps the first
    # source's now-fully-elapsed entry out of the dict.
    limiter.check("10.0.0.2", now=1_000_061)
    assert "10.0.0.1" not in limiter._seen
    assert "10.0.0.2" in limiter._seen


def test_a_spoofed_forwarded_hop_does_not_buy_a_fresh_rate_limit_bucket(
    deps, ctx_factory
):
    """The limiter keys on what the proxy observed, not what the caller claimed.

    X-Forwarded-For is appended to by each proxy, so the *last* entry is the
    one the nearest trusted proxy wrote and every earlier entry is caller
    input. A caller varying its first hop per request is the whole attack:
    keyed on the leftmost value it gets an unlimited number of buckets, and
    each quote it slips through parks an ALGO deposit.
    """
    price = make_price_callable(deps)
    allowed = deps.settings.quotes_per_source_per_minute

    for n in range(allowed):
        await_price(
            price,
            ctx_factory(
                sha256=f"{n:064x}",
                size=1024,
                # A different claimed first hop every time; same real caller.
                headers={"x-forwarded-for": f"203.0.113.{n}, 10.0.0.7"},
            ),
        )

    with pytest.raises(RateLimited):
        await_price(
            price,
            ctx_factory(
                sha256="a" * 64,
                size=1024,
                headers={"x-forwarded-for": "198.51.100.42, 10.0.0.7"},
            ),
        )


def test_a_proxy_set_client_ip_header_outranks_the_forwarded_list(deps, ctx_factory):
    """A single-valued header the caller cannot extend wins when present.

    The forwarded list below claims a different caller on every request; the
    proxy-set header says they are all the same one, and it is the one that
    decides.
    """
    price = make_price_callable(deps)
    allowed = deps.settings.quotes_per_source_per_minute

    for n in range(allowed):
        await_price(
            price,
            ctx_factory(
                sha256=f"{n:064x}",
                size=1024,
                headers={
                    TRUSTED_CLIENT_IP_HEADER: "10.0.0.7",
                    "x-forwarded-for": f"203.0.113.{n}",
                },
            ),
        )

    with pytest.raises(RateLimited):
        await_price(
            price,
            ctx_factory(
                sha256="b" * 64,
                size=1024,
                headers={
                    TRUSTED_CLIENT_IP_HEADER: "10.0.0.7",
                    "x-forwarded-for": "198.51.100.42",
                },
            ),
        )


def test_the_boot_sample_agrees_with_what_the_callable_serves(deps, ctx_factory):
    """The drift this closes: a sample that no longer describes the real price.

    api/config.py checks the tag and the fee payer against `price.sample()`
    rather than by running the callable, so a sample that has drifted from the
    callable turns the startup guard into a check of the wrong object -- and
    the guard's whole purpose is that a missing tag is unrepairable after the
    first MainNet settlement.
    """
    price = make_price_callable(deps)

    served = await_price(price, ctx_factory(sha256="a" * 64, size=1024))
    sampled = price.sample()

    assert sampled.amount == served.amount
    assert sampled.asset == served.asset
    # Everything but the agreement id, which is the one thing a sample cannot
    # know and the one thing the guard does not look at.
    assert {k: v for k, v in sampled.extra.items() if k != "agreementId"} == {
        k: v for k, v in served.extra.items() if k != "agreementId"
    }


def test_the_boot_sample_costs_nothing(deps):
    """No chain call, no store write -- which is the entire point of it."""
    price = make_price_callable(deps)

    sample = price.sample()

    assert sample.extra["tag"] == "x402-global-challenge"
    assert deps.escrow.created == 0
    assert deps.jobs.count_unfunded() == 0
