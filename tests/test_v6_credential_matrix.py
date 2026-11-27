"""Exhaustive verification for the ReviewSensei v6 credential matrix and docs."""

from __future__ import annotations

import json
import re
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import MappingProxyType

from review_sensei_github.errors import GitHubHTTPError
from review_sensei_github.v6_credential_matrix import (
    BROKER_APP_READ_ALLOWLIST,
    V6_ENDPOINT_CREDENTIAL_MATRIX,
    WORKFLOW_READ_ONLY_SCOPES,
    CredentialKind,
    EndpointCredentialRule,
    GitHubEventKind,
    OperationKind,
    RepositoryVisibility,
    V6CredentialPermissionError,
    authorize_v6_endpoint_credential,
    get_v6_endpoint_rule,
    validate_v6_matrix_least_privilege,
)

from review_sensei.errors import ReviewInputError

ROOT = Path(__file__).resolve().parents[1]

EXPECTED_BULK_READ_ENDPOINTS = frozenset(
    {
        "pr_metadata",
        "pr_commits",
        "pr_diff",
        "pr_compare",
        "git_tree",
        "git_blob",
        "git_commit",
        "pr_files",
        "pr_issue_comments",
        "pr_review_comments",
        "pr_reviews",
        "graphql_review_threads",
        "check_runs_and_suites",
        "workflow_runs_and_jobs",
    }
)

EXPECTED_BROKER_AUTHORITY_READ_ENDPOINTS = frozenset(
    {
        "broker_app_identity",
        "broker_pr_binding_fence",
        "broker_collaborator_permission",
        "broker_active_writer_fence",
        "broker_effect_verification",
    }
)

EXPECTED_MUTATION_ENDPOINTS = frozenset(
    {
        "write_intent_comment",
        "completion_receipt_comment",
        "advisory_progress_hint",
        "pr_review_publish",
        "companion_prose_review_publish",
        "review_thread_mutation",
        "check_run_write",
        "pr_approval_publish",
    }
)


