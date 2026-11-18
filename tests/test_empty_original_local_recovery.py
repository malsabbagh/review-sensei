"""Local retained decoding never acquires hosted omission custody."""

import inspect
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from review_sensei_github import session_ledger
from review_sensei_github.empty_original_admission import consume
from review_sensei_github.publication_recovery import (
    recover_retained_review_result,
    retain_checkpoint_review_result,
)

from review_sensei.session import LocalSessionLedger
from tests import test_publication_recovery as fixture


class EmptyOriginalLocalRecoveryTests(unittest.TestCase):
    def test_original_local_decode_preserves_result_and_declines_hosted_receipt(self):
        for count, lookalike, unrelated_mutation in (
            (0, False, False),
            (1, False, False),
            (0, True, False),
            (0, False, True),
        ):
            with (
                self.subTest(
                    count=count,
                    lookalike=lookalike,
                    unrelated_mutation=unrelated_mutation,
                ),
                tempfile.TemporaryDirectory() as directory,
            ):
                ledger = LocalSessionLedger(Path(directory))
                result, _key = fixture.checkpoint(ledger, count=count)
                state = fixture.RetentionState()
                retained = retain_checkpoint_review_result(
                    http=state.http,
                    token="fixture",
                    result=result,
                    identity=fixture.IDENTITY,
                    base_sha=fixture.BASE,
                    head_sha=fixture.HEAD,
                    app_slug=fixture.APP,
                    pending_transaction=result.transaction,
                    configuration_context=fixture.CONFIGURATION,
                    evidence_context=fixture.EVIDENCE,
                    policy=fixture.POLICY,
                )
                before = ledger.load(fixture.IDENTITY).record
                fresh = LocalSessionLedger(Path(directory))
                if lookalike:
                    # Field resemblance is not original hosted ledger custody.
                    fresh.http = state.http
                    fresh.caller = SimpleNamespace(_checkpoint=None, _root=None)
                receipts = []
                wrapper_code = recover_retained_review_result.__code__
                self.assertIsNot(
                    wrapper_code,
                    inspect.unwrap(recover_retained_review_result).__code__,
                )

                def observe(frame, event, value):
                    if frame.f_code is wrapper_code and event == "return":
                        receipts.append(id(value) in frame.f_locals["cold_reads"])

                state.calls.clear()
                previous = sys.getprofile()
                try:
                    sys.setprofile(observe)
                    mutation = (
                        patch.object(session_ledger, "render_session_comment", object())
                        if unrelated_mutation
                        else nullcontext()
                    )
                    with mutation:
                        restored = fixture.recover(
                            fixture.application(state, fresh), fresh
                        )
                finally:
                    sys.setprofile(previous)
                self.assertEqual(restored.to_dict(), retained.to_dict())
                self.assertEqual(fresh.load(fixture.IDENTITY).record, before)
                self.assertEqual(receipts, [False])
                self.assertTrue(state.calls)
                self.assertTrue(all(call[0] == "GET" for call in state.calls))
                self.assertFalse(consume(object(), fresh, before, restored))
