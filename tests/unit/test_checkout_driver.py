"""The live harness's synthetic checkout traffic and the storefront preflight.

These pin the properties that make it safe to let the harness place orders: it calls
two fixed paths on one configured origin, it is bounded, a failed order is a count and
never an exception, it runs only for the Payment plans and only when a factory is
passed, it stops on every exit path, and the result says the traffic was synthetic.
"""

from __future__ import annotations

import contextlib
import json
import threading
from typing import Any

import httpx
import pytest

from opsaccord import cli
from opsaccord.config import Settings
from opsaccord.scenarios import live as live_scenarios
from opsaccord.scenarios.traffic import CheckoutDriver, describe, storefront_origin
from support.live_fakes import (
    FakeFlagStore,
    FakeStack,
    StubController,
    health,
    live_settings,
    stack_health,
    use_fake_stack,
)

UNUSED_DB: Any = None


def _shop(responses: dict[str, int] | None = None, *, raise_on: str | None = None):
    """A MockTransport shop that records every request."""
    seen: list[tuple[str, str, dict[str, Any]]] = []
    lock = threading.Lock()
    codes = {"/api/cart": 200, "/api/checkout": 200, **(responses or {})}

    def handler(request: httpx.Request) -> httpx.Response:
        with lock:
            seen.append((request.method, str(request.url), json.loads(request.content or b"{}")))
        if raise_on and request.url.path == raise_on:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(codes.get(request.url.path, 404))

    return seen, httpx.Client(transport=httpx.MockTransport(handler))


def _run(driver: CheckoutDriver) -> dict[str, Any]:
    driver.start()
    assert driver._thread is not None
    driver._thread.join(5)
    return driver.stop()


def test_an_order_is_a_cart_add_then_a_checkout_on_the_configured_origin() -> None:
    seen, client = _shop()
    summary = _run(
        CheckoutDriver(
            "http://shop.test:8080/", interval_seconds=0.001, max_checkouts=3, client=client
        )
    )

    assert summary["attempts"] == 3
    assert summary["outcomes"] == {"checkout_ok": 3}
    assert summary["kind"] == "synthetic_checkout"
    assert [(m, u) for m, u, _ in seen] == [
        ("POST", "http://shop.test:8080/api/cart"),
        ("POST", "http://shop.test:8080/api/checkout"),
    ] * 3
    # Each order is its own user, and the checkout names the user the cart was filled for.
    carts = [body for _, url, body in seen if url.endswith("/api/cart")]
    checkouts = [body for _, url, body in seen if url.endswith("/api/checkout")]
    assert len({body["userId"] for body in carts}) == 3
    assert [b["userId"] for b in carts] == [b["userId"] for b in checkouts]
    assert all(body["item"]["quantity"] == 1 for body in carts)


def test_failed_orders_are_counted_and_never_raised() -> None:
    _, failing_checkout = _shop({"/api/checkout": 500})
    assert _run(
        CheckoutDriver(
            "http://shop.test", interval_seconds=0.001, max_checkouts=2, client=failing_checkout
        )
    )["outcomes"] == {"checkout_http_500": 2}

    seen, failing_cart = _shop({"/api/cart": 503})
    summary = _run(
        CheckoutDriver(
            "http://shop.test", interval_seconds=0.001, max_checkouts=2, client=failing_cart
        )
    )
    assert summary["outcomes"] == {"cart_http_503": 2}
    assert not any(url.endswith("/api/checkout") for _, url, _ in seen)

    _, refused = _shop(raise_on="/api/cart")
    assert _run(
        CheckoutDriver("http://shop.test", interval_seconds=0.001, max_checkouts=1, client=refused)
    )["outcomes"] == {"transport_error:ConnectError": 1}


def test_stop_ends_the_driver_before_its_cap_and_is_idempotent() -> None:
    _, client = _shop()
    driver = CheckoutDriver(
        "http://shop.test", interval_seconds=60.0, max_checkouts=50, client=client
    )
    driver.start()
    first = driver.stop()
    assert driver._thread is not None and not driver._thread.is_alive()
    assert first["attempts"] == 1
    assert driver.stop()["attempts"] == 1
    with pytest.raises(RuntimeError):
        driver.start()


