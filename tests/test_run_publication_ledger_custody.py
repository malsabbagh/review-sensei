"""Run-owned wrapper reuse never crosses a changed native authority."""

import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from review_sensei_github.application import GitHubApplication
from review_sensei_github.errors import GitHubPublicationError

from review_sensei.errors import ReviewInputError
from review_sensei.session import SessionLoadResult
from tests import test_api_work_lifecycle as fixtures


class RunPublicationLedgerCustodyTests(unittest.TestCase):
    def test_broker_boundary_root_and_copied_source_mutations_refuse_business(self):
        for fault in (
            "external_root",
            "copied_source",
            "shadowed_load",
            "record_bytes",
        ):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                captured = []
                mutations = []
                fresh_loads = []
                constructor = GitHubApplication.__init__
                external = fixtures.LifecycleFixture._external_open

                def construct(app, *args, **kwargs):
                    constructor(app, *args, **kwargs)
                    captured.append(app)

                def reject_load(identity, **kwargs):
                    fresh_loads.append(identity)
                    return SessionLoadResult(status="conflict", detail="foreign source")

                def boundary(fixture, request, timeout):
                    response = external(fixture, request, timeout)
                    body = json.loads(request.data) if request.data else {}
                    if (
                        urlsplit(request.full_url).hostname == "broker.github.test"
                        and body.get("capability") == "review_publish"
                    ):
                        self.assertFalse(fixture.reviews)
                        self.assertFalse(fixture.check_runs)
                        app = captured[-1]
                        if fault == "external_root":
                            fixture.comment["body"] = (
                                "externally replaced selected root"
                            )
                        elif fault == "copied_source":
                            forged = copy.copy(app.session_ledger)
                            forged.load = reject_load
                            app.session_ledger = forged
                        elif fault == "shadowed_load":
                            app.session_ledger.load = reject_load
                        elif fault == "record_bytes":
                            record = app.session_ledger.caller._effect_record
                            self.assertIsNotNone(record.api_run_accounting)
                            record.api_run_accounting["unexpected_field"] = True
                        mutations.append(fault)
                    return response

                with (
                    patch.object(GitHubApplication, "__init__", construct),
                    patch.object(fixtures.LifecycleFixture, "_external_open", boundary),
                ):
                    fixture = fixtures.LifecycleFixture(directory)
                    native, config, _, owner = fixture.prelude()
                    fixture.accounted.initialize_and_activate(
                        native, fixtures.IDENTITY, config, owner
                    )
                    with self.assertRaises((ReviewInputError, GitHubPublicationError)):
                        fixtures.CallerLifecycleTests().journey(
                            directory, prepared=(fixture, native)
                        )
                self.assertEqual(mutations, [fault])
                self.assertEqual(fixture.reviews, [])
                # Failure reporting is an observation, never a successful gate.
                for check in fixture.check_runs:
                    self.assertEqual(check["conclusion"], "action_required")
                    self.assertEqual(
                        check["output"]["title"], "Review publication failed"
                    )
                self.assertEqual(fixture.inline, [])
                if fault in {"copied_source", "shadowed_load"}:
                    self.assertTrue(fresh_loads)
                if fault == "external_root":
                    self.assertEqual(
                        fixture.comment["body"], "externally replaced selected root"
                    )


if __name__ == "__main__":
    unittest.main()
