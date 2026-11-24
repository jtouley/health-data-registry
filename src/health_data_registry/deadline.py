"""One overall deadline for a tool call, shared by every fetch, retry and download it makes.

The hosted server (hosted.py) gives each tool call a budget well under the 240 seconds claude.ai waits for a
result. Without it a restricted source read live could retry for minutes (crawl.RETRIES attempts of up to
crawl.FETCH_TIMEOUT_S each, plus back-off, per page). Inside `call_deadline(seconds)`, `remaining()` says how
much of the budget is left; fetchers cap each attempt by it and stop retrying when it runs out, raising
`DeadlineExceeded`, whose message is plain words for a clinician. Outside a deadline (the CLI, stdio, the
maintainers' refresh) nothing changes.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_DEADLINE: ContextVar[float | None] = ContextVar("hdr_call_deadline", default=None)


class DeadlineExceeded(TimeoutError):
    """The call's time budget ran out; the message is plain words with a next step."""

    code = "too_slow"

    def __init__(self, seconds: float | None = None) -> None:
        budget = f" ({seconds:.0f} seconds)" if seconds else ""
        super().__init__(
            "This question needed more time than the hosted server allows for one answer"
            f"{budget}, so it was stopped and no numbers are shown. The publisher or the data hub may be slow "
            "right now. Try again in a few minutes, or narrow the question: one condition, fewer places or "
            "fewer years."
        )


@contextmanager
def call_deadline(seconds: float) -> Iterator[None]:
    """Run the body with an overall budget of `seconds` (nested deadlines keep the earlier one)."""
    end = time.monotonic() + seconds
    current = _DEADLINE.get()
    token = _DEADLINE.set(end if current is None else min(current, end))
    try:
        yield
    finally:
        _DEADLINE.reset(token)


def remaining() -> float | None:
    """Seconds left in the current call's budget, or None when no deadline is set."""
    end = _DEADLINE.get()
    return None if end is None else end - time.monotonic()


def cap(seconds: float) -> float:
    """`seconds`, shortened to what is left of the budget; raises DeadlineExceeded when nothing is left."""
    left = remaining()
    if left is None:
        return seconds
    if left <= 0:
        raise DeadlineExceeded()
    return min(seconds, left)
