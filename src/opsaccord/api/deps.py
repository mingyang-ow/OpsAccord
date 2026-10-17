"""Local-development actor authentication.

This is an explicitly documented local mechanism, not production auth: the API
binds to loopback and callers identify themselves with headers. Human-only
endpoints require the ``human`` role, and the backend (never the UI) enforces it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from opsaccord.domain.enums import ActorType

_ROLE_MAP = {"human": ActorType.HUMAN, "agent": ActorType.AGENT, "system": ActorType.SYSTEM}


@dataclass(frozen=True)
class Actor:
    name: str
    type: ActorType

    @property
    def is_human(self) -> bool:
        return self.type is ActorType.HUMAN


def current_actor(
    x_opsaccord_actor: Annotated[str | None, Header(alias="X-OpsAccord-Actor")] = None,
    x_opsaccord_role: Annotated[str | None, Header(alias="X-OpsAccord-Role")] = None,
) -> Actor:
    if not x_opsaccord_actor or not x_opsaccord_role:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-OpsAccord-Actor and X-OpsAccord-Role headers are required",
        )
    actor_type = _ROLE_MAP.get(x_opsaccord_role.lower())
    if actor_type is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown role {x_opsaccord_role!r}; expected human, agent, or system",
        )
    return Actor(name=x_opsaccord_actor, type=actor_type)


def require_human(actor: Annotated[Actor, Depends(current_actor)]) -> Actor:
    if not actor.is_human:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="this endpoint requires a human-authenticated actor; the agent cannot act here",
        )
    return actor