class V6CredentialMatrixTests(unittest.TestCase):
    """Verify v6 dual-credential rules, least privilege, and exhaustive matrix."""

    def test_matrix_contains_expected_endpoints_and_is_immutable(self) -> None:
        self.assertIsInstance(V6_ENDPOINT_CREDENTIAL_MATRIX, MappingProxyType)
        validate_v6_matrix_least_privilege()

        bulk_ids = {
            endpoint_id
            for endpoint_id, rule in V6_ENDPOINT_CREDENTIAL_MATRIX.items()
            if rule.operation_kind == OperationKind.BULK_READ
        }
        broker_read_ids = {
            endpoint_id
            for endpoint_id, rule in V6_ENDPOINT_CREDENTIAL_MATRIX.items()
            if rule.operation_kind == OperationKind.BROKER_AUTHORITY_READ
        }
        mutation_ids = {
            endpoint_id
            for endpoint_id, rule in V6_ENDPOINT_CREDENTIAL_MATRIX.items()
            if rule.operation_kind == OperationKind.MUTATION
        }

        self.assertEqual(bulk_ids, EXPECTED_BULK_READ_ENDPOINTS)
        self.assertEqual(broker_read_ids, EXPECTED_BROKER_AUTHORITY_READ_ENDPOINTS)
        self.assertEqual(mutation_ids, EXPECTED_MUTATION_ENDPOINTS)

        with self.assertRaises(TypeError):
            V6_ENDPOINT_CREDENTIAL_MATRIX["unexpected"] = V6_ENDPOINT_CREDENTIAL_MATRIX[  # type: ignore[index]
                "pr_metadata"
            ]

        sample_rule = get_v6_endpoint_rule("pr_metadata")
        with self.assertRaises(FrozenInstanceError):
            sample_rule.allow_app_fallback = True  # type: ignore[misc]

    def test_broker_app_read_allowlist_is_strictly_five_non_paginated_reads(
        self,
    ) -> None:
        self.assertEqual(len(BROKER_APP_READ_ALLOWLIST), 5)
        self.assertEqual(
            frozenset(BROKER_APP_READ_ALLOWLIST),
            EXPECTED_BROKER_AUTHORITY_READ_ENDPOINTS,
        )
        for endpoint_id in BROKER_APP_READ_ALLOWLIST:
            rule = get_v6_endpoint_rule(endpoint_id)
            with self.subTest(endpoint_id=endpoint_id):
                self.assertEqual(
                    rule.operation_kind, OperationKind.BROKER_AUTHORITY_READ
                )
                self.assertEqual(rule.credential_kind, CredentialKind.APP_BROKER)
                self.assertEqual(rule.method, "GET")
                self.assertFalse(rule.allow_app_fallback)
                self.assertIsNone(rule.max_items_per_page)

    def test_least_privilege_scopes_never_require_contents_write(self) -> None:
        self.assertEqual(
            WORKFLOW_READ_ONLY_SCOPES,
            frozenset(
                {
                    "contents: read",
                    "pull-requests: read",
                    "issues: read",
                    "checks: read",
                    "actions: read",
                }
            ),
        )
        for endpoint_id, rule in V6_ENDPOINT_CREDENTIAL_MATRIX.items():
            with self.subTest(endpoint_id=endpoint_id):
                self.assertNotIn("contents: write", rule.required_permissions)
                self.assertFalse(rule.allow_app_fallback)
                if rule.credential_kind == CredentialKind.WORKFLOW_GITHUB_TOKEN:
                    self.assertEqual(rule.operation_kind, OperationKind.BULK_READ)
                    self.assertTrue(rule.required_permissions)
                    for scope in rule.required_permissions:
                        self.assertIn(scope, WORKFLOW_READ_ONLY_SCOPES)
                        self.assertFalse(scope.endswith(": write"))

    def test_exhaustive_endpoint_event_visibility_credential_matrix(self) -> None:
        all_events = tuple(GitHubEventKind)
        all_visibilities = tuple(RepositoryVisibility)
        self.assertEqual(
            set(all_events),
            {
                GitHubEventKind.PULL_REQUEST,
                GitHubEventKind.ISSUE_COMMENT,
                GitHubEventKind.WORKFLOW_DISPATCH,
            },
        )
        self.assertEqual(
            set(all_visibilities),
            {
                RepositoryVisibility.PUBLIC,
                RepositoryVisibility.PRIVATE,
                RepositoryVisibility.FORK_PULL_REQUEST,
            },
        )

        for endpoint_id, rule in V6_ENDPOINT_CREDENTIAL_MATRIX.items():
            for event in all_events:
                for visibility in all_visibilities:
                    with self.subTest(
                        endpoint_id=endpoint_id,
                        event=event.value,
                        visibility=visibility.value,
                    ):
                        # Opposite credential always fails closed.
                        disallowed_cred = (
                            CredentialKind.APP_BROKER
                            if rule.credential_kind
                            == CredentialKind.WORKFLOW_GITHUB_TOKEN
                            else CredentialKind.WORKFLOW_GITHUB_TOKEN
                        )
                        with self.assertRaises(ReviewInputError) as wrong_cred:
                            authorize_v6_endpoint_credential(
                                endpoint_id,
                                disallowed_cred,
                                event=event,
                                visibility=visibility,
                                granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
                            )
                        if rule.operation_kind == OperationKind.BULK_READ:
                            self.assertEqual(
                                wrong_cred.exception.diagnostic,
                                "v6_app_fallback_forbidden",
                            )
                            self.assertIn(
                                "forbidden", str(wrong_cred.exception).lower()
                            )
                        else:
                            self.assertEqual(
                                wrong_cred.exception.diagnostic,
                                "v6_workflow_token_forbidden",
                            )

                        # Intended credential respects supported events/visibilities.
                        expected_allowed = (
                            event in rule.supported_events
                            and visibility in rule.supported_visibilities
                        )
                        if expected_allowed:
                            authorized = authorize_v6_endpoint_credential(
                                endpoint_id,
                                rule.credential_kind,
                                event=event,
                                visibility=visibility,
                                granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
                            )
                            self.assertIs(authorized, rule)
                        else:
                            with self.assertRaises(ReviewInputError):
                                authorize_v6_endpoint_credential(
                                    endpoint_id,
                                    rule.credential_kind,
                                    event=event,
                                    visibility=visibility,
                                    granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
                                )

    def test_missing_workflow_scopes_fail_closed_with_structured_403(self) -> None:
        for endpoint_id, rule in V6_ENDPOINT_CREDENTIAL_MATRIX.items():
            if rule.operation_kind != OperationKind.BULK_READ:
                continue
            for missing_scope in rule.required_permissions:
                granted = sorted(WORKFLOW_READ_ONLY_SCOPES - {missing_scope})
                for event in rule.supported_events:
                    for visibility in rule.supported_visibilities:
                        with self.subTest(
                            endpoint_id=endpoint_id,
                            missing_scope=missing_scope,
                            event=event.value,
                            visibility=visibility.value,
                        ):
                            with self.assertRaises(GitHubHTTPError) as caught:
                                authorize_v6_endpoint_credential(
                                    endpoint_id,
                                    CredentialKind.WORKFLOW_GITHUB_TOKEN,
                                    event=event,
                                    visibility=visibility,
                                    granted_scopes=granted,
                                )
                            err = caught.exception
                            self.assertIsInstance(err, V6CredentialPermissionError)
                            self.assertEqual(err.http_status, 403)
                            self.assertEqual(err.failed_lookup, endpoint_id)
                            self.assertIn(endpoint_id, str(err))
                            self.assertIn(
                                CredentialKind.WORKFLOW_GITHUB_TOKEN.value, str(err)
                            )
                            self.assertIn(missing_scope, str(err))

    def test_unknown_endpoint_and_invalid_rules_fail_closed(self) -> None:
        with self.assertRaises(ReviewInputError) as unknown_err:
            authorize_v6_endpoint_credential(
                "nonexistent_endpoint",
                CredentialKind.WORKFLOW_GITHUB_TOKEN,
                event=GitHubEventKind.PULL_REQUEST,
                visibility=RepositoryVisibility.PUBLIC,
                granted_scopes=WORKFLOW_READ_ONLY_SCOPES,
            )
        self.assertEqual(unknown_err.exception.diagnostic, "v6_unknown_endpoint")

        with self.assertRaises(ValueError):
            EndpointCredentialRule(
                endpoint_id="bad_fallback",
                surface="/repos/{owner}/{repo}/issues/1/comments",
                method="GET",
                operation_kind=OperationKind.BULK_READ,
                credential_kind=CredentialKind.WORKFLOW_GITHUB_TOKEN,
                required_permissions=("issues: read",),
                supported_events=frozenset({GitHubEventKind.PULL_REQUEST}),
                supported_visibilities=frozenset({RepositoryVisibility.PUBLIC}),
                allow_app_fallback=True,
            )

        with self.assertRaises(ValueError):
            EndpointCredentialRule(
                endpoint_id="bad_contents_write",
                surface="/repos/{owner}/{repo}/contents/file",
                method="PUT",
                operation_kind=OperationKind.MUTATION,
                credential_kind=CredentialKind.APP_BROKER,
                required_permissions=("contents: write",),
                supported_events=frozenset({GitHubEventKind.PULL_REQUEST}),
                supported_visibilities=frozenset({RepositoryVisibility.PUBLIC}),
            )

        with self.assertRaises(ValueError):
            EndpointCredentialRule(
                endpoint_id="bad_workflow_write",
                surface="/repos/{owner}/{repo}/issues/1/comments",
                method="GET",
                operation_kind=OperationKind.BULK_READ,
                credential_kind=CredentialKind.WORKFLOW_GITHUB_TOKEN,
                required_permissions=("issues: write",),
                supported_events=frozenset({GitHubEventKind.PULL_REQUEST}),
                supported_visibilities=frozenset({RepositoryVisibility.PUBLIC}),
            )

        with self.assertRaises(ValueError):
            validate_v6_matrix_least_privilege(
                {"pr_metadata": get_v6_endpoint_rule("pr_metadata")}
            )

    def test_wrangler_keeps_v5_pinned_and_v6_docs_links_resolve(self) -> None:
        wrangler = json.loads(
            (ROOT / "deploy" / "cloudflare" / "wrangler.jsonc").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(wrangler["vars"]["PUBLIC_WORKFLOW_TAG"], "v5")

        proposal_path = ROOT / "docs" / "proposals" / "v6-github-governed-recovery.md"
        adr_path = ROOT / "docs" / "adr" / "0144-github-governed-recovery.md"
        adr_index_path = ROOT / "docs" / "adr" / "README.md"

        self.assertTrue(proposal_path.is_file())
        self.assertTrue(adr_path.is_file())
        self.assertIn(
            "[0144](0144-github-governed-recovery.md)",
            adr_index_path.read_text(encoding="utf-8"),
        )

        link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
        for doc_path in (proposal_path, adr_path):
            content = doc_path.read_text(encoding="utf-8")
            self.assertNotIn("file://", content)
            self.assertNotIn("/Users/", content)
            for target in link_pattern.findall(content):
                if target.startswith(("http://", "https://", "#")):
                    continue
                resolved = (doc_path.parent / target.split("#", 1)[0]).resolve()
                self.assertTrue(
                    resolved.exists(),
                    f"broken relative link {target!r} in {doc_path}",
                )


if __name__ == "__main__":
    unittest.main()
