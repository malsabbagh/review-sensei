"""Current command metadata/permission safety through real CLI and native writes.

External HTTP is synthetic. The application, v3 authority parser, source fences,
run owner, caller meter, command handler and finalizer remain real.
"""

from __future__ import annotations

import copy
import os
import tempfile
import unittest
from unittest.mock import patch

from review_sensei_github import action_run
from review_sensei_github import cli as package_cli
from test_current_command_inventory import HEAD, CurrentMetadataFixture


class HostedOverrideMetadataTests(unittest.TestCase):
    def assert_no_override_or_approval(self, fixture):
        self.assertEqual(fixture.business_writes, 0)
        self.assertFalse(fixture.record().dispositions)
        self.assertFalse(any(r["state"] == "APPROVED" for r in fixture.reviews))
        self.assertEqual(fixture.provider_calls, 1)  # only the original review

    def assert_current_accounting(self, fixture):
        self.assertEqual(len(fixture.accounted.observations), len(fixture.trace))
        self.assertTrue(
            all(
                p["session_attestation"]["version"] == 3
                for p in fixture.broker_requests
            )
        )

    def test_admin_and_maintain_apply_using_the_current_session_capability(self):
        for permission in (
            {"permission": "admin", "role_name": "admin"},
            {"permission": "write", "role_name": "maintain"},
            {"permission": "maintain"},
        ):
            for token_name in ("GITHUB_TOKEN", "GH_TOKEN"):
                with (
                    self.subTest(permission=permission, token=token_name),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    f = CurrentMetadataFixture(directory).seed_partial()
                    f.permission = permission
                    original_bootstrap = action_run.bootstrap

                    def bootstrap(*args, **kwargs):
                        os.environ.pop("GITHUB_TOKEN", None)
                        os.environ[token_name] = " synthetic-workflow-read "
                        return original_bootstrap(*args, **kwargs)

                    with patch.object(action_run, "bootstrap", side_effect=bootstrap):
                        self.assertEqual(f.invoke_command(), 0, (f.error, f.exception))
                    record = f.record()
                    self.assertEqual(len(record.dispositions), 1)
                    self.assertEqual(
                        record.dispositions[0]["fingerprint"], f.fingerprint
                    )
                    self.assertEqual(record.dispositions[0]["head_sha"], HEAD)
                    self.assertEqual(record.dispositions[0]["actor"], "alice")
                    self.assertTrue(f.permission_reads)
                    self.assertTrue(
                        all(
                            token == "Bearer synthetic-review_session"
                            for _, token in f.permission_reads
                        )
                    )
                    self.assertEqual(f.provider_calls, 1)
                    self.assertFalse(any(r["state"] == "APPROVED" for r in f.reviews))
                    self.assert_current_accounting(f)

    def test_missing_or_blank_workflow_tokens_refuse_before_any_command_http(self):
        for environment in ({}, {"GITHUB_TOKEN": " "}, {"GH_TOKEN": "\t"}):
            with (
                self.subTest(environment=environment),
                tempfile.TemporaryDirectory() as directory,
            ):
                f = CurrentMetadataFixture(directory).seed_partial()
                prior = f.comment["body"]
                original_bootstrap = action_run.bootstrap

                def bootstrap(*args, **kwargs):
                    os.environ.pop("GITHUB_TOKEN", None)
                    os.environ.pop("GH_TOKEN", None)
                    os.environ.update(environment)
                    return original_bootstrap(*args, **kwargs)

                with patch.object(action_run, "bootstrap", side_effect=bootstrap):
                    self.assertNotEqual(f.invoke_command(), 0)
                self.assertFalse(f.trace)
                self.assertFalse(f.broker_requests)
                self.assertEqual(f.comment["body"], prior)
                self.assert_no_override_or_approval(f)

    def test_write_and_untrusted_custom_roles_do_not_gain_override_authority(self):
        for permission in (
            {"permission": "write"},
            {"permission": "write", "role_name": "write"},
            {"permission": "write", "role_name": "custom-maintainer"},
            {"permission": "write", "role_name": "admin"},
            {"permission": "write", "role_name": "Maintain"},
            {"permission": "write", "role_name": "maintain "},
            {"permission": "read", "role_name": "maintain"},
            {"permission": "none", "role_name": "maintain"},
            {"permission": "triage", "role_name": "maintain"},
        ):
            with (
                self.subTest(permission=permission),
                tempfile.TemporaryDirectory() as directory,
            ):
                f = CurrentMetadataFixture(directory).seed_partial()
                f.permission = permission
                self.assertEqual(f.invoke_command(), 0, (f.error, f.exception))
                self.assertIn("unauthorized", f.stdout)
                self.assert_no_override_or_approval(f)
                self.assert_current_accounting(f)

    def test_untrusted_actors_and_missing_write_opt_in_never_apply_a_command(self):
        for field, value in (
            ("association", "CONTRIBUTOR"),
            ("type", "Bot"),
            ("login", "reviewsensei[bot]"),
            ("login", "alice/../bob"),
        ):
            with (
                self.subTest(field=field, value=value),
                tempfile.TemporaryDirectory() as directory,
            ):
                f = CurrentMetadataFixture(directory).seed_partial()
                f.actor_identity[field] = value
                f.select_command(f.command_body)
                self.assertNotEqual(f.invoke_command(), 0)
                self.assertFalse(f.permission_reads)
                self.assert_no_override_or_approval(f)
        with tempfile.TemporaryDirectory() as directory:
            f = CurrentMetadataFixture(directory).seed_partial()
            main = package_cli.main

            def without_write(arguments):
                return main([a for a in arguments if a != "--allow-write"])

            with patch.object(package_cli, "main", side_effect=without_write):
                self.assertNotEqual(f.invoke_command(), 0)
            self.assertFalse(f.trace)
            self.assert_no_override_or_approval(f)

    def test_external_authority_and_mismatched_events_refuse_before_http(self):
        for extra in (
            ("--session-attestation", "missing-old-authority.json"),
            ("--oidc-token", "caller-supplied-token"),
        ):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as directory:
                f = CurrentMetadataFixture(directory).seed_partial()
                self.assertNotEqual(f.invoke_command(extra=extra), 0)
                self.assertFalse(f.trace)
                self.assert_no_override_or_approval(f)
        for field in ("pull_request", "comment_id", "body", "updated_at"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                f = CurrentMetadataFixture(directory).seed_partial()
                event = {"issue": {"number": 7}, "comment": copy.deepcopy(f.source)}
                if field == "pull_request":
                    event["issue"]["number"] = 8
                elif field == "comment_id":
                    event["comment"]["id"] += 1
                elif field == "body":
                    event["comment"]["body"] += " changed"
                else:
                    event["comment"]["updated_at"] = None
                self.assertNotEqual(f.invoke_command(event_override=event), 0)
                self.assertFalse(f.trace)
                self.assert_no_override_or_approval(f)

    def test_current_grant_and_fresh_source_rechecks_precede_business_mutation(self):
        for fault in ("purpose", "run", "head", "source", "obsolete"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                f = CurrentMetadataFixture(directory).seed_partial()
                f.grant_fault = fault
                self.assertNotEqual(f.invoke_command(), 0)
                self.assert_no_override_or_approval(f)
        for fault in ("body", "association", "actor_type", "head"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                f = CurrentMetadataFixture(directory).seed_partial()

                def changed(current):
                    if current.source_reads != 1:
                        return
                    if fault == "body":
                        current.source["body"] += " edited"
                    elif fault == "association":
                        current.source["author_association"] = "CONTRIBUTOR"
                    elif fault == "actor_type":
                        current.source["user"]["type"] = "Bot"
                    else:
                        current.live_head = "f" * 40

                f.before_source_read = changed
                self.assertNotEqual(f.invoke_command(), 0)
                self.assert_no_override_or_approval(f)
                self.assert_current_accounting(f)

    def test_only_latest_owned_review_on_current_head_supplies_fingerprints(self):
        with tempfile.TemporaryDirectory() as directory:
            f = CurrentMetadataFixture(directory).seed_partial()
            # Keep the owned root live: native PUT/readback must observe the
            # updated eligibility, not an operation-long cached review body.
            current = f.reviews[-1]
            f.metadata_reviews = [
                current,
                {
                    **copy.deepcopy(current),
                    "id": current["id"] + 10,
                    "user": {"login": "mallory", "type": "User", "id": 888},
                    "body": "foreign numeric producer",
                },
                {
                    **copy.deepcopy(current),
                    "id": current["id"] + 20,
                    "commit_id": "f" * 40,
                    "body": "stale",
                },
            ]
            self.assertEqual(f.invoke_command(), 0, (f.error, f.exception))
            self.assertEqual(f.record().dispositions[0]["fingerprint"], f.fingerprint)
            self.assertFalse(any(r["state"] == "APPROVED" for r in f.reviews))
            self.assert_current_accounting(f)
        for body in ("", None):
            with (
                self.subTest(newest_body=body),
                tempfile.TemporaryDirectory() as directory,
            ):
                f = CurrentMetadataFixture(directory).seed_partial()
                newest = {
                    **copy.deepcopy(f.original_review),
                    "id": f.original_review["id"] + 1,
                    "body": body,
                }
                f.metadata_reviews = [f.reviews[-1], newest]
                self.assertNotEqual(f.invoke_command(), 0)
                self.assert_no_override_or_approval(f)
                self.assertEqual(len(f.inventory_reads), 1)
                self.assertFalse(f.inline_inventory_reads)
                self.assert_current_accounting(f)

    def test_unavailable_metadata_never_applies_override_or_approval(self):
        for changes in (
            {"permission_status": 403},
            {"permission": {"role_name": "maintain"}},
            {"permission": {"permission": None}},
            {"review_status": 403},
        ):
            with (
                self.subTest(changes=changes),
                tempfile.TemporaryDirectory() as directory,
            ):
                f = CurrentMetadataFixture(directory).seed_partial()
                for key, value in changes.items():
                    setattr(f, key, value)
                self.assertNotEqual(f.invoke_command(), 0)
                self.assert_no_override_or_approval(f)
                self.assert_current_accounting(f)

    def test_supported_accept_risk_and_coverage_commands_require_live_elevated_role(
        self,
    ):
        for command in ("accept-risk", "override", "coverage"):
            for permission, applied in (("write", False), ("maintain", True)):
                with (
                    self.subTest(command=command, permission=permission),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    f = CurrentMetadataFixture(directory).seed_partial()
                    if command == "accept-risk":
                        body = (
                            f"@sensei accept-risk {f.fingerprint} --reason known issue"
                        )
                    elif command == "override":
                        body = f"@sensei override {f.fingerprint} acceptable risk"
                    else:
                        body = "@reviewsensei accept-coverage-uncertainty --reason bounded window"
                    f.select_command(body)
                    f.permission = {"permission": "write", "role_name": permission}
                    self.assertEqual(f.invoke_command(), 0, (f.error, f.exception))
                    if applied:
                        self.assertTrue(f.record().dispositions)
                    else:
                        self.assertIn("unauthorized", f.stdout)
                        self.assert_no_override_or_approval(f)
                    self.assertFalse(any(r["state"] == "APPROVED" for r in f.reviews))
                    self.assert_current_accounting(f)


if __name__ == "__main__":
    unittest.main()
