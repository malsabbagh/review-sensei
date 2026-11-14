"""Source-owned pre-entry planning never installs an admitted action."""

import copy
import tempfile
import unittest
from unittest.mock import patch

from review_sensei_github.action_run import execute_command

from review_sensei.bounded_evidence import canonical_bytes
from tests.test_run_authority import NOW
from tests.test_trust_status_route import StatusFixture


class PreentryIntentTests(unittest.TestCase):
    def test_pure_source_intent_matches_actual_admitted_bytes_without_effect(self):
        import hashlib

        with (
            tempfile.TemporaryDirectory() as directory,
            patch("time.time", return_value=NOW),
        ):
            f = StatusFixture(directory)
            source = f.session.attestation["source"]
            key = hashlib.sha256(
                canonical_bytes(
                    {
                        "purpose": "command",
                        "repository_id": f.run.identity.repository_id,
                        "pull_request": f.run.identity.pull_request,
                        "head_sha": f.run.head_sha,
                        "source": source,
                        "configuration": f.caller.accounting.configuration.digest,
                    }
                )
            ).hexdigest()
            before = canonical_bytes(f.caller.accounting.to_document())
            root, requests = f.caller._root, len(f.caller.observations)
            preview = f.caller.action_intent_document(
                purpose="command",
                source=copy.deepcopy(source),
                head_sha=f.run.head_sha,
                operation_key=key,
            )
            self.assertEqual(canonical_bytes(f.caller.accounting.to_document()), before)
            self.assertEqual(f.caller._root, root)
            self.assertEqual(len(f.caller.observations), requests)
            self.assertIsNone(f.caller.accounting.action)
            self.assertEqual(execute_command(f.run, f.args), 0)
            self.assertEqual(
                canonical_bytes(f.caller.accounting.action["intent"]),
                canonical_bytes(preview),
            )

    def test_admission_rejects_instance_and_class_intent_substitution(self):
        from review_sensei_github.api_run_requests import ApiRunRequestTransport

        from review_sensei.errors import ReviewInputError

        for target in ("instance", "class"):
            with (
                self.subTest(target=target),
                tempfile.TemporaryDirectory() as directory,
                patch("time.time", return_value=NOW),
            ):
                f = StatusFixture(directory)
                before = canonical_bytes(f.caller.accounting.to_document())
                root, count = f.caller._root, len(f.caller.observations)
                invoked = []

                def forged(*args, **kwargs):
                    invoked.append(True)
                    return {
                        "purpose": "command",
                        "source": {},
                        "head_sha": "f" * 40,
                        "operation_key": "f" * 64,
                        "source_root": "f" * 64,
                    }

                owner = f.caller if target == "instance" else ApiRunRequestTransport
                with patch.object(owner, "action_intent_document", forged):
                    with self.assertRaises(ReviewInputError):
                        f.caller.admit_action(
                            f.action_ledger,
                            f.run.identity,
                            purpose="command",
                            source=f.session.attestation["source"],
                            head_sha=f.run.head_sha,
                            operation_key="a" * 64,
                        )
                self.assertFalse(invoked)
                self.assertEqual(
                    canonical_bytes(f.caller.accounting.to_document()), before
                )
                self.assertEqual(f.caller._root, root)
                self.assertEqual(len(f.caller.observations), count)
                self.assertIsNone(f.caller.accounting.action)

    def test_actual_retained_finding_preview_does_not_admit_or_write(self):
        from dataclasses import replace

        from review_sensei_github import action_run
        from review_sensei_github.broker_client import BrokerClient
        from review_sensei_github.publication import parse_finding_index
        from review_sensei_github.trust_disposition import prepare_source_preview
        from review_sensei_github.trust_effects import enable

        from review_sensei.errors import ReviewInputError
        from tests.test_trust_pause_cold import CurrentRoleEntryFixture

        with tempfile.TemporaryDirectory() as directory:
            f = CurrentRoleEntryFixture(directory)
            f.provider_comments = [
                dict(
                    path="src/app.py",
                    line=2,
                    side="RIGHT",
                    body="Confirm this exact interface assumption.",
                    severity="high",
                    blocking=False,
                    needs_human=True,
                )
            ]
            self.assertEqual(f.opted(f.invoke), 0, f.error)
            index = parse_finding_index(
                f.reviews[-1]["body"],
                repository_id=123,
                pull_request=7,
                head_sha=f.requested_head,
            )
            finding = "RS-" + index.entries[0].instance[:6].upper()
            f.select_command(
                "@reviewsensei override " + finding + " acceptable risk", new_run=True
            )
            captured = []
            with patch.object(
                action_run,
                "execute_command",
                lambda run, args: captured.append((run, args)) or 0,
            ):
                self.assertEqual(f.opted(f.invoke_command), 0, f.error)
            run, args = captured[0]
            broker = BrokerClient(
                broker_url=run.broker.broker_url,
                control=run.broker.control,
                opener=run.caller.open,
                authority={**run.broker.authority, "version": 4},
            )
            session = broker.open_session(
                "synthetic-oidc",
                repository_id=123,
                pull_request=7,
                head_sha=run.head_sha,
                observe_actor_role=True,
            )
            run = replace(
                run, session_authority=session.attestation, session_grant=session.grant
            )
            enable(run.caller, broker, session, api_url=run.http.api_url)
            before, root, start = (
                copy.deepcopy(f.comment),
                run.caller._root,
                len(f.trace),
            )
            provider_calls = f.provider_calls
            result, preview, report, details = prepare_source_preview(run, args)
            self.assertEqual(f.provider_calls, provider_calls)
            self.assertTrue(report.representable)
            self.assertTrue(result.applied)
            self.assertEqual(len(result.receipts), 1)
            self.assertEqual(result.receipts[0].reference, finding)
            self.assertEqual(f.comment, before)
            self.assertEqual(run.caller._root, root)
            self.assertIsNone(run.caller.accounting.action)
            self.assertFalse(
                any(
                    method != "GET" and path.startswith("/repos/")
                    for _, method, path in f.trace[start:]
                )
            )
            self.assertEqual(run.caller.accounting.configuration.limit, 512)
            # A truthful detached preview never creates a native permit.
            with self.assertRaises(ReviewInputError):
                execute_command(run, args)
            self.assertEqual(f.comment, before)
