"""Internal closed failure observations shared by engine and host diagnostics.

This module imports no CLI, provider or hosted package. Observations describe
handled or propagating failures; they do not determine handling, exit, accounting
or authority. Callbacks receive classifications only, never exception objects.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from .errors import ReviewSenseiError

_CLI_FAILURE_OBSERVER: ContextVar[Callable[[str, str | None], None] | None] = (
    ContextVar("cli_failure_observer", default=None)
)


def exception_kind(error: BaseException) -> str:
    """Closed diagnostic vocabulary; never inspect exception messages/notes."""
    return (
        "interrupted"
        if isinstance(error, KeyboardInterrupt)
        else "input_or_protocol"
        if isinstance(error, ReviewSenseiError)
        else "os"
        if isinstance(error, OSError)
        else "value"
        if isinstance(error, ValueError)
        else "system_exit"
        if isinstance(error, SystemExit)
        else "unexpected"
    )


@contextmanager
def observe_cli_failures(
    observer: Callable[[str, str | None], None],
) -> Iterator[None]:
    """Host-owned observation only; no diagnostic value controls business work."""
    token = _CLI_FAILURE_OBSERVER.set(observer)
    try:
        yield
    finally:
        _CLI_FAILURE_OBSERVER.reset(token)


def notify_cli_failure(
    primary: BaseException, cleanup: BaseException | None = None
) -> None:
    try:
        observer = _CLI_FAILURE_OBSERVER.get()
        if observer is not None:
            observer(
                exception_kind(primary),
                None if cleanup is None else exception_kind(cleanup),
            )
    except BaseException:
        pass  # Diagnostic cancellation/failure must not replace the original.
