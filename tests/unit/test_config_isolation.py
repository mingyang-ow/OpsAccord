"""Configuration stays overridable, and a defaults test still means something.

The offline suite failed once for a reason that had nothing to do with the code:
``scripts/demo.sh endpoints`` writes ``OPSACCORD_DEMO_MODE=live`` into
``.local/demo-endpoints.env``, the documented live command exports it with
``set -a; . .local/demo-endpoints.env``, and the export outlives the run. A test
that asserted a default then observed the developer's shell instead.

These tests pin both halves of the correction, so neither is bought with the
other:

* the override mechanism still works - the process environment wins, a ``.env``
  file is read, and live mode selects the live adapter with no fallback - so
  nothing here weakens runtime configuration;
* a test that means "this is the default" can ask for an isolated configuration
  context and get one, whatever the parent shell exports.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import LIVE_SESSION_VARS, scrub_configuration_environment
from opsaccord.config import REPO_ROOT, Settings, get_settings
from opsaccord.tools.demo_environment import DemoEnvironment
from opsaccord.tools.factory import build_environment
from opsaccord.tools.live_environment import LiveEnvironment


def test_defaults_hold_in_an_isolated_configuration_context(isolated_config: Path) -> None:
    """The isolated context sees defaults, not this developer's shell."""
    settings = Settings()

    assert settings.demo_mode == "simulated"
    assert settings.app_env == "dev"
    assert settings.model_provider == "deterministic"
    assert settings.allow_paid_model_calls is False
    assert settings.graph_checkpoint_url is None
    assert settings.soft_step_budget == 8
    assert settings.hard_step_cap == 16


def test_an_explicit_environment_override_selects_live_mode(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The override mechanism is untouched: an exported setting is honoured."""
    monkeypatch.setenv("OPSACCORD_DEMO_MODE", "live")
    monkeypatch.setenv("OPSACCORD_LIVE_DEMO", "1")

    settings = Settings()

    assert settings.demo_mode == "live"
    environment = build_environment(settings)
    assert isinstance(environment, LiveEnvironment)
    assert environment.is_simulated is False


def test_a_temporary_dotenv_is_read_and_the_process_environment_wins(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``.env`` is a real override source, one step below the environment."""
    (isolated_config / ".env").write_text(
        "OPSACCORD_DEMO_MODE=live\nOPSACCORD_SOFT_STEP_BUDGET=5\n", encoding="utf-8"
    )

    from_file = Settings()
    assert from_file.demo_mode == "live"
    assert from_file.soft_step_budget == 5

    # The process environment outranks the file, which is the documented order.
    monkeypatch.setenv("OPSACCORD_DEMO_MODE", "simulated")
    assert Settings().demo_mode == "simulated"
    assert Settings().soft_step_budget == 5


def test_live_mode_never_resolves_to_the_simulator(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each mode has exactly one adapter, and live evidence cannot be simulated."""
    monkeypatch.setenv("OPSACCORD_DEMO_MODE", "live")
    live = build_environment(Settings())
    assert isinstance(live, LiveEnvironment)
    assert not isinstance(live, DemoEnvironment)
    assert live.is_simulated is False

    monkeypatch.setenv("OPSACCORD_DEMO_MODE", "simulated")
    simulated = build_environment(Settings())
    assert isinstance(simulated, DemoEnvironment)
    assert simulated.is_simulated is True

    # Constructing the adapter directly cannot be fooled either.
    assert Settings(demo_mode="live").demo_mode == "live"
    assert isinstance(build_environment(Settings(demo_mode="live")), LiveEnvironment)


OVERRIDE_VALUES = {
    "demo_mode": "live",
    "model_provider": "openai_compatible",
    "allow_paid_model_calls": "true",
    "graph_checkpoint_url": "postgresql://inherited/checkpoints",
}


@pytest.mark.parametrize("style", ["upper", "lower"])
def test_the_default_tests_pass_while_the_shell_exports_live_mode(style: str) -> None:
    """The user's failure, as a test: the whole file must pass with overrides inherited.

    This runs a real pytest subprocess because the property under test is about a
    *process environment*: no fixture can prove that the module is independent of
    the shell it is launched from while running inside that same shell.

    Both spellings are exercised because ``Settings`` reads environment names
    case-insensitively, so ``opsaccord_demo_mode=live`` leaks exactly like
    ``OPSACCORD_DEMO_MODE=live``.
    """
    inherited = {
        key: value for key, value in os.environ.items() if not key.startswith(("PYTEST_", "COV_"))
    }
    inherited.update(
        {
            (f"OPSACCORD_{name.upper()}" if style == "upper" else f"opsaccord_{name}"): value
            for name, value in OVERRIDE_VALUES.items()
        }
    )

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "tests/unit/test_domain_contracts.py"],
        cwd=REPO_ROOT,
        env=inherited,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )

    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"


def test_the_scrub_removes_case_variants_and_clears_the_cached_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A case variant is a real leak, and the settings cache outlives the variables.

    Two holes were found by review in the first version of the scrub: it compared
    names case-sensitively while ``Settings`` does not, and it cleared
    ``os.environ`` but not ``get_settings``'s cache, so a value read from the
    leaked shell would still be returned afterwards. Both are pinned here.
    """
    monkeypatch.setenv("opsaccord_demo_mode", "live")
    monkeypatch.setenv("Opsaccord_Soft_Step_Budget", "5")
    assert get_settings().demo_mode == "live"  # cached from the leaked environment
    assert get_settings().soft_step_budget == 5

    scrub_configuration_environment(monkeypatch)

    assert "opsaccord_demo_mode" not in os.environ
    assert "Opsaccord_Soft_Step_Budget" not in os.environ
    assert Settings().demo_mode == "simulated"
    assert Settings().soft_step_budget == 8
    # The cached settings must not survive the scrub.
    assert get_settings().demo_mode == "simulated"
    assert get_settings().soft_step_budget == 8


def test_the_live_session_variable_list_covers_what_demo_sh_exports() -> None:
    """The documented leak list cannot drift from the script that creates it.

    The pattern allows leading whitespace, an ``export`` prefix and either case,
    so the guard holds for the spellings ``scripts/demo.sh`` could plausibly grow
    rather than only for the one it uses today.
    """
    script = (REPO_ROOT / "scripts" / "demo.sh").read_text(encoding="utf-8")
    exported = {
        match.group(1).upper()
        for match in re.finditer(
            r"^\s*(?:export\s+)?(OPSACCORD_[A-Za-z0-9_]+)=", script, re.MULTILINE
        )
    }

    assert exported, "scripts/demo.sh is expected to export OPSACCORD_* endpoints"
    assert exported <= set(LIVE_SESSION_VARS), (
        "scripts/demo.sh exports settings that the live-session list does not cover: "
        f"{sorted(exported - set(LIVE_SESSION_VARS))}"
    )
