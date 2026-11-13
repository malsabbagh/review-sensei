"""Original source seals cannot be minted from supplied modules or late calls."""

import types
import unittest
from unittest.mock import patch

from review_sensei import disposition
from review_sensei.errors import ReviewInputError
from review_sensei.source_registry import require_module_seal, seal_module


class SourceRegistryTests(unittest.TestCase):
    def test_real_module_read_only_seal_and_late_registration_refusal(self):
        require_module_seal(disposition)
        with self.assertRaisesRegex(ReviewInputError, "defining module"):
            seal_module()

    def test_copied_module_or_late_callable_cannot_supply_original_source(self):
        copied = types.ModuleType(disposition.__name__)
        copied.__dict__.update(disposition.__dict__)
        with self.assertRaisesRegex(ReviewInputError, "unavailable"):
            require_module_seal(copied)
        with patch.object(disposition, "apply_session_command", lambda *a, **k: None):
            with self.assertRaisesRegex(ReviewInputError, "source changed"):
                require_module_seal(disposition)
        require_module_seal(disposition)

    def test_original_ledger_method_consumer_is_read_only_and_rejects_shadow(self):
        from review_sensei_github.review_run import ActionRunLedger, ReviewRunLedger
        from review_sensei_github.session_ledger import GitHubIssueCommentSessionLedger

        from review_sensei.source_registry import require_instance_source

        methods = ("load", "read_evidence", "replace", "evidence_producer")
        for cls in (GitHubIssueCommentSessionLedger, ActionRunLedger, ReviewRunLedger):
            # This checks executable source only, not initialized data authority.
            ledger = object.__new__(cls)
            require_instance_source(ledger, methods=methods)
            with patch.object(ledger, "load", lambda *a, **k: None):
                with self.assertRaisesRegex(
                    ReviewInputError, "instance method changed"
                ):
                    require_instance_source(ledger, methods=methods)
