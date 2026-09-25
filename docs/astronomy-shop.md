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
| `paymentUnreachable=on` | Charge error share (120s window) | 0.0 | ~0.33 (marginal 2026-09-24 measurement; not reproduced in every run - see "Dependency attribution") |
| `paymentUnreachable=on` | PlaceOrder error share (90s window) | 0.0 | ~0.75 |
| `paymentUnreachable=on` | Charge call rate | ~0.075-0.13/s | ~0.042/s |
| `adHighCpu=on` | ad CPU in cores (120s window) | ~0.010 | ~1.06 |
| `cartFailure=100%` | EmptyCart | all `UNSET` | ERROR series grows, success series freezes |

The load generator (`LOCUST_USERS=5`, `LOCUST_HEADLESS=false`) is bursty: Charge
traffic is checkout-driven at roughly 4/min and stops for minutes at a time while
GetCart runs at ~1/s. A quiet window is therefore not a symptom, and no live
predicate may treat "no calls" as "faulted" or as "recovered".

### Dependency attribution

`paymentUnreachable` is not a payment-side fault, and the pinned source says
exactly what it does. At tag `3.1.0` (commit `dedc0178`) checkout evaluates the flag
with an empty evaluation context and a `false` default
(`src/checkout/flags/flags_gen.go:73-91`; the manifest is `src/checkout/flags.json`,
and the flag itself is an `off`/`on` boolean with no targeting in
`src/flagd/demo.flagd.json`). When it reads true, `chargeCard` does **not** skip the
call: it swaps the client for one pointed at an unresolvable address and then issues
the same Charge against it (`src/checkout/main.go:573-589`):

```go
func (cs *checkout) chargeCard(ctx context.Context, amount *pb.Money, paymentInfo *pb.CreditCardInfo) (string, error) {
	paymentService := cs.paymentSvcClient
	if flags.PaymentUnreachable.Value(ctx, openfeature.EvaluationContext{}) {
		badAddress := "badAddress:50051"
		c := mustCreateClient(badAddress)
		paymentService = pb.NewPaymentServiceClient(c)
	}

	paymentResp, err := paymentService.Charge(ctx, &pb.ChargeRequest{ ... })
```

`mustCreateClient` is `grpc.NewClient` with `otelgrpc.NewClientHandler()`
(`src/checkout/main.go:473-483`), a lazy client, so the failure surfaces on the RPC
rather than at construction. Source-derived expectations, therefore:

* the caller's own client span for `oteldemo.PaymentService/Charge` is the failing
  operation, and it is what can attribute the fault;
* **no** Charge reaches payment while every evaluation returns the armed variant, so
  payment's own server span carries no error and a successful Charge cannot coexist
  with a redirected one *for the same request*;
* the flag document is not the evaluation: a document that reads `on` does not prove
  the running service evaluated it as true.

Measured behaviour has agreed with that shape in one run and not in another, and
both are recorded rather than averaged:

* `reports/local/live-scenarios-20260924T131320.json` (revision `b4d80e7`): the
  attributed edge read 100.00% of 0.033 calls/s on the caller's own client spans, and
  the payment probe read `NOT_SERVING`, `error_rate` 1.0, because an unscoped
  operation selector was summing the caller's client spans with the callee's server
  spans;
* the 2026-09-24 `233003` run (revision `9058671`): the attributed edge read
  `attributable: true`, `error_rate` 0.0, `calls_per_second` 0.0125 while checkout's
  PlaceOrder spans carried a 17.66% failure share, payment served the Charge path
  normally, and the diagnosis - correctly, on the evidence it had - concluded
  "payment-service is serving normally and the checkout-service to payment-service
  path is carrying successful calls". The post-run capture in
  `.local/user-live-checks-20260924T232852/diagnose-after.log` groups a window that
  lies inside the armed period and shows the same thing: Charge has an `UNSET`
  series at 0.050/s from each side and **no** `STATUS_CODE_ERROR` series, while
  PlaceOrder shows `ERROR` at 0.0109/s on both sides.
