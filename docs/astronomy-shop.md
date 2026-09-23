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
| Checkout service | `checkout` | `${CHECKOUT_PORT}` = 5050 | gRPC health probe; needs the local memory override, see below |
| Payment service | `payment` | `${PAYMENT_PORT}` = 50051 | `Charge` RPC, gRPC health probe |
| Frontend / Envoy proxy | `frontend-proxy` | `${ENVOY_PORT}` = 8080 | Routes `/feature` to `flagd-ui`; one of only two stable host ports |
| Feature-flag service | `flagd` | `8013`, `8016` | Serves `file:./etc/flagd/demo.flagd.json` |
| Feature-flag console | `flagd-ui` | `${FLAGD_UI_PORT}` = 4000 | REST control plane, see below |
| OpenTelemetry Collector | `otel-collector` | `4317` (gRPC), `4318` (HTTP) | Receives all demo telemetry |
| Prometheus | `prometheus` | `9090` | Present in the observability layer; the second stable host port |
| Jaeger | `jaeger` | `16686` (UI) | Present in the observability layer |

On this host only two of those host ports are stable: `frontend-proxy` (8080)
and `prometheus` (9090). Every other published port is allocated from the
ephemeral range, so the harness resolves them at run time instead of hard-coding
them; see "Live adapter calibration" below.

The `x-default-logging` anchor in `compose.yaml` caps each container's logs at
5 MB x 2 files. Memory limits declared in `compose.yaml` include `ad` 300 MB,
`cart` 160 MB, `flagd-ui` 200 MB, `flagd` 75 MB, and `otel-collector` 400 MB.
Running core plus observability therefore needs several GB of free RAM and the
usual Docker/Compose prerequisites. Measured footprint here: 14 containers,
~6 GB of the host's 15.5 GB in use while the stack runs.

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

The container itself serves `GET /api/read`; Envoy strips the `/feature` prefix
on the way through. Both shapes were verified against the running stack:

```
curl -s http://localhost:8080/feature/api/read   # through the proxy
curl -s http://localhost:<flagd-ui>/api/read     # direct, resolved by scripts/demo.sh endpoints
```

## Live adapter calibration (verified against the running stack)

Measured on 2026-09-23 against the pinned `3.1.0` stack running in Docker Desktop
under WSL2 and driven through `scripts/demo.sh`. These numbers are measurements,
not assumptions; the exact commands and their output are recorded in
`docs/reports/phase1-implementation.md`.

### Host ports are not the compose defaults

Only `frontend-proxy` (8080) and `prometheus` (9090) are stable. Observed
ephemeral allocations in one run: jaeger `65459`, flagd-ui `65489`,
otel-collector `65481`/`65482`, grafana `65464`, ad `65492`, cart `65493`,
payment `65490`, checkout `61289`, flagd `65456`/`65457`. `scripts/demo.sh
endpoints` resolves them with `docker compose port` and writes
`.local/demo-endpoints.env`, which the live commands load. Checkout's port in
particular moves between stack restarts.

### Span names differ per service, and the fault is not always on the obvious RPC

| Target | Verified span | Notes |
| --- | --- | --- |
| `payment-service` | `oteldemo.PaymentService/Charge` | Emitted by both `payment` (server) and `checkout` (client); the client side carries `STATUS_CODE_ERROR` |
| `ad-service` | `oteldemo.AdService/GetAds` | `adHighCpu` is a resource fault: it shows in container CPU, not in span status |
| `cart-service` | `POST /oteldemo.CartService/EmptyCart` | `cartFailure` routes **EmptyCart** to the bad cart store; `GetCart` is untouched by the flag |
| `checkout-service` | `oteldemo.CheckoutService/PlaceOrder` | `paymentUnreachable` is read by checkout (`src/checkout/main.go`), so caller-side PlaceOrder spans fail |

The cart server span uses the ASP.NET naming convention rather than the
`oteldemo.*` one, which is why the adapter keeps an explicit per-target span map
rather than deriving a name from the service.

### Two telemetry pipelines, two cadences

* **Span metrics** (`traces_span_metrics_*`) publish every ~60s. Sampled 24
  times: `[60s]` returned no data 24/24 times, `[90s]` 12/24, and `[120s]`,
  `[180s]` and `[300s]` never. No span-derived query may use a window shorter
  than 120s, or it reads as "no traffic" instead of "no data".
