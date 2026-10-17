"""Sortable, dependency-free identifiers.

ULIDs are used instead of UUID4 so that insert order matches lexical order in
audit and event tables, which makes append-only history easy to reason about.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        value, rem = divmod(value, 32)
        out.append(_CROCKFORD[rem])
    return "".join(reversed(out))


def new_ulid(timestamp_ms: int | None = None) -> str:
    """Return a 26-character Crockford base32 ULID."""
    ts = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
    randomness = int.from_bytes(os.urandom(10), "big")
    return _encode(ts, 10) + _encode(randomness, 16)


def prefixed(prefix: str) -> str:
    """Return a new prefixed identifier such as ``act_01J...``."""
    return f"{prefix}_{new_ulid()}"


def utcnow() -> datetime:
    return datetime.now(UTC)
