"""Test-only interruption at an original, observed caller's finish entry."""

import dis
import inspect
import sys
from contextlib import contextmanager


@contextmanager
def interrupt_original_finish(caller_class, *, run_id, attempt):
    """Observe original construction, then interrupt exactly that instance once.

    This deliberately raises at a profile event; it does not replace production
    code or describe the failure injection as a read-only observation. Python
    disables profiling when this callback raises; the prior profiler is restored
    on context exit, not continuously during the interrupt's unwind.
    """
    constructor = inspect.unwrap(caller_class.__init__)
    finish = inspect.unwrap(caller_class.finish_review_run)
    constructor_code, constructor_globals = (
        constructor.__code__,
        constructor.__globals__,
    )
    finish_code, finish_globals = finish.__code__, finish.__globals__
    # Profile emits "return" for exceptional exits too. Only an actual original
    # return opcode proves completion; a None event value cannot distinguish it.
    completed_offsets = frozenset(
        instruction.offset
        for instruction in dis.get_instructions(constructor_code)
        if instruction.opname in {"RETURN_VALUE", "RETURN_CONST"}
    )
    previous = sys.getprofile()
    captured = []
    interrupted = []

    def observe(frame, event, value):
        if previous is not None:
            previous(frame, event, value)
        if event == "return" and (
            frame.f_code is constructor_code
            and frame.f_globals is constructor_globals
            and frame.f_lasti in completed_offsets
        ):
            owner = frame.f_locals["self"]
            run = frame.f_locals["run"]
            if (
                type(owner) is caller_class
                and run.run_id == run_id
                and frame.f_locals["attempt"] == attempt
            ):
                if captured:
                    raise AssertionError("duplicate matching original caller")
                captured.append(owner)
        if (
            event == "call"
            and frame.f_code is finish_code
            and frame.f_globals is finish_globals
            and captured
            and frame.f_locals.get("self") is captured[0]
        ):
            if interrupted:
                raise AssertionError("duplicate original finish interruption")
            interrupted.append(True)
            raise KeyboardInterrupt

    sys.setprofile(observe)
    try:
        yield interrupted
    finally:
        sys.setprofile(previous)
