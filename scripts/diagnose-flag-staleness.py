#!/usr/bin/env python
"""Does restarting flagd under a running checkout leave checkout's flag cache stale?

On 2026-10-18 checkout kept failing every Charge after ``paymentUnreachable`` had been
turned off: the flag store read ``off`` and checkout behaved as ``on``. checkout's flagd
provider caches flag values and learns of changes through an event stream. The leading
reading was that the stream broke when flagd restarted, since flagd and checkout had both
been restarted at 07:06 with checkout up about 3 s before flagd was listening. This
experiment tests that reading directly, in three phases, each judged by real orders:

1. control: on a healthy stack, arm the flag, then clear it. checkout should follow both.
2. restart **only flagd**, with checkout left running, then arm and clear again. If
   checkout no longer follows, the stale cache is reproduced and its trigger is named.
3. restart checkout, then arm and clear once more. checkout should follow again.

A phase "follows" when orders fail while the flag is on and succeed once it is off. Each
check places a few real orders through the storefront, the same path as every Payment
scenario.

Safety: it changes only ``paymentUnreachable``, and only through the flag store; it
restarts only flagd and checkout, through ``scripts/demo.sh``; and on every exit path it
turns the flag off and reads it back. It writes ``result.json`` and ``experiment.log`` to
``--out``.

Exit status: 0 the experiment ran (whatever it found); 1 it could not run or could not
restore the flag; 2 usage.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from opsaccord.scenarios.live import live_settings
from opsaccord.scenarios.traffic import CHECKOUT_PERSON, PRODUCT_IDS, storefront_origin
from opsaccord.tools.live_environment import LiveEnvironment

FLAG = "paymentUnreachable"
ON, OFF = "on", "off"
ORDERS_PER_CHECK = 3
REPO_ROOT = Path(__file__).resolve().parents[1]

_log_lines: list[str] = []


def log(message: str) -> None:
    line = f"{datetime.now(UTC).strftime('%H:%M:%S')} {message}"
    _log_lines.append(line)
    print(line, flush=True)


def order(client: httpx.Client, base: str) -> str:
    """One real order through the storefront; 'ok', 'http_<code>' or an error name."""
    user = str(uuid.uuid4())
    try:
        cart = client.post(
            f"{base}/api/cart",
            json={"item": {"productId": PRODUCT_IDS[0], "quantity": 1}, "userId": user},
        )
        if cart.status_code >= 400:
            return f"cart_http_{cart.status_code}"
        checkout = client.post(f"{base}/api/checkout", json={**CHECKOUT_PERSON, "userId": user})
    except httpx.HTTPError as exc:
        return type(exc).__name__
    return "ok" if checkout.status_code < 400 else f"http_{checkout.status_code}"


def check(client: httpx.Client, base: str, label: str) -> list[str]:
    results = [order(client, base) for _ in range(ORDERS_PER_CHECK)]
    log(f"  {label}: orders -> {results}")
    return results


def demo(*args: str) -> None:
    log(f"  demo.sh {' '.join(args)}")
    subprocess.run(["bash", "scripts/demo.sh", *args], cwd=REPO_ROOT, check=True)


def set_flag(env: LiveEnvironment, variant: str) -> None:
    env.set_flag_variant(flag_id=FLAG, variant=variant, timeout_seconds=10.0)
    log(f"  {FLAG} set to {variant!r}")


def phase(
    env: LiveEnvironment, client: httpx.Client, base: str, name: str, settle: float
) -> dict[str, Any]:
    log(f"phase {name}")
    set_flag(env, ON)
    time.sleep(settle)
    while_on = check(client, base, "flag on")
    set_flag(env, OFF)
    time.sleep(settle)
    after_off = check(client, base, "flag off")
    follows_on = all(r != "ok" for r in while_on)
    follows_off = all(r == "ok" for r in after_off)
    verdict = "follows" if follows_on and follows_off else "does not follow"
    log(f"  -> checkout {verdict} the flag (on: failing={follows_on}, off: ok={follows_off})")
    return {
        "while_on": while_on,
        "after_off": after_off,
        "follows_on": follows_on,
        "follows_off": follows_off,
        "follows": follows_on and follows_off,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, help="directory for result.json and the log")
    parser.add_argument("--settle-seconds", type=float, default=15.0)
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    settings = live_settings()
    env = LiveEnvironment(settings=settings)
    base = storefront_origin(settings)
    result: dict[str, Any] = {"flag": FLAG, "started_at": datetime.now(UTC).isoformat()}
    rc = 0
    with httpx.Client(timeout=15.0) as client:
        try:
            log(f"baseline (flag should be off): {check(client, base, 'baseline')}")
            result["control"] = phase(
                env, client, base, "1/3 control, healthy stack", args.settle_seconds
            )

            log("restarting flagd only; checkout keeps running")
            demo("restart", "flagd")
            time.sleep(20)  # flagd listening again, before any flag change
            result["after_flagd_restart"] = phase(
                env,
                client,
                base,
                "2/3 after restarting flagd under a running checkout",
                args.settle_seconds,
            )

            log("restarting checkout so it reconnects with a fresh cache")
            demo("restart", "checkout")
            demo("verify-recovery", "checkout")
            time.sleep(10)
            result["after_checkout_restart"] = phase(
                env, client, base, "3/3 after restarting checkout", args.settle_seconds
            )
        except Exception as exc:
            log(f"experiment stopped: {type(exc).__name__}: {exc}")
            result["error"] = f"{type(exc).__name__}: {exc}"
            rc = 1
        finally:
            try:
                set_flag(env, OFF)
                read_back = env.read_flag_document(timeout_seconds=10.0)["flags"][FLAG][
                    "defaultVariant"
                ]
                log(f"cleanup: {FLAG} read back {read_back!r}")
                result["cleanup_read_back"] = read_back
                if read_back != OFF:
                    rc = 1
            except Exception as exc:
                log(f"cleanup FAILED: {type(exc).__name__}: {exc}; turn {FLAG} off by hand")
                result["cleanup_read_back"] = None
                rc = 1

    control = result.get("control", {}).get("follows")
    stale = result.get("after_flagd_restart", {}).get("follows")
    recovered = result.get("after_checkout_restart", {}).get("follows")
    if control and stale is False and recovered:
        verdict = (
            "REPRODUCED: checkout follows the flag on a healthy stack, stops following it after "
            "flagd restarts under it, and follows again once checkout restarts"
        )
    elif control and stale and recovered:
        verdict = "NOT REPRODUCED: checkout kept following the flag after flagd restarted"
    else:
        verdict = "INCONCLUSIVE: the control phase or the recovery phase did not behave as expected"
    result["verdict"] = verdict
    log(f"verdict: {verdict}")
    (out / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    (out / "experiment.log").write_text("\n".join(_log_lines) + "\n", encoding="utf-8")
    return rc


if __name__ == "__main__":
    sys.exit(main())
