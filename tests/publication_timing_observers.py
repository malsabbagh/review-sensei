"""Passive original-source timing for the genuinely empty publication fixture."""

import inspect
import sys
from contextlib import contextmanager

from review_sensei_github.publication import ReviewPublisher

from review_sensei.models import ReviewResult


@contextmanager
def at_empty_original_publication_fence(callback):
    """Change the synthetic external PR at the original final pre-write fence.

    An empty original no longer stages a redundant per-comment artifact. Bind
    the actual source frame after complete marker reconciliation instead of
    waiting for that omitted artifact or counting arbitrary preflight calls.
    No publisher, preflight, source seal or result is replaced.
    """
    preflight = inspect.unwrap(ReviewPublisher._preflight_pr)
    publish = inspect.unwrap(ReviewPublisher._publish_impl)
    previous = sys.getprofile()
    fired = []

    def observe(frame, event, arg):
        parent = frame.f_back
        if (
            not fired
            and event == "call"
            and frame.f_code is preflight.__code__
            and frame.f_globals is preflight.__globals__
            and parent is not None
            and parent.f_code is publish.__code__
            and parent.f_globals is publish.__globals__
            and frame.f_locals.get("self") is parent.f_locals.get("self")
            and "identity_states" in parent.f_locals
            and parent.f_locals.get("stage_human_inventory") is False
        ):
            original = parent.f_locals.get("original_result")
            if type(original) is ReviewResult and not original.comments:
                fired.append(True)
                callback()
        if previous is not None:
            previous(frame, event, arg)

    try:
        sys.setprofile(observe)
        yield fired
    finally:
        sys.setprofile(previous)