* the 2026-09-25 capture (`reports/local/payment-fault-diagnostic-1790297762.json`,
  revision `723fe7b`, user-run, 30 polls over 00:56:03-01:05:48 UTC with
  `paymentUnreachable` armed at 00:56:03 and restored to `off` at 01:05:48): the same
  contradiction, caught in one poll. At 00:58:04 the caller's `PlaceOrder` series read
  0.0500 calls/s with an **error share of 0.3333**, while the attributed Charge series
  read 0.0334 calls/s on `checkout`'s client spans and 0.0167 calls/s on `payment`'s
  server spans - **both at error share 0.0**. Across the whole capture the Charge
  client series never exceeded 0.0334 calls/s and never reported a non-zero error
  share, the caller's share reached 1.0 in the closing polls (0.0167 calls/s, every
  call failing), and not one retained Charge trace carried an error span.

What those two runs do not settle is *why* the 17.66% of PlaceOrder calls failed
without a failing Charge attempt: whether the running service evaluated the flag as
false for them, whether they failed before the Charge call for a reason unrelated to
payment, or whether a failing Charge attempt produced no exported client span. The
`233003` log also carries a four-minute hole in the span-metric series for both
operations, which is recorded as an observation and not explained.

The 2026-09-25 capture narrows that question to a *trace* question and cannot answer
it from the metrics it took: the failures are real and simultaneous with successful
Charge traffic, so the next discriminating fact is which child span the failing
`PlaceOrder` request actually had. See "Trace reads are not time-bounded" for why the
trace read as it stands cannot supply it.

The observation that can attribute this class of fault is the **caller's client
span for the same operation**, which the table above already records as existing:
`oteldemo.PaymentService/Charge` is emitted by both `payment` (server) and
`checkout` (client), and the client side carries `STATUS_CODE_ERROR`. The
allow-listed edge is therefore:

| Caller | Callee | Operation | Client-side signal |
| --- | --- | --- | --- |
| `checkout-service` | `payment-service` | `Charge` | error share of `checkout`'s client spans |

The adapter does not assume the label schema. It reads it from the server
(`/api/v1/series` for `span_name='oteldemo.PaymentService/Charge'`, then
`/api/v1/label/<name>/values`) and resolves which label names the emitting
service (`service_name` or `service`) and which side of the RPC a span is
(`span_kind` or `span_kind_client`, with a value containing the token `client`).
It then compares the caller's own error share against the caller's own call rate.
If the exposed schema cannot supply those two label names, the read reports
`attributable: false` with the reason: an instrumentation gap is reported as a
gap, never as a healthy edge and never as a zero.

Three limits are recorded rather than papered over:

* the **client-side error share for this edge is intermittent on the pinned
  stack**: it has been measured at 100% in one run and at 0.0 in another, with the
  parent-span failure present in both. `scripts/diagnose-payment-fault.py` captures
  both readings side by side, while the flag is armed, so the next Payment decision
  is made from a measurement;
* a scenario's fault premise is established from that attributed caller-side
  reading, never from the caller's generic parent span: `LivePlan.corroborating_dependency`
  names the caller whose own client call must show the fault, and a rising
  `corroborating_target` reading is recorded as context only. A parent span says the
  caller is unhappy, not which dependency is why;
* a window with no Charge calls proves nothing in either direction, and the
  span-metric cadence means any window shorter than 120s reads as "no data"
  (see "Two telemetry pipelines, two cadences"). The **trace** read has a third
  limit, and it is a defect rather than a calibration - see the next section.

### Trace reads are not time-bounded, so their window is whatever Jaeger still holds

`LiveEnvironment.read_traces` (`src/opsaccord/tools/live_environment.py:598-627`)
sends `service`, `operation`, `lookback` and `limit=20`, but no `start`/`end`.
Jaeger's `/api/traces` answers with the newest matching traces, so the reply means
"the 20 most recent traces for this operation", not "the traces in this window":
two polls seconds apart return the *same payload* whenever no new matching trace
completed in between, and the `lookback` only bounds the search, not the result.

