"""Runtime configuration.

Every provider/model identifier is explicit. The coding agent's configuration is
never consulted; the application runtime model is chosen here and nowhere else.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_LOCAL_DATABASE_URL = "postgresql+psycopg://opsaccord@127.0.0.1:55432/opsaccord"
DEFAULT_LOCAL_SCENARIO_URL = "postgresql+psycopg://opsaccord@127.0.0.1:55432/opsaccord_scenarios"
DEFAULT_LOCAL_TEST_URL = "postgresql+psycopg://opsaccord@127.0.0.1:55432/opsaccord_test"


def canonical_json(value: object) -> str:
    """Canonical JSON used for argument and intent digests."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def to_libpq_dsn(url: str) -> str:
    """Convert a SQLAlchemy URL into a DSN that libpq/psycopg can parse.

    LangGraph's Postgres checkpointer takes a libpq connection string, while
    SQLAlchemy needs the driver-qualified form. Keeping both helpers here avoids
    guessing the connection shape at each call site.
    """
    for prefix in ("postgresql+psycopg://", "postgresql+psycopg2://"):
        if url.startswith(prefix):
            return "postgresql://" + url[len(prefix) :]
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OPSACCORD_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # "scenario" is the acceptance-harness environment. It is deliberately a
    # third database: the pytest session rebuilds the schema of the *_test
    # database, and scenario runs must survive that (and vice versa).
    app_env: Literal["dev", "test", "ci", "scenario"] = "dev"
    database_url: str = DEFAULT_LOCAL_DATABASE_URL
    scenario_database_url: str = DEFAULT_LOCAL_SCENARIO_URL
    test_database_url: str = DEFAULT_LOCAL_TEST_URL
    graph_checkpoint_url: str | None = None

    api_host: str = "127.0.0.1"
    api_port: int = 8410
    ui_port: int = 8511
    api_base_url: str = "http://127.0.0.1:8410"

    # Demo environment: "simulated" uses the in-process model of the pinned
    # Astronomy Shop revision; "live" drives the real containerised demo stack
    # through bounded Prometheus/Jaeger/flagd-ui calls. Live mode never falls
    # back to the simulator: an unreachable demo stack fails closed and the
    # failure is recorded as collection-error evidence.
    demo_mode: Literal["simulated", "live"] = "simulated"
    # Pinned upstream revision; provenance and verification steps are recorded in
    # docs/astronomy-shop.md. The flag and service names above come from this tag.
    astronomy_shop_revision: str = "3.1.0"
    metrics_base_url: str = "http://127.0.0.1:19090"
    # Live demo stack endpoints; see docs/astronomy-shop.md for the calibration
    # commands and the observed series/span names.
    #
    # Only two of the pinned revision's host ports are fixed: the frontend proxy
    # (8080) and Prometheus (9090). Everything else -- including Jaeger -- is
    # published on an ephemeral port, so scripts/demo.sh writes the resolved
    # values to .local/demo-endpoints.env and the live commands load them from
    # there rather than guessing.
    #
    # Flags are read and written through the proxy's /feature prefix (Envoy strips
    # it before forwarding to flagd-ui), which keeps the endpoint on the fixed
    # 8080 port. Jaeger is configured with base_path=/jaeger/ui upstream, so the
    # base URL already contains it and the query API is /api/traces.
    prometheus_base_url: str = "http://127.0.0.1:9090"
    jaeger_base_url: str = "http://127.0.0.1:16686/jaeger/ui"
    flag_control_base_url: str = "http://127.0.0.1:8080/feature"

    # Application runtime reasoning model. Explicitly configured; never inherited.
    model_provider: Literal["deterministic", "openai_compatible"] = "deterministic"
    model_id: str = "deterministic-v1"
    model_base_url: str = "https://api.deepseek.com/v1"
    model_api_key_env: str = "DEEPSEEK_API_KEY"
    model_timeout_seconds: float = 45.0
    model_max_output_tokens: int = 1200
    # Cost accounting is unknown unless a rate is configured; unknown is not zero.
    model_input_cost_per_1k_usd: float | None = None
    model_output_cost_per_1k_usd: float | None = None
    allow_paid_model_calls: bool = False

    # Budgets and limits (deterministic, persisted on the workflow row).
    soft_step_budget: int = Field(default=8, ge=1)
    hard_step_cap: int = Field(default=16, ge=1)
    max_budget_extensions: int = Field(default=2, ge=0)
    max_actions_per_incident: int = Field(default=2, ge=1)

    # Bounded read retries and action timeouts.
    read_attempts: int = Field(default=3, ge=1)
    read_deadline_seconds: float = Field(default=2.0, gt=0)
    action_timeout_seconds: float = Field(default=5.0, gt=0)
    approval_ttl_seconds: int = Field(default=900, ge=1)

    # Verification settling. Real telemetry needs a convergence window before a
    # probe taken after a fix can show the fix, so verification may refresh its
    # fresh evidence a bounded number of times. Defaults reproduce the single
    # immediate probe; live runs raise these because their lookback window is a
    # real 300s rate(). Only evidence is refreshed -- never a side effect.
    verification_settle_attempts: int = Field(default=1, ge=1)
    verification_settle_seconds: float = Field(default=0.0, ge=0)
    # Observation window the verification reads ask for. A live stack publishes
    # span metrics on a cadence, so this has to be long enough to contain samples
    # (see docs/astronomy-shop.md, "Live adapter calibration"). The default keeps
    # the simulator's behaviour unchanged.
    verification_window_seconds: int = Field(default=300, ge=30, le=900)

    # Fixed allow-listed local demo targets. Never model-controlled.
    allowed_targets: tuple[str, ...] = (
        "payment-service",
        "ad-service",
        "cart-service",
        "checkout-service",
    )
    allow_listed_remediation_targets: tuple[str, ...] = ("ad-service",)

    @field_validator("hard_step_cap")
    @classmethod
    def _hard_cap_above_soft(cls, value: int, info: object) -> int:
        soft = getattr(info, "data", {}).get("soft_step_budget", 6)
        if value < soft:
            raise ValueError("hard_step_cap must be >= soft_step_budget")
        return value

    @field_validator("prometheus_base_url", "jaeger_base_url", "flag_control_base_url")
    @classmethod
    def _live_endpoints_are_http(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError(f"live demo endpoint must be an http(s) URL, got {value!r}")
        return value.rstrip("/")

    @property
    def state_database_url(self) -> str:
        """The database that holds authoritative state for the selected environment.

        Each environment gets its own database so that a destructive schema reset
        in one can never reach the records of another:

        * ``test``/``ci`` -> the *_test* database, rebuilt by the pytest session;
        * ``scenario``    -> the acceptance-harness database;
        * ``dev``         -> the interactive demo/live database.
        """
        if self.app_env in {"test", "ci"}:
            return self.test_database_url
        if self.app_env == "scenario":
            return self.scenario_database_url
        return self.database_url

    @property
    def checkpoint_url(self) -> str:
        # Graph checkpoints must live beside the state they describe, otherwise a
        # recovery test could resume from a checkpoint written against a
        # different database than the one holding the authoritative rows.
        return self.graph_checkpoint_url or self.state_database_url


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
