# Pinned Astronomy Shop revision and fault mapping

OpsAccord treats the OpenTelemetry Astronomy Shop as an external, isolated demo
environment. Nothing in this repository may invent a feature flag, a container
name, a port, or a tool capability: every identifier used by the application is
recorded here with the revision it was verified against.

## Provenance

| Item | Value |
| --- | --- |
| Repository | https://github.com/open-telemetry/opentelemetry-demo |
| Pinned revision | tag `3.1.0` |
| Commit | `dedc0178918e260823323b8d95005a8cb924b007` |
| Flag catalog | `src/flagd/demo.flagd.json` |
| Compose files | `compose.yaml`, `compose.full.yaml`, `compose.observability.yaml`, `compose.extras.yaml` |
| Verified on | 2026-09-23, from the public GitHub API at tag `3.1.0` |

The revision is configured as `OPSACCORD_ASTRONOMY_SHOP_REVISION` (default
`3.1.0`) and asserted by `tests/unit/test_tool_registry.py`. Bumping the pin
means re-verifying every flag, target, and port listed below.

## Fault flags verified at this revision

From `src/flagd/demo.flagd.json` at tag `3.1.0`:

| Flag | Variants | Phase 1 use |
| --- | --- | --- |
| `paymentUnreachable` | `off` (default), `on` | Payment-remediation fault |
| `adHighCpu` | `off` (default), `on` | Ad-service fault with an allow-listed remediation |
| `adFailure` | `off` (default), `on` | Verifiable adjacent Ad fault, not allow-listed in Phase 1 |
| `adManualGc` | `off` (default), `on` | Verifiable adjacent Ad fault, not allow-listed in Phase 1 |
| `cartFailure` | graded `off`, `10%`, `25%`, `50%`, `75%`, `90%`, `100%` | Cart handoff path |
| `paymentFailure` | graded `off`, `10%`, `25%`, `50%`, `75%`, `90%`, `100%` | Not allow-listed in Phase 1 |

These six identifiers are the closed allow-list in
`src/opsaccord/tools/registry.py` (`VERIFIED_FLAG_IDS`). `cartFailure` and
`paymentFailure` are deliberately *not* selected as automated remediations: the
flag is graded, so "disabling" it would mean choosing a variant on the
operator's behalf, and the Cart scenario is required to end in a human handoff
instead.

## Fault to remediation mapping

| Demo target | Fault flag | Permitted Phase 1 remediation | Gate |
| --- | --- | --- | --- |
| `payment-service` | `paymentUnreachable` | `disable_feature_flag(paymentUnreachable)` | Exact human approval |
| `ad-service` | `adHighCpu` | `disable_feature_flag(adHighCpu)` | Allow-listed, autonomous |
| `cart-service` | `cartFailure` | none | Evidence-backed human handoff |

`restart_workload` exists as a second typed remediation primitive, but it is not
allow-listed for autonomous execution; it requires an exact approval.

## Service and port map (tag `3.1.0`)

`compose.yaml` is the minimal core stack; `compose.full.yaml` adds Kafka,
accounting, and fraud-detection, and `compose.observability.yaml` adds Jaeger,
Prometheus, OpenSearch, Grafana, and the OpAMP server.

| Component | Compose service | Published port | Notes |
| --- | --- | --- | --- |
| Ad service | `ad` | `${AD_PORT}` = 9555 | Metrics exposed on `${AD_PROMETHEUS_PORT}` = 9465 |
| Cart service | `cart` | `${CART_PORT}` = 7070 | Lives in Valkey (`valkey-cart` = 6379) |
| Checkout service | `checkout` | `${CHECKOUT_PORT}` = 5050 | gRPC health probe |
| Payment service | `payment` | `${PAYMENT_PORT}` = 50051 | `Charge` RPC, gRPC health probe |
| Frontend / Envoy proxy | `frontend-proxy` | `${ENVOY_PORT}` = 8080 | Routes `/feature` to `flagd-ui` |
| Feature-flag service | `flagd` | `8013`, `8016` | Serves `file:./etc/flagd/demo.flagd.json` |
| Feature-flag console | `flagd-ui` | `${FLAGD_UI_PORT}` = 4000 | REST control plane, see below |
| OpenTelemetry Collector | `otel-collector` | `4317` (gRPC), `4318` (HTTP) | Receives all demo telemetry |
| Prometheus | `prometheus` | `9090` | Present in the observability layer |
| Jaeger | `jaeger` | `16686` (UI) | Present in the observability layer |

The `x-default-logging` anchor in `compose.yaml` caps each container's logs at
5 MB x 2 files. Memory limits declared in `compose.yaml` include `ad` 300 MB,
`cart` 160 MB, `flagd-ui` 200 MB, `flagd` 75 MB, and `otel-collector` 400 MB.
Running core plus observability therefore needs several GB of free RAM and the
usual Docker/Compose prerequisites; it was **not** run in this environment
(see the status note below).

## Feature-flag control plane

`flagd-ui` is a Phoenix application. At tag `3.1.0` its README documents a REST
API, and `src/frontend-proxy/envoy.tmpl.yaml` routes the `/feature` prefix to the
`flagd-ui` cluster with a `/` prefix rewrite, so the host-facing base URL is the
Envoy listener on `http://localhost:8080/feature`.

| Operation | Method and path | Body |
| --- | --- | --- |
| Read the whole flag document | `GET /feature/api/read` | none |
| Replace the whole flag document | `POST /feature/api/write` | `{"data": {<entire flag document>}}` |
| Legacy file read / write | `GET /read-file`, `POST /write-to-file` | see upstream README |

The write endpoint **rewrites the entire document**, so any remediation tool
must read, mutate one variant, and write the full payload back. That is exactly
why the OpsAccord tool surface exposes a single-purpose
`disable_feature_flag(target, flag_id)` capability instead of a generic
HTTP call: the model never composes a URL or a request body.

Re-verify at any time with:

```
curl -s http://localhost:8080/feature/api/read
```

## Status in this environment: no live adapter, all evidence simulated

* **Docker and Compose are unavailable here.** `docker` is not on `PATH` in this
  WSL distribution (the shell reports that Docker Desktop WSL integration is
  off), `podman` is not installed, and `/var/run/docker.sock` does not exist. The
  live Astronomy Shop stack therefore could not be started.
* **There is no live tool adapter.** `src/opsaccord/tools/demo_environment.py`
  is a deterministic in-process model of this revision. It mirrors the flag
  identifiers and the payment/ad/cart fault shapes so the controller, policy
  engine, gateway, recovery logic, and acceptance scenarios can be exercised
  end to end, and it labels every result `is_simulated=True`.
* **`demo_mode=live` is refused.** Because no adapter can produce live
  telemetry, `Settings` rejects `OPSACCORD_DEMO_MODE=live` rather than run the
  simulator under a live label. Simulated evidence is never reported as live.
* Live execution is the one Phase 1 gate that could not be demonstrated; the
  implementation report records it as a precise blocker rather than a pass.

## What a live adapter would need

1. Typed reads against Prometheus (`9090`) and Jaeger (`16686`) for the metric
   and trace tools, bounded by the existing attempt and deadline budgets.
2. A flagd-ui client for `GET /feature/api/read` and `POST /feature/api/write`
   with full-document read-modify-write, restricted to the six verified flags.
3. The Compose stack (core plus observability layers) running locally, with the
   `adHighCpu`, `paymentUnreachable`, and `cartFailure` faults driven through the
   verified control plane.

None of this is implemented yet, and it must not be inferred from the simulated
scenario results in `docs/reports/phase1-implementation.md`.
