"""Select the demo execution surface from explicit configuration.

The selection is deliberately boring and total: ``simulated`` builds the
in-process model of the pinned revision, ``live`` builds the real-stack adapter.
There is no third path and no fallback. If live mode cannot reach the demo
stack, its reads fail and the gateway records collection-error evidence, so an
operator can never be shown simulated numbers that look live.
"""

from __future__ import annotations

from opsaccord.config import Settings
from opsaccord.tools.contracts import Environment
from opsaccord.tools.demo_environment import build_demo_environment
from opsaccord.tools.live_environment import LiveEnvironment


def build_environment(settings: Settings) -> Environment:
    if settings.demo_mode == "live":
        return LiveEnvironment(settings=settings)
    return build_demo_environment()


__all__ = ["build_environment"]
