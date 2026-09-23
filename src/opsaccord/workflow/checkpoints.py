"""LangGraph checkpoint plumbing.

Checkpoints provide execution continuity (including interrupt/resume). They are
never the only record of an action or an approval: PostgreSQL domain rows are
authoritative, and the controller reconciles the two.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from langgraph.checkpoint.postgres import PostgresSaver

from opsaccord.config import to_libpq_dsn

_setup_lock = threading.Lock()
_setup_done: set[str] = set()


@contextmanager
def checkpoint_saver(url: str) -> Iterator[PostgresSaver]:
    with PostgresSaver.from_conn_string(to_libpq_dsn(url)) as saver:
        yield saver


def ensure_checkpoint_schema(url: str) -> None:
    """Create the checkpoint tables once per process."""
    with _setup_lock:
        if url in _setup_done:
            return
        with checkpoint_saver(url) as saver:
            saver.setup()
        _setup_done.add(url)


def reset_setup_cache() -> None:
    with _setup_lock:
        _setup_done.clear()