* **Container metrics** (`container_cpu_usage_nanoseconds_total`,
  `container_memory_usage_total_bytes`) come from the `docker_stats` receiver and
  are much fresher: samples were never older than ~8s. CPU questions can use a
  60s window.

`container_cpu_utilization_ratio` is **not** usable. With the ad container idle
(docker stats 0.11-0.31% CPU) it reported `4.015`, and it kept reporting 4.0 for
minutes after a CPU burn stopped. The adapter differentiates the cumulative
counter instead:

```
sum(rate(container_cpu_usage_nanoseconds_total{container_name='<ctr>'}[<window>s])) / 1e9
```

which answers in cores and matched docker stats to two decimal places at every
sample.

### Measured fault signatures

| Fault | Signal | Fault off | Fault armed |
| --- | --- | --- | --- |
| `paymentUnreachable=on` | Charge error share (120s window) | 0.0 | ~0.33 |
| `paymentUnreachable=on` | PlaceOrder error share (90s window) | 0.0 | ~0.75 |
| `paymentUnreachable=on` | Charge call rate | ~0.075-0.13/s | ~0.042/s |
| `adHighCpu=on` | ad CPU in cores (120s window) | ~0.010 | ~1.06 |
| `cartFailure=100%` | EmptyCart | all `UNSET` | ERROR series grows, success series freezes |

The load generator (`LOCUST_USERS=5`, `LOCUST_HEADLESS=false`) is bursty: Charge
traffic is checkout-driven at roughly 4/min and stops for minutes at a time while
GetCart runs at ~1/s. A quiet window is therefore not a symptom, and no live
predicate may treat "no calls" as "faulted" or as "recovered".

### Calibrated run parameters

| Parameter | Simulated default | Live value | Why |
| --- | --- | --- | --- |
| Health-probe window (payment, ad) | 120s | 120s | shortest span window that always has samples |
| Health-probe window (cart) | 300s | 300s | EmptyCart carries roughly one call per 1-2 minutes |
| Ad `cpu_utilization` read window | 300s | 60s | container metrics are fresh; a five-minute average dilutes a burn that started a minute ago |
| Verification window | 300s | 120s | lets a recovering signal leave the lookback promptly |
| Verification settling | 1 attempt / 0s | 6 rounds x 30s | covers one 120s lookback plus one export interval |
| Step budgets | soft 8 / hard 16 | soft 12 / hard 32 | investigation 5 + dispatch 1 + 6 settle rounds x 3 reads |

Both budgets stay enforced; the live values are larger because bounded settling
for a real telemetry cadence costs more reads than the simulator needs. They are
set in one place, `opsaccord.scenarios.live.live_settings`, so the CLI, the live
integration tests, and the report describe the same run.

### Local override for the checkout container

`compose.yaml` pins `checkout` to `memory: 20M` with `GOMEMLIMIT=16MiB`. Under
that limit the pinned Go service enters a GC death spiral: ~700% CPU, 18.1/20 MiB
resident, gRPC never listening, and 118 consecutive failed health probes.
`demo/compose.opsaccord.yaml` raises only the resource limits (256M /
`GOMEMLIMIT=192MiB`) and `scripts/demo.sh` copies it beside the checkout before
starting the stack. No flag, span name, or service behaviour is changed, and the
override is documented in the file header with the measurement that motivated it.

## Live adapter

`src/opsaccord/tools/live_environment.py` implements the same typed tool surface
against the real stack, through three allow-listed channels and nothing else:

1. Prometheus HTTP API (`/api/v1/query`, `/api/v1/label/__name__/values`);
2. the Jaeger query API for recent spans of one operation;
3. the flagd-ui read/write API for the flag document.

There is no shell, no generic URL fetch, and no Docker API call. Targets are
resolved through a fixed catalog, so a model can never name an arbitrary
container, metric, or operation. `is_simulated` is False on every result, and
live mode fails closed: if the stack does not answer, reads raise and the gateway
records collection-error evidence instead of falling back to simulated numbers.

`src/opsaccord/tools/factory.py` selects the adapter from `OPSACCORD_DEMO_MODE`,
and `scripts/demo.sh` provides `pull|start|stop|status|health|wait|endpoints|urls|host|logs|override`
for lifecycle management limited to this project's containers.
