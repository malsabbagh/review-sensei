"""Retained rescan admission through actual entry, transaction and completion."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace

from review_sensei_github.publication import (
    approval_eligibility_from_body,
)
from review_sensei_github.session_ledger import parse_session_comment
from test_api_work_persistence import HEAD, IDENTITY
from test_review_run import EntryFixture

from review_sensei.errors import ReviewInputError
from review_sensei.human_assessment import finding_instance_fingerprint
from review_sensei.models import ReviewResult


def partial_fixture(directory, *, limit=512):
    f = EntryFixture(directory, limit=limit)
    f.provider_comments = [
        {
            "path": "src/app.py",
            "line": 2,
            "side": "RIGHT",
            "body": f"Confirm interface assumption {i}.",
            "severity": "high",
            "blocking": False,
            "needs_human": True,
        }
        for i in range(20)
    ]
    patch = f.root / "review.patch"
    patch.write_text(
        patch.read_text()
        + "\ndiff --git a/image.bin b/image.bin\nnew file mode 100644\nindex 0000000..1111111\nBinary files /dev/null and b/image.bin differ\n",
        encoding="utf-8",
        newline="\n",
    )
    if f.invoke() != 0:
        raise AssertionError(f.error)
    record = parse_session_comment(f.comment["body"], identity=IDENTITY)
    result = ReviewResult.from_dict(json.loads((f.root / "review.json").read_text()))
    source = record.transaction
    # The current ordinary producer already writes the complete human review
    # into its authenticated eligibility. Do not rewrite that native body from
    # an unrelated optional HumanInventoryStore carrier (or fabricate a receipt).
    eligibility = approval_eligibility_from_body(f.reviews[-1]["body"])
    if eligibility is None or eligibility.human_review is None:
        raise AssertionError("ordinary partial producer omitted human inventory")
    if len(eligibility.human_review.findings) != 20:
        raise AssertionError("ordinary partial producer lost human obligations")
    decisions = [
        {
            "fingerprint": finding_instance_fingerprint(comment),
            "action": "accept-risk",
            "reason": "Synthetic retained decision",
            "actor": "alice",
            "head_sha": HEAD,
            "expires_at": None,
            "actor_role": "admin",
            "finding_reference": "RS-"
            + finding_instance_fingerprint(comment)[:6].upper(),
            "recorded_at": "2026-10-26T00:00:00Z",
            "policy_digest": source.policy_digest,
            "provenance": "comment:8",
            "resolution_kind": "accepted_unresolved",
            "target_kind": "finding",
        }
        for comment in result.comments
    ]
    f.rewrite_record(
        mutate=lambda r: r.evolve(generation=r.generation + 1, dispositions=decisions)
    )
    return f, source


def request(f, identifier, intent):
    f.next_run()
    f.requested_head = f.live_head = HEAD
    f.event_name = "issue_comment"
    body = "@reviewsensei " + intent
    comment = {
        "id": identifier,
        "body": body,
        "created_at": f"2026-10-26T00:00:{identifier:02d}Z",
        "updated_at": f"2026-10-26T00:00:{identifier:02d}Z",
        "user": {"id": 42, "login": "alice", "type": "User"},
        "author_association": "OWNER",
        "issue_url": "https://api.github.test/repos/owner/repo/issues/7",
    }
    f.extra_comments[identifier] = comment
    f.event_document = {
        "repository": {"id": 123, "full_name": "owner/repo"},
        "sender": {"login": "alice", "type": "User"},
        "action": "created",
        "issue": {"number": 7},
        "comment": comment,
    }
    f.review_intent = (
        "--review-remaining" if intent == "review remaining" else "--rescan"
    )


class RetainedEntryTests(unittest.TestCase):
    def test_failed_rescan_then_full_rescan_keeps_decisions_and_finishes_transaction(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            f, source = partial_fixture(directory)
            request(f, 9, "rescan")

            def refusal():
                raise ReviewInputError(
                    "known local validation refusal before operation entry"
                )

            f.before_entry = refusal
            code = f.invoke()
            self.assertEqual(code, 1, f.error)
            failed = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertIsNone(failed.transaction)
            self.assertIsNotNone(failed.api_run_accounting["analysis_abort"])
            self.assertEqual(len(failed.dispositions), 20)
            f.before_entry = None
            f.provider_comments = []
            request(f, 10, "rescan")
            self.assertEqual(f.invoke(), 0, f.error)
            final = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(final.transaction.phase, "publication_succeeded")
            self.assertEqual(len(final.dispositions), 20)
            self.assertEqual(f.provider_calls, 2)
            self.assertFalse(any(r["state"] == "APPROVED" for r in f.reviews))
            from review_sensei_github.api_run_history import read_previous_run
            from test_api_work_lifecycle import caller_measurement

            archived = read_previous_run(
                f.selected.ledger, IDENTITY, f.accounted.accounting.previous_record
            )
            self.assertEqual(archived.to_dict(), failed.to_dict())
            self.assertEqual(archived.dispositions, failed.dispositions)
            reference = f.accounted.accounting.previous_record
            self.assertEqual(len(reference["manifest"]["parts"]), 1)
            carrier = f.extra_comments[
                int(reference["manifest"]["parts"][0]["storage_id"])
            ]["body"]
            self.measurement = {
                **caller_measurement(f),
                "canonical_archived_root_bytes": len(
                    json.dumps(
                        failed.to_dict(), sort_keys=True, separators=(",", ":")
                    ).encode()
                ),
                "rendered_current_comment_bytes": len(f.comment["body"].encode()),
                "archived_part_bytes": len(carrier.encode()),
                "decisions_preserved": 20,
            }

    def test_prior_reference_controls_refuse_without_replacing_decisions(self):
        import base64
        import copy

        from review_sensei_github.prior_rescan_publication import (
            ArchivedRescanSource,
            admitted_rescan,
        )
        from review_sensei_github.publication import _APPROVAL_ELIGIBILITY_RE

        from review_sensei.api_work import ApiExecutionOwner, ApiRunAccounting

        for fault in (
            "reference",
            "carrier",
            "producer",
            "owner",
            "newer-review",
            "source",
            "review-id",
            "lost-read",
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                f, _source = partial_fixture(directory)
                original = parse_session_comment(
                    f.comment["body"], identity=IDENTITY
                ).dispositions
                request(f, 9, "rescan")

                def corrupt():
                    if fault == "reference":
                        match = _APPROVAL_ELIGIBILITY_RE.search(f.reviews[0]["body"])
                        raw = match.group("payload").rstrip("=")
                        doc = json.loads(
                            base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
                        )
                        # Inline current eligibility binds its exact admitted result,
                        # rather than an absent optional inventory-part reference.
                        doc["result_digest"] = "f" * 64
                        replacement = (
                            "<!-- reviewsensei:eligibility:v1 "
                            + base64.urlsafe_b64encode(json.dumps(doc).encode())
                            .decode()
                            .rstrip("=")
                            + " -->"
                        )
                        f.reviews[0]["body"] = f.reviews[0]["body"].replace(
                            match.group(0), replacement
                        )
                    elif fault == "carrier":
                        record = parse_session_comment(
                            f.comment["body"], identity=IDENTITY
                        )
                        manifest = record.finding_lifecycle["manifest"]
                        self.assertEqual(manifest["encoding"], "partitioned-json-v1")
                        selected_part = int(manifest["parts"][0]["storage_id"])
                        f.extra_comments[selected_part]["body"] += "tampered"
                    elif fault == "producer":
                        f.archive_bad = "owner"
                    elif fault == "owner":

                        def takeover(record):
                            state = ApiRunAccounting.from_document(
                                record.api_run_accounting
                            )
                            state = state.claim_startup(
                                ApiExecutionOwner(
                                    2, "hosted", state.owner.generation + 1
                                )
                            )
                            return record.evolve(
                                generation=record.generation + 1,
                                api_run_accounting=state.to_document(),
                            )

                        f.rewrite_record(mutate=takeover)
                    elif fault == "newer-review":
                        f.reviews.append(
                            {
                                **copy.deepcopy(f.reviews[0]),
                                "id": 101,
                                "body": "<!-- reviewsensei:eligibility:v1 broken -->",
                            }
                        )
                    elif fault in {"source", "review-id"}:
                        record = parse_session_comment(
                            f.comment["body"], identity=IDENTITY
                        )
                        authorized = admitted_rescan(record)
                        if fault == "source":
                            authorized = replace(
                                authorized,
                                source=replace(
                                    authorized.source, result_sha256="f" * 64
                                ),
                            )
                        else:
                            authorized = replace(authorized, review_id=999)
                        ArchivedRescanSource.create(f.selected.ledger, authorized)
                    else:
                        # Force this supported retained-source read explicitly:
                        # inline human eligibility needs no optional old inventory
                        # read, so merely installing a late HTTP hook exercises none.
                        record = parse_session_comment(
                            f.comment["body"], identity=IDENTITY
                        )
                        authorized = admitted_rescan(record)
                        native = f.accounted._opener
                        ids = {
                            str(p["storage_id"])
                            for p in f.accounted.accounting.previous_record["manifest"][
                                "parts"
                            ]
                        }

                        def lost(req, timeout):
                            from urllib.parse import urlsplit

                            path = urlsplit(req.full_url).path
                            if (
                                req.get_method() == "GET"
                                and path.rsplit("/", 1)[-1] in ids
                            ):
                                f.trace.append((f.attempt, "GET", path))
                                raise TimeoutError("synthetic lost archived read")
                            return native(req, timeout)

                        f.accounted._opener = lost
                        ArchivedRescanSource.create(f.selected.ledger, authorized)

                f.before_publication = corrupt
                self.assertEqual(f.invoke(), 1, f.error)
                final = parse_session_comment(f.comment["body"], identity=IDENTITY)
                self.assertEqual(final.dispositions, original)
                self.assertFalse(any(r["state"] == "APPROVED" for r in f.reviews))
                self.assertEqual(f.provider_calls, 2)
                if fault == "lost-read":
                    self.assertGreater(f.accounted.accounting.uncertain, 0)

    def test_fresh_rescan_preserves_authority_without_consuming_edited_old_prose(self):
        """Accepted old projections are not a new rescan's finding authority.

        Unresolved native-root reuse still has its own exact-readback refusal
        tests. Here all twenty decisions are retained and a fresh analysis uses
        the complete durable inventory, not edited REST/GraphQL/eligibility prose.
        """
        import base64

        from review_sensei_github.finding_lifecycle import _unpack_journal
        from review_sensei_github.publication import _APPROVAL_ELIGIBILITY_RE

        from review_sensei.bounded_evidence import (
            EvidenceReadBudget,
            read_inline_evidence,
            read_partitioned_evidence,
        )
        from review_sensei.finding_lifecycle import decode_lifecycle_state

        def observe_stored_state(f, record):
            # Inspect already-produced synthetic service bytes with the real
            # owner/framing/digest/codec validators. This observer performs no
            # HTTP and supplies no state or authority to the production route.
            before = len(f.trace)
            manifest = record.finding_lifecycle["manifest"]
            binding = manifest["binding"]
            if manifest["encoding"] == "inline-json-v1":
                envelope = read_inline_evidence(manifest, expected_binding=binding)
            else:
                envelope = read_partitioned_evidence(
                    manifest,
                    reader=lambda sid: f.selected.ledger._part_from_item(
                        f.extra_comments[int(sid)], IDENTITY, sid
                    ),
                    expected_binding=binding,
                    budget=EvidenceReadBudget(),
                )
            self.assertEqual(len(f.trace), before)
            journal = _unpack_journal(envelope["lifecycle"])
            return decode_lifecycle_state(journal["lifecycle"]).state

        with tempfile.TemporaryDirectory() as directory:
            f, _source = partial_fixture(directory)
            original = parse_session_comment(f.comment["body"], identity=IDENTITY)
            original_state = observe_stored_state(f, original)
            original_obligations = original_state.inventory.obligations
            self.assertEqual(len(original_obligations), 20)
            request(f, 9, "rescan")
            edited = {}

            def mutate_projection_only():
                row = f.inline[0]
                row["body"] += "\nSubstituted explanation."
                edited["comment_id"] = row["id"]
                edited["comment_body"] = row["body"]
                for thread in f.threads:
                    for comment in thread["comments"]["nodes"]:
                        if comment["databaseId"] == row["id"]:
                            comment["body"] = row["body"]
                review = f.reviews[0]
                match = _APPROVAL_ELIGIBILITY_RE.search(review["body"])
                raw = match.group("payload").rstrip("=")
                document = json.loads(
                    base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
                )
                document["human_review"]["findings"][0]["body"] = (
                    "Substituted explanation."
                )
                marker = (
                    "<!-- reviewsensei:eligibility:v1 "
                    + base64.urlsafe_b64encode(json.dumps(document).encode())
                    .decode()
                    .rstrip("=")
                    + " -->"
                )
                review["body"] = review["body"].replace(match.group(0), marker)
                edited["review_body"] = review["body"]

            f.before_publication = mutate_projection_only
            self.assertEqual(f.invoke(), 0, f.error)
            final = parse_session_comment(f.comment["body"], identity=IDENTITY)
            final_state = observe_stored_state(f, final)
            obligations = final_state.inventory.obligations
            self.assertEqual(final.dispositions, original.dispositions)
            self.assertEqual(len(final.dispositions), 20)
            self.assertEqual(
                {
                    (o.obligation_id, o.criterion.resolution_criterion)
                    for o in obligations
                },
                {
                    (o.obligation_id, o.criterion.resolution_criterion)
                    for o in original_obligations
                },
            )
            self.assertFalse(
                any("Substituted explanation." in str(o) for o in obligations)
            )
            self.assertEqual(f.reviews[0]["body"], edited["review_body"])
            self.assertEqual(
                next(c["body"] for c in f.inline if c["id"] == edited["comment_id"]),
                edited["comment_body"],
            )
            self.assertEqual(
                next(
                    c["body"]
                    for t in f.threads
                    for c in t["comments"]["nodes"]
                    if c["databaseId"] == edited["comment_id"]
                ),
                edited["comment_body"],
            )
            self.assertFalse(any(r["state"] == "APPROVED" for r in f.reviews))
            result = ReviewResult.from_dict(
                json.loads((f.root / "review.json").read_text())
            )
            self.assertEqual(result.review_status, "partial")
            self.assertEqual(f.provider_calls, 2)
            self.assertEqual(f.accounted.accounting.configuration.limit, 512)

    def test_original_transaction_archive_and_accounting_only_checkpoint_are_readable(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            f, _source = partial_fixture(directory)
            original = parse_session_comment(
                f.comment["body"], identity=IDENTITY
            ).dispositions
            request(f, 9, "rescan")
            f.provider_comments = []

            def accounting_only():
                f.selected.ledger.replace(
                    IDENTITY, lambda r: r.evolve(generation=r.generation + 1)
                )

            f.before_publication = accounting_only
            self.assertEqual(f.invoke(), 0, f.error)
            final = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(final.dispositions, original)
            self.assertEqual(final.transaction.phase, "publication_succeeded")
            self.assertFalse(any(r["state"] == "APPROVED" for r in f.reviews))

    def test_same_run_successor_rebuilds_prior_publication_reader_from_records(self):
        with tempfile.TemporaryDirectory() as directory:
            f, _source = partial_fixture(directory)
            original = parse_session_comment(
                f.comment["body"], identity=IDENTITY
            ).dispositions
            request(f, 9, "rescan")
            f.provider_comments = []

            def interrupted():
                raise KeyboardInterrupt()

            f.before_publication = interrupted
            self.assertEqual(f.invoke(), 130, f.error)
            pending = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(pending.transaction.phase, "publication_pending")
            f.before_publication = None
            f.attempt = 2
            self.assertEqual(f.invoke(), 0, f.error)
            final = parse_session_comment(f.comment["body"], identity=IDENTITY)
            self.assertEqual(final.dispositions, original)
            self.assertEqual(final.transaction.phase, "publication_succeeded")
            self.assertEqual(
                final.transaction.transaction_id, pending.transaction.transaction_id
            )
            self.assertEqual(f.provider_calls, 2)
            self.assertFalse(any(r["state"] == "APPROVED" for r in f.reviews))
