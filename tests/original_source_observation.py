"""Test-only observations and fixture faults at unchanged original source frames."""

import dis
import inspect
import sys
from contextlib import contextmanager


@contextmanager
def observe_original(function, *, entered=None, returned=None):
    """Keep production definitions intact; invoke callbacks at exact source events.

    A return callback requires an actual return opcode, excluding exceptional
    profile-return events. Callbacks may inspect test state or inject a fixture
    fault; they do not replace a production result or grant source authority.
    """
    original = inspect.unwrap(function)
    code, namespace = original.__code__, original.__globals__
    returns = frozenset(
        instruction.offset
        for instruction in dis.get_instructions(code)
        if instruction.opname in {"RETURN_VALUE", "RETURN_CONST"}
    )
    previous = sys.getprofile()

    def observe(frame, event, value):
        if previous is not None:
            previous(frame, event, value)
        if frame.f_code is not code or frame.f_globals is not namespace:
            return
        if event == "call" and entered is not None:
            entered(frame)
        elif event == "return" and frame.f_lasti in returns and returned is not None:
            returned(frame, value)

    try:
        sys.setprofile(observe)
        yield
    finally:
        sys.setprofile(previous)
