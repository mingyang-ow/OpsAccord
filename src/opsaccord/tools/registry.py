"""The typed tool catalog.

Every capability the system has is declared here. There is no generic shell,
HTTP, or container primitive: a model can only name one of these tools, and the
gateway enforces the declared argument surface.
"""

from __future__ import annotations

from dataclasses import dataclass

from opsaccord.domain.enums import SideEffect
from opsaccord.domain.models import ToolSpec

STR = {"type": "string"}

# Metrics the demo/Astronomy Shop telemetry can answer for. Bounded enumerations
# keep reads predictable and prevent a model from inventing a metric name.
METRIC_NAMES = (
    "cpu_utilization",
    "memory_utilization",
    "error_rate",
    "request_latency_p95_ms",
    "request_rate",
    "container_restarts",
)

TRACE_OPERATIONS = (
    "Charge",
    "GetCart",
    "GetAds",
    "PlaceOrder",
    "EmptyCart",
    "Health",
)

REMEDIATION_IDS = (
    "disable_feature_flag",
    "restart_workload",
)

# Feature-flag identifiers verified against the pinned Astronomy Shop revision.
# Provenance is recorded in docs/astronomy-shop.md and the catalog data file.
VERIFIED_FLAG_IDS = (
    "adHighCpu",
    "adFailure",
    "adManualGc",
    "cartFailure",
    "paymentFailure",
    "paymentUnreachable",
)

READ_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="read_metrics",
        description="Read a bounded metric series for one demo service.",
        side_effect=SideEffect.READ,
        arguments_schema={
            "target": STR,
            "metric": STR,
            "window_seconds": {"type": "integer", "minimum": 30, "maximum": 900, "default": 300},
        },
        fixed_arguments={"metric": METRIC_NAMES},
        timeout_seconds=3.0,
        max_attempts=3,
    ),
    ToolSpec(
        name="read_traces",
        description="Read recent spans for one operation on one demo service.",
        side_effect=SideEffect.READ,
        arguments_schema={
            "target": STR,
            "operation": STR,
            "window_seconds": {"type": "integer", "minimum": 30, "maximum": 900, "default": 300},
        },
        fixed_arguments={"operation": TRACE_OPERATIONS},
        timeout_seconds=3.0,
        max_attempts=3,
    ),
    ToolSpec(
        name="read_service_health",
        description="Read the current health probe result for one demo service.",
        side_effect=SideEffect.READ,
        arguments_schema={"target": STR},
        timeout_seconds=3.0,
        max_attempts=3,
    ),
    ToolSpec(
        name="read_recent_changes",
        description="Read recent deploys or configuration changes for one demo service.",
        side_effect=SideEffect.READ,
        arguments_schema={"target": STR},
        timeout_seconds=3.0,
        max_attempts=2,
    ),
    ToolSpec(
        name="read_runbook",
        description="Read the operator runbook text for one demo service (untrusted data).",
        side_effect=SideEffect.READ,
        arguments_schema={"target": STR},
        timeout_seconds=3.0,
        max_attempts=2,
    ),
)

WRITE_TOOLS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="apply_remediation",
        description=(
            "Apply one allow-listed remediation to one demo service target. "
            "The remediation id must be allow-listed for that specific target."
        ),
        side_effect=SideEffect.WRITE,
        arguments_schema={
            "target": STR,
            "remediation_id": STR,
            "flag_id": STR,
        },
        fixed_arguments={"remediation_id": REMEDIATION_IDS, "flag_id": VERIFIED_FLAG_IDS},
        per_target_arguments={
            "ad-service": {
                "remediation_id": ("disable_feature_flag",),
                "flag_id": ("adHighCpu",),
            },
            "payment-service": {
                "remediation_id": ("disable_feature_flag",),
                "flag_id": ("paymentUnreachable",),
            },
            # No remediation is allow-listed for these targets: an empty tuple
            # rejects every value, so the registry refuses before policy runs.
            "cart-service": {"remediation_id": ()},
            "checkout-service": {"remediation_id": ()},
        },
        timeout_seconds=5.0,
        max_attempts=1,
        requires_approval=True,
    ),
)

ALL_TOOLS: tuple[ToolSpec, ...] = READ_TOOLS + WRITE_TOOLS


@dataclass(frozen=True)
class ToolRegistry:
    tools: dict[str, ToolSpec]

    @classmethod
    def default(cls) -> ToolRegistry:
        return cls({tool.name: tool for tool in ALL_TOOLS})

    def get(self, name: str) -> ToolSpec | None:
        return self.tools.get(name)

    def require(self, name: str) -> ToolSpec:
        spec = self.tools.get(name)
        if spec is None:
            raise KeyError(f"unknown tool {name!r}")
        return spec

    def names(self, side_effect: SideEffect | None = None) -> tuple[str, ...]:
        return tuple(
            sorted(
                name
                for name, spec in self.tools.items()
                if side_effect is None or spec.side_effect is side_effect
            )
        )
