"""A cached dictionary never excuses a changed immutable native base."""

import hashlib
import tempfile
import unittest
from urllib.parse import urlsplit

from review_sensei_github import finding_lifecycle_dictionary as codec
from review_sensei_github.finding_lifecycle import _pack_journal

from review_sensei.errors import ReviewInputError
from review_sensei.finding_lifecycle_store import stage_finding_lifecycle
from tests import test_finding_lifecycle_journal as f


class DictionaryWriteFenceTests(unittest.TestCase):
    def fixture(self, directory):
        from tests.test_finding_lifecycle_parts import CurrentRunFindingPartsTests

        fixture, record, kwargs = CurrentRunFindingPartsTests().fixture(directory)
        body = "".join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(180))
        logical = f.document(
            [
                f.effect(
                    {**f.REQUEST, "body": body},
                    path="/repos/owner/repo/pulls/7/reviews",
                )
            ]
        )
        kwargs = {**kwargs, "document": _pack_journal(logical)}
        current = stage_finding_lifecycle(fixture.ledger, record, **kwargs)
        manifest = current.finding_lifecycle["manifest"]
        self.assertEqual(manifest["encoding"], "partitioned-json-v1")
        base = {
            "schema_version": "finding-lifecycle-evidence-v2",
            "lifecycle": _pack_journal(logical),
        }
        carrier = codec.pack(
            logical, base=base, manifest=manifest, current_binding=manifest["binding"]
        )
        return (
            fixture,
            current,
            kwargs,
            carrier,
            int(manifest["parts"][0]["storage_id"]),
        )

    def test_fresh_listing_rejects_base_edit_missing_and_foreign_owner_before_activation(
        self,
    ):
        for fault in ("edited", "missing", "foreign"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                fixture, record, kwargs, carrier, base_id = self.fixture(directory)
                ledger = fixture.ledger
                original = fixture.caller._opener
                mutated = []
                before_root = record.finding_lifecycle

                def opener(request, timeout):
                    path = urlsplit(request.full_url).path
                    # Initial admission used actual source bytes. Mutate the
                    # native base immediately before a fresh discovery response.
                    if (
                        not mutated
                        and request.get_method() == "GET"
                        and path.endswith("/issues/7/comments")
                    ):
                        mutated.append(True)
                        if fault == "missing":
                            fixture.service.extra_comments.pop(base_id)
                        elif fault == "foreign":
                            fixture.service.extra_comments[base_id]["user"]["id"] = 9999
                        else:
                            fixture.service.extra_comments[base_id]["body"] += "edited"
                    return original(request, timeout)

                fixture.caller._opener = opener
                dispatched = []
                with self.assertRaises(ReviewInputError):
                    with ledger.finding_dictionary_checkpoint(record, carrier):
                        stage_finding_lifecycle(
                            ledger, record, **{**kwargs, "document": carrier}
                        )
                        dispatched.append("native-effect")
                self.assertTrue(mutated)
                self.assertFalse(dispatched)
                # No v4 can have become the selected finding reference.
                from review_sensei_github.session_ledger import parse_session_comment

                selected = parse_session_comment(
                    fixture.service.comment["body"], identity=fixture.identity
                )
                self.assertEqual(selected.finding_lifecycle, before_root)
                fixture.assert_physical_accounting(self)

    def test_post_patch_base_edit_keeps_new_pending_root_and_no_native_dispatch(self):
        from review_sensei_github.session_ledger import parse_session_comment

        with tempfile.TemporaryDirectory() as directory:
            fixture, record, kwargs, carrier, base_id = self.fixture(directory)
            original = fixture.caller._opener
            changed = []

            def opener(request, timeout):
                response = original(request, timeout)
                if request.get_method() == "PATCH" and request.data:
                    import json

                    payload = json.loads(request.data)
                    candidate = parse_session_comment(
                        payload.get("body"), identity=fixture.identity
                    )
                    if (
                        candidate is not None
                        and candidate.finding_lifecycle != record.finding_lifecycle
                    ):
                        fixture.service.extra_comments[base_id]["body"] += (
                            "edited-after-activation"
                        )
                        changed.append(True)
                return response

            fixture.caller._opener = opener
            native = []
            with self.assertRaises(ReviewInputError):
                with fixture.ledger.finding_dictionary_checkpoint(record, carrier):
                    stage_finding_lifecycle(
                        fixture.ledger, record, **{**kwargs, "document": carrier}
                    )
                    native.append("dispatch")
            self.assertTrue(changed)
            self.assertFalse(native)
            current = parse_session_comment(
                fixture.service.comment["body"], identity=fixture.identity
            )
            self.assertNotEqual(current.finding_lifecycle, record.finding_lifecycle)
            self.assertEqual(
                current.finding_lifecycle["operation_id"],
                record.finding_lifecycle["operation_id"],
            )
            self.assertEqual(
                current.finding_lifecycle["previous_snapshot_sha256"],
                record.finding_lifecycle["manifest"]["sha256"],
            )
            self.assertIn(base_id, fixture.service.extra_comments)
            fixture.assert_physical_accounting(self)

    def test_source_owned_base_validation_adds_no_network_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture, record, kwargs, carrier, _ = self.fixture(directory)
            before = len(fixture.service.trace)
            with fixture.ledger.finding_dictionary_checkpoint(record, carrier):
                current = stage_finding_lifecycle(
                    fixture.ledger, record, **{**kwargs, "document": carrier}
                )
            manifest = current.finding_lifecycle["manifest"]
            parts = len(manifest.get("parts", ()))
            self.assertLessEqual(len(fixture.service.trace) - before, 5 + 3 * parts)
            fixture.assert_physical_accounting(self)
