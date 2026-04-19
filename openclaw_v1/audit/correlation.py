"""
Correlation IDs. Inline ULID-like generator (no external dep): 48-bit ms
timestamp + 80-bit randomness, Crockford's Base32 encoded → 26 chars.

An asyncio ContextVar carries the "current" correlation ID so any helper
deep in a call stack can stamp records without threading the ID through
every function signature.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import time
from contextvars import ContextVar
from typing import Iterator, Optional

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford's Base32
_current: ContextVar[Optional[str]] = ContextVar("openclaw_correlation_id", default=None)


def _b32(n: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(_ALPHABET[n & 0x1F])
        n >>= 5
    return "".join(reversed(out))


def new_correlation_id() -> str:
    """Generate a fresh 26-char ULID-ish correlation ID."""
    ts_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rand = int.from_bytes(secrets.token_bytes(10), "big")  # 80 bits
    return _b32(ts_ms, 10) + _b32(rand, 16)


def current_correlation_id() -> Optional[str]:
    """Return the correlation ID scoped to the current task, if any."""
    return _current.get()


@contextlib.contextmanager
def with_correlation(cid: Optional[str] = None) -> Iterator[str]:
    """
    Bind a correlation ID to the surrounding block. Nested usage stacks
    correctly via ContextVar tokens.

        with with_correlation() as cid:
            ledger.record(kind="intent", ...)   # cid is auto-included
    """
    cid = cid or new_correlation_id()
    token = _current.set(cid)
    try:
        yield cid
    finally:
        _current.reset(token)


# Convenience for scripts that want correlation IDs without a context
# manager (e.g. a scheduler handler's top-level entry).
def bind_correlation_id(cid: Optional[str] = None) -> str:
    cid = cid or new_correlation_id()
    _current.set(cid)
    return cid


# Override for tests that need deterministic IDs without a ContextVar.
def force_correlation_id_for_tests(cid: str) -> None:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        _current.set(cid)
