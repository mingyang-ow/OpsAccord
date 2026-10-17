"""Bounded synthetic checkout traffic for the live Payment paths.

The live Payment plans (P1-01, P1-05, P1-06, P1-07) are measured on the caller's own
``checkout -> payment`` ``Charge`` client spans. Those spans only exist when an order
reaches the payment step, and the pinned demo's load generator does not guarantee one:
it averages a couple of checkouts a minute, stops for minutes at a time, and produces
none at all while ``product-catalog`` is unhealthy. On 2026-10-17 that left P1-01 with
no ``Charge`` call for its whole 420 s fault-visibility budget and P1-07 with none in
its verification window, so neither path could be measured.

This module removes that dependence. While a Payment path runs, the harness places its
own orders through the shop's public HTTP API - exactly what the load generator does:
one ``POST /api/cart`` and one ``POST /api/checkout`` per order, through the frontend
proxy. The orders travel the real request path, so the spans they produce are ordinary
live telemetry; nothing is injected into Prometheus or Jaeger and nothing reads them
here.

What it is allowed to do, and what it is not:

* It is harness code, never a tool. The model, the policy engine and the gateway never
  see it, and it never takes a URL or an argument from an incident.
* It calls two fixed paths on one origin that comes from the operator's configuration.
* It is bounded three ways - an interval between orders, a total order count, and a
  per-request timeout - and it stops when the scenario body finishes, whatever the exit.
* It never raises into the scenario. A failed order is a count in the summary, because
  orders are *expected* to fail while a Payment fault is armed.
* The summary is recorded on every live result, so a reader can see that the traffic
  the measurement saw was partly synthetic.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from opsaccord.config import Settings

#: One order every 15 s is about 4/min, above the 1.56-2.43/min the passing runs saw
#: from the load generator alone, so a 120 s span-metric window holds several Charge
#: calls even when the load generator is idle.
CHECKOUT_INTERVAL_SECONDS = 15.0
#: A scenario budget is 900 s; 60 orders at 15 s cover it, and the cap stops a run
#: whose stop signal never arrives from ordering forever.
MAX_CHECKOUTS = 60
REQUEST_TIMEOUT_SECONDS = 10.0
#: How long ``stop`` waits for an in-flight order before giving up on it.
STOP_JOIN_SECONDS = 2 * REQUEST_TIMEOUT_SECONDS + 1.0

#: Product ids of the pinned revision's catalog (``src/load-generator/locustfile.py``).
PRODUCT_IDS = (
    "0PUK6V6EV0",
    "1YMWWN1N4O",
    "2ZYFJ3GM2N",
    "66VCHSJNUP",
    "6E92ZMYYFZ",
    "9SIQT8TOJO",
    "L9ECAV7KIM",
    "LS4PSXUNUM",
    "OLJCESPC7Z",
    "HQTGWGPNH4",
)

#: The shape of the pinned load generator's ``people.json`` entries. The card number is
#: the upstream fixture's test card; the address is a documentation placeholder.
CHECKOUT_PERSON: dict[str, Any] = {
    "email": "opsaccord-live-driver@example.com",
    "address": {
        "streetAddress": "1600 Amphitheatre Parkway",
        "zipCode": "94043",
        "city": "Mountain View",
        "state": "CA",
        "country": "United States",
    },
    "userCurrency": "USD",
    "creditCard": {
        "creditCardNumber": "4432-8015-6152-0454",
        "creditCardExpirationMonth": 1,
        "creditCardExpirationYear": 2039,
        "creditCardCvv": 672,
    },
}

USER_AGENT = "opsaccord-live-checkout-driver"


def storefront_origin(settings: Settings) -> str:
    """The shop's origin: configured explicitly, or the flag control plane's origin.

    The flag control plane is reached through the frontend proxy's ``/feature`` prefix
    on the same fixed port that serves the shop, so its origin *is* the shop's origin
    unless the operator says otherwise.
    """
    if settings.storefront_base_url:
        return settings.storefront_base_url.rstrip("/")
    parts = urlsplit(settings.flag_control_base_url)
    return f"{parts.scheme}://{parts.netloc}"


class CheckoutDriver:
    """Place bounded synthetic orders on a background thread until stopped."""

    def __init__(
        self,
        base_url: str,
        *,
        interval_seconds: float = CHECKOUT_INTERVAL_SECONDS,
        max_checkouts: int = MAX_CHECKOUTS,
        timeout_seconds: float = REQUEST_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval_seconds <= 0 or max_checkouts < 1 or timeout_seconds <= 0:
            raise ValueError("the checkout driver must be bounded")
        self.base_url = base_url.rstrip("/")
        self.interval_seconds = interval_seconds
        self.max_checkouts = max_checkouts
        self.timeout_seconds = timeout_seconds
        self._client = client
        self._owns_client = client is None
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._attempts = 0
        self._outcomes: Counter[str] = Counter()
        self._started_at: float | None = None
        self._stopped_at: float | None = None

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> CheckoutDriver:
        if self._thread is not None:
            raise RuntimeError("the checkout driver was already started")
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.timeout_seconds, headers={"User-Agent": USER_AGENT}
            )
        self._started_at = self._clock()
        self._thread = threading.Thread(
            target=self._run, name="opsaccord-checkout-driver", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> dict[str, Any]:
        """Stop placing orders and return the summary. Safe to call more than once."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(STOP_JOIN_SECONDS)
        if self._stopped_at is None:
            self._stopped_at = self._clock()
            if self._owns_client and self._client is not None:
                self._client.close()
        return self.summary()

    def __enter__(self) -> CheckoutDriver:
        return self.start()

    def __exit__(self, *_: object) -> None:
        self.stop()

    # -- work ------------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                if self._attempts >= self.max_checkouts:
                    return
                self._attempts += 1
                sequence = self._attempts
            outcome = self._place_order(sequence)
            with self._lock:
                self._outcomes[outcome] += 1
            if self._stop.wait(self.interval_seconds):
                return

    def _place_order(self, sequence: int) -> str:
        """One order. Returns a short outcome label and never raises."""
        assert self._client is not None
        user = str(uuid.uuid4())
        product = PRODUCT_IDS[sequence % len(PRODUCT_IDS)]
        try:
            cart = self._client.post(
                f"{self.base_url}/api/cart",
                json={"item": {"productId": product, "quantity": 1}, "userId": user},
                timeout=self.timeout_seconds,
            )
            if cart.status_code >= 400:
                return f"cart_http_{cart.status_code}"
            order = self._client.post(
                f"{self.base_url}/api/checkout",
                json={**CHECKOUT_PERSON, "userId": user},
                timeout=self.timeout_seconds,
            )
        except httpx.TimeoutException:
            return "timeout"
        except httpx.HTTPError as exc:
            return f"transport_error:{type(exc).__name__}"
        return "checkout_ok" if order.status_code < 400 else f"checkout_http_{order.status_code}"

    # -- evidence --------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        with self._lock:
            outcomes = dict(sorted(self._outcomes.items()))
            attempts = self._attempts
        duration = None
        if self._started_at is not None:
            end = self._stopped_at if self._stopped_at is not None else self._clock()
            duration = round(end - self._started_at, 1)
        return {
            "kind": "synthetic_checkout",
            "base_url": self.base_url,
            "interval_seconds": self.interval_seconds,
            "max_checkouts": self.max_checkouts,
            "attempts": attempts,
            "outcomes": outcomes,
            "duration_seconds": duration,
        }


def describe(summary: dict[str, Any] | None) -> str:
    """One human-readable line for the run log and the result's reasons."""
    if not summary:
        return "synthetic checkout traffic: none (the load generator's traffic only)"
    outcomes = ", ".join(f"{k}={v}" for k, v in summary["outcomes"].items()) or "none finished"
    return (
        f"synthetic checkout traffic: {summary['attempts']} order(s) placed through "
        f"{summary['base_url']} every {summary['interval_seconds']}s over "
        f"{summary['duration_seconds']}s ({outcomes}); the measured telemetry includes them"
    )


__all__ = [
    "CHECKOUT_INTERVAL_SECONDS",
    "MAX_CHECKOUTS",
    "CheckoutDriver",
    "describe",
    "storefront_origin",
]
