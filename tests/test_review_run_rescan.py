"""Retained rescan admission through actual entry, transaction and completion."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from dataclasses import replace

from review_sensei_github.default_human_inventory import (
    HumanInventoryStore,
    build_human_inventory_context,
)
from review_sensei_github.default_human_reader import transaction_publication_digest
from review_sensei_github.publication import (
    approval_eligibility_from_body,
    approval_eligibility_marker,
)
from review_sensei_github.session_ledger import parse_session_comment

from review_sensei.errors import ReviewInputError
from review_sensei.human_assessment import finding_instance_fingerprint
from review_sensei.human_inventory import inventory_from_result_document
from review_sensei.models import ReviewResult
from tests.test_api_work_persistence import BASE, HEAD, IDENTITY
from tests.test_review_run import EntryFixture


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
        + "\ndiff --git a/image.bin b/image.bin\nnew file mode 100644\nindex 0000000..1111111\nBinary files /dev/null and b/image.bin differ\n"
    )
    if f.invoke() != 0:
        raise AssertionError(f.error)
    record = parse_session_comment(f.comment["body"], identity=IDENTITY)
    result = ReviewResult.from_dict(json.loads((f.root / "review.json").read_text()))
    source = record.transaction
    context = build_human_inventory_context(
        http=f.selected.http,
        token=f.selected.ledger.token,
        ledger=f.selected.ledger,
        identity=IDENTITY,
        base_sha=BASE,
        head_sha=HEAD,
        app_slug="reviewsensei[bot]",
        publication_digest=transaction_publication_digest(source),
        policy_digest=source.policy_digest,
        configuration_digest=source.configuration_digest,
        evidence_digest=source.evidence_digest,
        budget=f.accounted.budget_view,
    )
    reference, document = HumanInventoryStore(
        f.selected.http, f.selected.ledger.token, f.selected.ledger, context
    ).read_available()
    eligibility = approval_eligibility_from_body(f.reviews[-1]["body"])
    inventory = inventory_from_result_document(
        document["human_inventory"],
        result_digest=context.publication_digest,
        base_sha=BASE,
    )
    marker = approval_eligibility_marker(
        replace(
            eligibility, human_review=inventory, human_inventory_reference=reference
        )
    )
    f.reviews[-1]["body"] = re.sub(
        r"<!-- reviewsensei:eligibility:v1 [A-Za-z0-9_=-]+ -->",
        lambda _match: marker,
        f.reviews[-1]["body"],
    )
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

            from tests.test_api_work_lifecycle import caller_measurement

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
            "prose",
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
                        doc["human_inventory_reference"]["payload_digest"] = "f" * 64
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
                    elif fault == "prose":
                        f.inline[0]["body"] = f.inline[0]["body"].replace(
                            "Confirm interface assumption 0.",
                            "Substituted explanation.",
                        )
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

                f.before_publication = corrupt
                self.assertEqual(f.invoke(), 1, f.error)
                final = parse_session_comment(f.comment["body"], identity=IDENTITY)
                self.assertEqual(final.dispositions, original)
                self.assertFalse(any(r["state"] == "APPROVED" for r in f.reviews))
                self.assertEqual(f.provider_calls, 2)
                if fault == "lost-read":
                    self.assertGreater(f.accounted.accounting.uncertain, 0)

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
