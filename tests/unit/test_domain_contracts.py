"""Ids, digests, configuration, and typed model contracts."""

from __future__ import annotations

import pytest

from opsaccord.config import Settings, canonical_json, digest, to_libpq_dsn
from opsaccord.domain.enums import IncidentStatus, transition_allowed
from opsaccord.domain.models import Action, ActionProposal, DiagnosisProposal
from opsaccord.ids import new_ulid, prefixed, utcnow


def test_ids_are_unique_sortable_and_prefixed() -> None:
    identifiers = {new_ulid() for _ in range(200)}
    assert len(identifiers) == 200
    assert all(len(identifier) == 26 for identifier in identifiers)
    assert prefixed("inc").startswith("inc_")
    assert prefixed("inc") != prefixed("inc")


def test_ulids_sort_by_creation_time() -> None:
    assert new_ulid(1_700_000_000_000) < new_ulid(1_700_000_001_000)


def test_utcnow_is_timezone_aware() -> None:
    assert utcnow().tzinfo is not None


def test_intent_digest_is_independent_of_the_action_id() -> None:
    arguments = {"target": "payment-service", "flag_id": "paymentUnreachable"}
    first = Action.compute_intent_digest("apply_remediation", "payment-service", arguments)
    second = Action.compute_intent_digest("apply_remediation", "payment-service", arguments)
    reordered = Action.compute_intent_digest(
        "apply_remediation",
        "payment-service",
        {"flag_id": "paymentUnreachable", "target": "payment-service"},
    )
    other_target = Action.compute_intent_digest("apply_remediation", "ad-service", arguments)
    assert first == second == reordered
    assert first != other_target


def test_canonical_json_and_digest_are_stable() -> None:
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    assert digest({"a": 2, "b": 1}) == digest({"b": 1, "a": 2})


def test_sqlalchemy_urls_convert_to_libpq_dsns() -> None:
    assert (
        to_libpq_dsn("postgresql+psycopg://user@127.0.0.1:55432/db")
        == "postgresql://user@127.0.0.1:55432/db"
    )
    assert to_libpq_dsn("postgresql://user@host/db") == "postgresql://user@host/db"


def test_test_environment_keeps_state_and_checkpoints_in_one_database() -> None:
    settings = Settings(app_env="test")
    assert settings.state_database_url == settings.test_database_url
    assert settings.checkpoint_url == settings.test_database_url

    overridden = Settings(app_env="test", graph_checkpoint_url="postgresql://other/checkpoints")
    assert overridden.checkpoint_url == "postgresql://other/checkpoints"


def test_application_runtime_model_is_explicit_and_not_inherited() -> None:
    settings = Settings()
    assert settings.model_provider == "deterministic"
    assert settings.allow_paid_model_calls is False
    assert settings.model_input_cost_per_1k_usd is None


def test_demo_mode_selects_the_adapter_and_never_falls_back() -> None:
    # The simulated surface stays the default, and each mode has exactly one
    # adapter: live never resolves to the in-process simulator, so live evidence
    # can never be produced by the simulated model.
    from opsaccord.tools.demo_environment import DemoEnvironment
    from opsaccord.tools.factory import build_environment
    from opsaccord.tools.live_environment import LiveEnvironment

    assert Settings().demo_mode == "simulated"
    assert isinstance(build_environment(Settings(demo_mode="simulated")), DemoEnvironment)
    assert isinstance(build_environment(Settings(demo_mode="live")), LiveEnvironment)


def test_live_endpoints_must_be_http_urls() -> None:
    with pytest.raises(ValueError, match="http"):
        Settings(prometheus_base_url="127.0.0.1:9090")


def test_proposal_models_reject_empty_or_oversized_rationale() -> None:
    with pytest.raises(ValueError):
        ActionProposal(tool_name="apply_remediation", target="ad-service", rationale="")
    with pytest.raises(ValueError):
        DiagnosisProposal(summary="ok", confidence=1.4)


def test_transition_map_is_explicit() -> None:
    assert transition_allowed(IncidentStatus.NEW, IncidentStatus.INVESTIGATING)
    assert not transition_allowed(IncidentStatus.RESOLVED, IncidentStatus.INVESTIGATING)
    assert not transition_allowed(IncidentStatus.NEW, IncidentStatus.RESOLVED)
    assert not transition_allowed(IncidentStatus.READY_FOR_HUMAN, IncidentStatus.RESOLVED)
