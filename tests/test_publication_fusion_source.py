"""Fusion checkpoint custody is original defining source, not wrapper metadata."""

import subprocess
import sys
import unittest


class PublicationFusionSourceTests(unittest.TestCase):
    def test_external_install_and_wrapper_claim_cannot_replace_original(self):
        source = """
from review_sensei import publication_intent_fusion as fusion
from review_sensei.errors import ReviewInputError
class Fake:
    pass
try:
    fusion.install_journal(Fake)
except ReviewInputError:
    pass
else:
    raise AssertionError("external first install accepted")
from review_sensei_github.finding_lifecycle import FindingLifecycleJournal
try:
    fusion.install_journal(FindingLifecycleJournal)
except ReviewInputError:
    pass
else:
    raise AssertionError("installer replay accepted")
wrapper = FindingLifecycleJournal.checkpoint
original = wrapper.__wrapped__
journal = object.__new__(FindingLifecycleJournal)
import inspect
for claimed in (original, object()):
    wrapper.__wrapped__ = claimed
    try:
        fusion._require_checkpoint_source(journal, inspect.currentframe())
    except ReviewInputError:
        pass
    else:
        raise AssertionError("foreign executing frame accepted")
wrapper.__wrapped__ = original
from types import FunctionType
class Stopped(BaseException):
    pass
clone = FunctionType(original.__code__, dict(original.__globals__),
                     original.__name__, original.__defaults__, original.__closure__)
clone.__kwdefaults__ = dict(original.__kwdefaults__ or {})
def trace(frame, event, arg):
    if event == "call" and frame.f_code is original.__code__:
        try:
            fusion._require_checkpoint_source(journal, frame)
        except ReviewInputError:
            raise Stopped()
        raise AssertionError("same code with substituted globals accepted")
    return None
import sys
sys.settrace(trace)
try:
    clone(journal)
except Stopped:
    pass
finally:
    sys.settrace(None)
from review_sensei_github import quarantine_capacity, trust_effects
from review_sensei_github.api_run_requests import ApiRunRequestTransport
assert quarantine_capacity._GUARDED_WRITER_SOURCE[1]["open"] is ApiRunRequestTransport.open
assert trust_effects._source[1] is ApiRunRequestTransport.open
"""
        result = subprocess.run(
            [sys.executable, "-c", source], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