The 2026-09-25 capture measures exactly that. Its 30 polls produced only **6 distinct
payloads**, and 12 consecutive polls shared one payload verbatim; every payload had
`error_spans: 0` and every span in every payload was `status: OK`. So a trace reading
here is evidence about *recent* Charge traffic, never about an interval, and it cannot
reconstruct one that has passed.

Retention makes the second half of that worse. Jaeger runs the in-memory store with
`max_traces: ${env:MEMORY_MAX_TRACES}` and compose sets `MEMORY_MAX_TRACES=25000`
(`src/jaeger/config.yml:43`, `compose.observability.yaml:29`), so traces are evicted
**by count, not by age**. Whether an earlier interval still exists is therefore a
function of how much traffic has been through the collector since - and the stored
capture keeps only `name`, `status` and `duration_ms` per span, no trace ID, so that
question is not answerable from the record either.

`scripts/diagnose-payment-trace-path.py` is the read-only exporter for it. It takes an
explicit `--start`/`--end` pair (UTC ISO) or `--lookback-minutes`, reports the window
it could *actually* see rather than the one it was asked for, and classifies each
retained trace as `edge_span_present_and_failing`,
`edge_span_absent_with_a_failure_recorded`, `edge_span_absent_no_failure_recorded`,
`edge_span_present_and_ok` or `no_root_span`. It issues HTTP GETs only, arms no flag,
runs no scenario, and so has nothing to restore.

### Health probes are scoped to one service

An operation name is not a service: `oteldemo.PaymentService/Charge` is exported by
`payment` (`SPAN_KIND_SERVER`) *and* by `checkout` (`SPAN_KIND_CLIENT`), and
`oteldemo.CheckoutService/PlaceOrder` by `checkout` and `frontend`. Measured label
names on the pinned stack include `service_name` and `span_kind`
(`.local/user-live-checks-20260924T232852/diagnose-after.log`), and an unscoped
`sum(rate(...{span_name='<op>'}[Ws]))` therefore sums both sides.

`read_service_health` and `read_metrics(error_rate)` resolve that label from the
exposed schema, cache the answer per operation, and scope the selector to the
target's own spans. The value records `service` and `service_scope` (for example
`service_name='payment'`), and where the server exposes no service label the read
says it is *unscoped* rather than presenting an aggregate as one service. The
semantic consequence, stated because it is a change: a health verdict is now a
statement about the target's own spans only, so it can no longer be decided by a
caller's failures (or credited with a caller's successes). "Can the caller reach it"
is `read_dependency`'s question, and recovery still requires both.

### Calibrated run parameters

| Parameter | Simulated default | Live value | Why |
| --- | --- | --- | --- |
| Health-probe window (payment, ad) | 120s | 120s | shortest span window that always has samples |
| Health-probe window (cart) | 300s | 300s | EmptyCart carries roughly one call per 1-2 minutes |
| Ad `cpu_utilization` read window | 300s | 60s | container metrics are fresh; a five-minute average dilutes a burn that started a minute ago |
| Verification window | 300s | 120s | lets a recovering signal leave the lookback promptly |
| Verification settling | 1 attempt / 0s | 6 rounds x 30s | covers one 120s lookback plus one export interval |
| Step budgets | soft 8 / hard 16 | soft 12 / hard 36 | investigation 6 + model 1 + dispatch 1 + 6 settle rounds x 4 reads = 32, with headroom |

Both budgets stay enforced; the live values are larger because bounded settling
for a real telemetry cadence costs more reads than the simulator needs. They are
set in one place, `opsaccord.scenarios.live.live_settings`, so the CLI, the live
integration tests, and the report describe the same run. The fourth read of a
settle round is the caller-side dependency read; without it a recovery verdict
would rest on the callee's probe alone, which is the blind spot described under
"Dependency attribution".

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