def test_an_unbounded_driver_is_refused() -> None:
    for kwargs in ({"interval_seconds": 0}, {"max_checkouts": 0}, {"timeout_seconds": 0}):
        with pytest.raises(ValueError):
            CheckoutDriver("http://shop.test", **kwargs)


def test_the_storefront_origin_is_the_flag_control_planes_unless_configured() -> None:
    assert storefront_origin(Settings(flag_control_base_url="http://127.0.0.1:8080/feature")) == (
        "http://127.0.0.1:8080"
    )
    assert storefront_origin(Settings(storefront_base_url="http://shop.local:9000/")) == (
        "http://shop.local:9000"
    )
    with pytest.raises(ValueError):
        Settings(storefront_base_url="file:///etc/passwd")


def test_describe_says_the_traffic_was_synthetic() -> None:
    assert "load generator's traffic only" in describe(None)
    line = describe(
        {
            "base_url": "http://shop.test",
            "interval_seconds": 15.0,
            "attempts": 4,
            "outcomes": {"checkout_http_500": 3, "checkout_ok": 1},
            "duration_seconds": 61.0,
        }
    )
    assert "4 order(s)" in line and "checkout_http_500=3" in line
    assert "the measured telemetry includes them" in line


# -- wiring into the live lifecycle ---------------------------------------------------


class FakeDriver:
    base_url = "http://shop.test"
    interval_seconds = 15.0
    max_checkouts = 60

    def __init__(self, log: list[str], plan: Any) -> None:
        self.log = log
        self.plan = plan

    def start(self) -> FakeDriver:
        self.log.append(f"start {self.plan.scenario_id}")
        return self

    def stop(self) -> dict[str, Any]:
        self.log.append(f"stop {self.plan.scenario_id}")
        return {
            "kind": "synthetic_checkout",
            "base_url": self.base_url,
            "interval_seconds": self.interval_seconds,
            "max_checkouts": self.max_checkouts,
            "attempts": 2,
            "outcomes": {"checkout_http_500": 2},
            "duration_seconds": 1.0,
        }


def _factory(log: list[str]) -> Any:
    def build(plan: Any) -> Any:
        return FakeDriver(log, plan) if plan.drive_checkout else None

    return build


def test_only_the_payment_plans_drive_checkout() -> None:
    driven = sorted(sid for sid, plan in live_scenarios.LIVE_PLANS.items() if plan.drive_checkout)
    assert driven == ["P1-01", "P1-05", "P1-06", "P1-07"]


def test_a_payment_plan_starts_and_stops_the_driver_and_records_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeFlagStore()
    probes = stack_health(**{"payment-service": health(calls=0.2, error_rate=0.4)})
    use_fake_stack(monkeypatch, FakeStack(store, probes), StubController())
    log: list[str] = []

    run = live_scenarios.run_live_suite(
        db=UNUSED_DB,
        settings=live_settings(),
        scenario_ids=["P1-01", "P1-02"],
        scenario_budget_seconds=0.3,
        poll_seconds=0.01,
        checkout_driver=_factory(log),
    )

    assert log == ["start P1-01", "stop P1-01"]
    p1_01, p1_02 = run.results
    record = p1_01.to_record()
    assert record["synthetic_traffic"]["attempts"] == 2
    assert any(r.startswith("synthetic checkout traffic: 2 order(s)") for r in record["reasons"])
    assert p1_02.to_record()["synthetic_traffic"] is None


def test_the_driver_is_stopped_and_recorded_when_the_premise_never_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No traffic at all: the scenario fails at traffic-wait, after the driver started.
    use_fake_stack(monkeypatch, FakeStack(FakeFlagStore(), stack_health()))
    log: list[str] = []

    run = live_scenarios.run_live_suite(
        db=UNUSED_DB,
        settings=live_settings(),
        scenario_ids=["P1-01"],
        scenario_budget_seconds=0.3,
        poll_seconds=0.01,
        checkout_driver=_factory(log),
    )

    assert log == ["start P1-01", "stop P1-01"]
    (result,) = run.results
    assert result.status == "NOT_MEASURED"
    assert result.synthetic_traffic is not None
    assert any("synthetic checkout traffic" in r for r in result.reasons)


