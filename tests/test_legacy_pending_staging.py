"""Legacy multipage receipts share the fenced, bounded staging contract."""

from __future__ import annotations

from dataclasses import replace

from tests import test_v5_pending_staging as staging


class LegacyPendingStagingTests(staging.V5PendingStagingTests):
    """All lost-write, ownership, body and freshness controls apply unchanged."""

    def setUp(self):
        super().setUp()
        self.state.eligibility = replace(
            self.state.eligibility, human_inventory_reference=None
        )
        self.state.reviews = [self.state.review(self.state.eligibility, 20)]
        self.expected_roots = [dict(item) for item in self.state.reviews]

    def test_legacy_receipts_without_redundant_association_field_still_reconcile(self):
        # Existing legacy operation ports return receipts from the scoped list
        # endpoint without redundant issue_url. A present URL must still match
        # (the inherited foreign-receipt test covers that closed boundary).
        opener = self.state.http.opener

        def legacy_port(request, timeout):
            response = opener(request, timeout)
            if request.method == "POST":
                for receipt in self.state.replies:
                    receipt.pop("issue_url", None)
            return response

        self.state.http.opener = legacy_port
        outcome = self.publish()
        self.assertEqual(outcome.status, "replied")
        self.assertEqual(self.state.issue_scans, 2)
        before = self.state.post_attempts
        self.assertEqual(self.publish().status, "already_replied")
        self.assertEqual(self.state.post_attempts, before)
        self.assertEqual(self.budget.calls, len(self.state.calls))
