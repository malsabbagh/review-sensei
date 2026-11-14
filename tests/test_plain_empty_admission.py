"""Original empty admission needs positive current-invocation source custody."""

import json
import sys
import tempfile
import unittest
from dataclasses import replace
from types import SimpleNamespace

from review_sensei_github import accepted_result
from review_sensei_github.empty_original_admission import consume
from review_sensei_github.publication_recovery import _publication_transport

from review_sensei.bounded_evidence import canonical_bytes
from review_sensei.models import ReviewComment, ReviewResult
from tests.test_hosted_history_readers import CurrentLargeEntryFixture


class PlainEmptyAdmissionTests(unittest.TestCase):
    def test_absent_forged_substituted_and_later_root_sources_cannot_omit(self):
        class Observed(BaseException):
            pass

        for case in (
            "valid",
            "absent",
            "forged",
            "result",
            "nonempty",
            "root",
            "other_run",
            "outside",
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                fixture = CurrentLargeEntryFixture.create(directory)
                values = []

                def observe(frame, event, value):
                    if frame.f_code is not consume.__code__:
                        return None
                    if event == "call":
                        ledger = frame.f_locals["ledger"]
                        result = frame.f_locals["result"]
                        key = _publication_transport(ledger.http)
                        if case == "absent":
                            accepted_result._CAPTURED.pop(key, None)
                        elif case == "forged":
                            accepted_result._CAPTURED[key] = SimpleNamespace()
                        elif case == "result":
                            source = accepted_result._CAPTURED[key]
                            accepted_result._CAPTURED[key] = replace(
                                source, payload=b'{"schema_version":"invalid"}'
                            )
                        elif case == "nonempty":
                            source = accepted_result._CAPTURED[key]
                            original = ReviewResult.from_dict(
                                json.loads(source.payload)
                            )
                            nonempty = replace(
                                original,
                                comments=(ReviewComment("src/app.py", 2, "Retain me"),),
                            )
                            accepted_result._CAPTURED[key] = replace(
                                source, payload=canonical_bytes(nonempty.to_dict())
                            )
                        elif case == "root":
                            ledger.caller._root = "f" * 64
                        elif case == "other_run":
                            source = accepted_result._CAPTURED[key]
                            reference = source.reference
                            reference["run_id"] = "987654"
                            accepted_result._CAPTURED[key] = replace(
                                source, reference_bytes=canonical_bytes(reference)
                            )
                        elif case == "outside":
                            # The original selection is not permission for a
                            # later invocation even with the same arguments.
                            self.assertFalse(
                                consume(
                                    object(), ledger, frame.f_locals["record"], result
                                )
                            )
                    if event == "return":
                        values.append(value)
                        raise Observed()
                    return observe

                previous = sys.gettrace()
                try:
                    sys.settrace(observe)
                    with self.assertRaises(Observed):
                        fixture.invoke()
                finally:
                    sys.settrace(previous)
                self.assertEqual(values, [case in {"valid", "outside"}])
                self.assertEqual(fixture.reviews, [])
                # The diagnostic abort may write its existing nonpassing check;
                # it must never yield a review or a successful completion.
                self.assertFalse(
                    any(c.get("conclusion") == "success" for c in fixture.check_runs)
                )

    def test_substituted_model_producers_are_never_invoked(self):
        from unittest.mock import patch

        class Observed(BaseException):
            pass

        for method in ("from_dict", "content_digest"):
            with (
                self.subTest(method=method),
                tempfile.TemporaryDirectory() as directory,
            ):
                fixture = CurrentLargeEntryFixture.create(directory)
                values, calls, patches = [], [], []

                def substituted(*args, **kwargs):
                    calls.append(method)
                    raise AssertionError("substituted model producer executed")

                def observe(frame, event, value):
                    if frame.f_code is not consume.__code__:
                        return None
                    if event == "call":
                        replacement = patch.object(ReviewResult, method, substituted)
                        replacement.start()
                        patches.append(replacement)
                    if event == "return":
                        values.append(value)
                        while patches:
                            patches.pop().stop()
                        raise Observed()
                    return observe

                previous = sys.gettrace()
                try:
                    sys.settrace(observe)
                    with self.assertRaises(Observed):
                        fixture.invoke()
                finally:
                    sys.settrace(previous)
                    while patches:
                        patches.pop().stop()
                self.assertEqual(values, [False])
                self.assertEqual(calls, [])
                self.assertEqual(fixture.reviews, [])