def test_without_a_factory_no_driver_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    store = FakeFlagStore()
    probes = stack_health(**{"payment-service": health(calls=0.2, error_rate=0.4)})
    use_fake_stack(monkeypatch, FakeStack(store, probes), StubController())

    run = live_scenarios.run_live_suite(
        db=UNUSED_DB,
        settings=live_settings(),
        scenario_ids=["P1-01"],
        scenario_budget_seconds=0.3,
        poll_seconds=0.01,
    )

    assert run.results[0].synthetic_traffic is None


# -- the storefront preflight ----------------------------------------------------------


class _Prometheus:
    def __init__(self, calls: float | None, errors: float | None) -> None:
        self.calls, self.errors = calls, errors
        self.queries: list[str] = []

    def resolve_metric(self, candidates: tuple[str, ...], timeout_seconds: float) -> str:
        return candidates[0]

    def scalar(self, query: str, timeout_seconds: float) -> float | None:
        self.queries.append(query)
        return self.errors if "STATUS_CODE_ERROR" in query else self.calls


class _Surface:
    is_simulated = False

    def __init__(self, prometheus: _Prometheus) -> None:
        self.prometheus = prometheus

    def set_flag_variant(self, **kwargs: Any) -> None:  # pragma: no cover - never called
        raise AssertionError("the preflight must not write")

    def read_flag_document(self, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        return {"flags": {}}


@pytest.mark.parametrize(
    ("calls", "errors", "healthy"),
    [
        (30.0, 0.0, True),
        (30.0, 3.0, True),
        (30.0, 29.0, False),
        (0.0, 0.0, None),
        (None, None, None),
    ],
)
def test_the_storefront_preflight_reads_the_frontends_catalog_calls(
    calls: float | None, errors: float | None, healthy: bool | None
) -> None:
    prometheus = _Prometheus(calls, errors)
    result = live_scenarios.storefront_preflight(_Surface(prometheus))  # type: ignore[arg-type]

    assert result["healthy"] is healthy
    assert all("service_name='frontend'" in q for q in prometheus.queries)
    assert all("ProductCatalogService" in q for q in prometheus.queries)


def test_the_cli_refuses_a_storefront_that_cannot_reach_the_catalog(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started: list[str] = []
    monkeypatch.setattr(
        cli,
        "preflight",
        lambda env, **kw: {
            "flag_count": 18,
            "missing_verified_flags": [],
            "prometheus_series": 300,
        },
    )
    monkeypatch.setattr(
        cli,
        "storefront_preflight",
        lambda env, **kw: {
            "healthy": False,
            "calls_per_minute": 30.0,
            "error_share": 0.97,
            "detail": "frontend -> product-catalog: 30.0 calls/min, 97.0% failing",
        },
    )
    monkeypatch.setattr(cli, "LiveEnvironment", lambda **kw: object())
    monkeypatch.setattr(cli, "run_live_suite", lambda **kw: started.append("ran"))

    assert cli.main(["run-live-scenarios"]) == 2
    output = capsys.readouterr().out
    assert "REFUSED" in output and "restart product-catalog checkout" in output
    assert started == []


def test_the_cli_passes_the_driver_unless_it_is_switched_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []
    monkeypatch.setattr(
        cli,
        "preflight",
        lambda env, **kw: {
            "flag_count": 18,
            "missing_verified_flags": [],
            "prometheus_series": 300,
        },
    )
    monkeypatch.setattr(
        cli,
        "storefront_preflight",
        lambda env, **kw: {
            "healthy": True,
            "calls_per_minute": 30.0,
            "error_share": 0.0,
            "detail": "ok",
        },
    )
    monkeypatch.setattr(cli, "LiveEnvironment", lambda **kw: object())

    class _NoDb:
        def dispose(self) -> None:
            pass

    monkeypatch.setattr(cli, "_db", lambda settings: _NoDb())

    def fake_suite(**kwargs: Any) -> Any:
        seen.append(kwargs["checkout_driver"])
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run_live_suite", fake_suite)
    for argv in (["run-live-scenarios"], ["run-live-scenarios", "--no-checkout-driver"]):
        with contextlib.suppress(KeyboardInterrupt):
            cli.main(argv)
    assert callable(seen[0]) and seen[1] is None
