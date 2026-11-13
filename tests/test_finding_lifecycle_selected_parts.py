"""Selected owned input saves one redundant list, never a final authority fence."""

import copy
import hashlib
import tempfile
import unittest
from unittest.mock import PropertyMock, patch

from review_sensei_github.api_run_requests import ApiRunSessionLedger
from review_sensei_github.session_ledger import parse_session_comment
from test_action_run import CommandEntryFixture
from test_api_work_persistence import IDENTITY


class SelectedPartsTests(unittest.TestCase):
    def producer(self, directory, *, limit=1024):
        f = CommandEntryFixture(directory, limit=limit)
        f.provider_comments = [
            dict(
                path="src/app.py",
                line=2,
                side="RIGHT",
                body=f"Confirm interface assumption {i}. "
                + "".join(
                    hashlib.sha256(f"body:{i}:{j}".encode()).hexdigest()
                    for j in range(15)
                ),
                severity="high",
                blocking=False,
                needs_human=True,
            )
            for i in range(20)
        ]
        external = f.external

        def long_native_ids(request, timeout):
            response = external(request, timeout)
            if (
                request.get_method() == "POST"
                and "/pulls/7/reviews" in request.full_url
            ):
                for index, thread in enumerate(f.threads):
                    thread["id"] = (
                        f"R{index:03d}"
                        + "".join(
                            chr(256 + b)
                            for j in range(4)
                            for b in hashlib.sha256(f"{index}:{j}".encode()).digest()
                        )[:126]
                    )
            return response

        f.external = long_native_ids
        return f

    def test_actual_two_part_producer_keeps_original_operation_and_funding(self):
        original = ApiRunSessionLedger.replace_finding_lifecycle_parts
        witnessed = []

        def run(ledger, identity, mutate, **kwargs):
            before = len(ledger.caller.observations)
            result = original(ledger, identity, mutate, **kwargs)
            witnessed.append(kwargs["parts"])
            self.assertLessEqual(len(ledger.caller.observations) - before, 10)
            return result

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(ApiRunSessionLedger, "replace_finding_lifecycle_parts", run),
        ):
            f = self.producer(directory)
            self.assertEqual(f.invoke(), 0, f.error)
            self.assertIn(
                2,
                witnessed,
                (len(f.threads), [len(n["id"].encode()) for n in f.threads]),
            )
            self.assertFalse(any(r["state"] == "APPROVED" for r in f.reviews))
            self.assertEqual(
                parse_session_comment(
                    f.comment["body"], identity=IDENTITY
                ).api_run_accounting["configuration"]["limit"],
                1024,
            )

    def test_copied_or_mutated_owned_input_cannot_stage_parts(self):
        original = ApiRunSessionLedger.replace_finding_lifecycle_parts
        for fault in ("clone", "mutated"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                f = self.producer(directory)
                caught = []

                def run(ledger, identity, mutate, **kwargs):
                    before = copy.deepcopy(f.extra_comments)
                    if fault == "clone":
                        kwargs["record"] = copy.deepcopy(kwargs["record"])
                    else:
                        owned = ledger.caller._effect_record
                        owned.api_run_accounting["configuration"]["limit"] += 1
                    try:
                        return original(ledger, identity, mutate, **kwargs)
                    finally:
                        caught.append(True)
                        self.assertEqual(f.extra_comments, before)

                with patch.object(
                    ApiRunSessionLedger, "replace_finding_lifecycle_parts", run
                ):
                    self.assertNotEqual(f.invoke(), 0)
                self.assertTrue(caught)

    def test_selected_capability_is_one_use_and_cannot_nest(self):
        from review_sensei.errors import ReviewInputError

        consume = ApiRunSessionLedger._consume_plain_finding_input
        caught = []

        def observed(ledger, identity):
            result = consume(ledger, identity)
            if result is not None:
                caught.append(True)
                with self.assertRaisesRegex(ReviewInputError, "already consumed"):
                    consume(ledger, identity)
                with self.assertRaises(ReviewInputError):
                    ledger.replace_finding_lifecycle_parts(
                        identity,
                        lambda current: current,
                        record=result[1],
                        parts=1,
                        retained_bytes=100,
                    )
            return result

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(ApiRunSessionLedger, "_consume_plain_finding_input", observed),
        ):
            f = self.producer(directory)
            self.assertEqual(f.invoke(), 0, f.error)
            self.assertTrue(caught)

    def test_renderer_bound_covers_unicode_quotes_and_validated_grants(self):
        from types import SimpleNamespace

        from review_sensei_github.session_ledger import (
            plain_session_comment_json_bound,
            render_session_comment,
        )

        from review_sensei.bounded_evidence import canonical_bytes
        from review_sensei.errors import ReviewInputError

        record = SimpleNamespace(
            to_dict=lambda: {"text": ('審査 "+\\"' * 300)},
            quarantine_lineage=None,
            generation=1,
            record_sha256="a" * 64,
        )
        body = render_session_comment(
            repository_id=123,
            pull_request=7,
            record=record,
            consumed_grants=("a" * 64,),
        )
        self.assertTrue(body.isascii())
        self.assertLessEqual(
            len(canonical_bytes(body)),
            plain_session_comment_json_bound(consumed_grants=("a" * 64,)),
        )
        for invalid in ("é" * 64, "a" * 63, '"' * 64, "a" * 63 + "\\"):
            with self.assertRaises(ReviewInputError):
                plain_session_comment_json_bound(consumed_grants=(invalid,))

    def test_single_page_exact_fit_and_overflow_keep_original_response_cap(self):
        from review_sensei_github.session_ledger import plain_session_comment_json_bound

        from review_sensei.bounded_evidence import canonical_bytes
        from review_sensei.errors import ReviewInputError

        with tempfile.TemporaryDirectory() as directory:
            f = self.producer(directory)
            self.assertEqual(f.invoke(), 0, f.error)
            ledger = f.selected.ledger
            items = copy.deepcopy(ledger._cached_comments())
            filler = {
                "id": 987654,
                "body": "",
                "user": {"id": 987, "type": "User", "login": "reader"},
            }
            overhead = (
                plain_session_comment_json_bound(repository_id=123, pull_request=7)
                + 4096
                + (100 + 512 + 4096)
            )
            remaining = (
                512 * 1024
                - 1
                - overhead
                - len(canonical_bytes(items + [filler]))
                + len(
                    canonical_bytes(
                        next(
                            item["body"]
                            for item in items
                            if parse_session_comment(item["body"], identity=IDENTITY)
                            is not None
                        )
                    )
                )
            )
            self.assertGreater(remaining, 0)
            filler["body"] = "x" * remaining
            with (
                patch.object(ledger, "_cached_comments", return_value=items + [filler]),
                patch.object(ledger.evidence_budget, "preflight"),
            ):
                ledger._preflight_plain_finding_parts(
                    IDENTITY, added_parts=1, added_bytes=100, checkpoint_parts=(1,)
                )
                filler["body"] += "x"
                with self.assertRaisesRegex(ReviewInputError, "single-page capacity"):
                    ledger._preflight_plain_finding_parts(
                        IDENTITY, added_parts=1, added_bytes=100, checkpoint_parts=(1,)
                    )

    def test_one_short_existing_funding_refuses_before_part_dispatch(self):
        original = ApiRunSessionLedger.replace_finding_lifecycle_parts
        with tempfile.TemporaryDirectory() as directory:
            f = self.producer(directory)
            caught = []

            def run(ledger, identity, mutate, **kwargs):
                if kwargs["parts"] != 2:
                    return original(ledger, identity, mutate, **kwargs)
                before = copy.deepcopy(f.extra_comments)
                with patch.object(
                    type(ledger.caller),
                    "usable",
                    new_callable=PropertyMock,
                    return_value=9,
                ):
                    try:
                        return original(ledger, identity, mutate, **kwargs)
                    finally:
                        caught.append(True)
                        self.assertEqual(f.extra_comments, before)

            with patch.object(
                ApiRunSessionLedger, "replace_finding_lifecycle_parts", run
            ):
                self.assertNotEqual(f.invoke(), 0)
            self.assertTrue(caught)

    def test_competing_root_after_staging_retains_orphans_and_never_attaches(self):
        original = ApiRunSessionLedger.stage_evidence
        with tempfile.TemporaryDirectory() as directory:
            f = self.producer(directory)
            raced = []

            def stage(ledger, identity, **kwargs):
                manifest = original(ledger, identity, **kwargs)
                if manifest["encoding"] == "partitioned-json-v1" and not raced:
                    current = parse_session_comment(
                        f.comment["body"], identity=IDENTITY
                    )
                    rival = current.evolve(generation=current.generation + 1)
                    from review_sensei_github.session_ledger import (
                        render_session_comment,
                    )

                    f.comment["body"] = render_session_comment(
                        repository_id=123, pull_request=7, record=rival
                    )
                    raced.append((f.comment["body"], copy.deepcopy(f.extra_comments)))
                return manifest

            with patch.object(ApiRunSessionLedger, "stage_evidence", stage):
                self.assertNotEqual(f.invoke(), 0)
            self.assertTrue(raced)
            self.assertEqual(f.comment["body"], raced[0][0])
            self.assertEqual(f.extra_comments, raced[0][1])
            self.assertTrue(f.extra_comments)
