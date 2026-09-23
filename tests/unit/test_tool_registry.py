"""The typed catalog is the executable surface: if it is not declared here, no
component in the system can perform it."""

from __future__ import annotations

import pytest

from opsaccord.domain.enums import SideEffect
from opsaccord.tools.registry import VERIFIED_FLAG_IDS, ToolRegistry


@pytest.fixture
def registry() -> ToolRegistry:
    return ToolRegistry.default()


def test_every_declared_tool_is_typed_and_has_no_shell_or_url_primitive(
    registry: ToolRegistry,
) -> None:
    assert set(registry.names()) == {
        "read_metrics",
        "read_traces",
        "read_service_health",
        "read_recent_changes",
        "read_runbook",
        "apply_remediation",
    }
    assert registry.require("apply_remediation").side_effect is SideEffect.WRITE
    for name in ("shell_exec", "http_get", "docker_restart"):
        assert registry.get(name) is None


def test_unknown_arguments_are_rejected(registry: ToolRegistry) -> None:
    spec = registry.require("apply_remediation")
    with pytest.raises(ValueError, match="unsupported arguments"):
        spec.validate_arguments(
            {
                "target": "ad-service",
                "remediation_id": "disable_feature_flag",
                "flag_id": "adHighCpu",
                "command": "rm -rf /",
            }
        )


def test_metric_names_are_a_closed_set(registry: ToolRegistry) -> None:
    spec = registry.require("read_metrics")
    with pytest.raises(ValueError, match="not allow-listed"):
        spec.validate_arguments(
            {"target": "ad-service", "metric": "arbitrary_metric", "window_seconds": 300}
        )


def test_window_seconds_is_bounded(registry: ToolRegistry) -> None:
    spec = registry.require("read_metrics")
    with pytest.raises(ValueError, match="above maximum"):
        spec.validate_arguments(
            {"target": "ad-service", "metric": "cpu_utilization", "window_seconds": 10_000}
        )


def test_ad_remediation_accepts_only_the_verified_flag(registry: ToolRegistry) -> None:
    spec = registry.require("apply_remediation")
    resolved = spec.validate_arguments(
        {
            "target": "ad-service",
            "remediation_id": "disable_feature_flag",
            "flag_id": "adHighCpu",
        }
    )
    assert resolved["flag_id"] == "adHighCpu"
    with pytest.raises(ValueError, match="not allow-listed for target"):
        spec.validate_arguments(
            {
                "target": "ad-service",
                "remediation_id": "disable_feature_flag",
                "flag_id": "paymentUnreachable",
            }
        )


def test_targets_without_a_supported_remediation_are_refused_before_policy(
    registry: ToolRegistry,
) -> None:
    spec = registry.require("apply_remediation")
    for target in ("cart-service", "checkout-service"):
        with pytest.raises(ValueError, match="not allow-listed for target"):
            spec.validate_arguments(
                {
                    "target": target,
                    "remediation_id": "disable_feature_flag",
                    "flag_id": "cartFailure",
                }
            )


def test_flag_identifiers_are_pinned_to_the_upstream_revision() -> None:
    assert "adHighCpu" in VERIFIED_FLAG_IDS
    assert "paymentUnreachable" in VERIFIED_FLAG_IDS
    assert "cartFailure" in VERIFIED_FLAG_IDS
