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
