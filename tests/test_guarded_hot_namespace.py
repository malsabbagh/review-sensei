"""Negative hook compatibility; no projection, effect or budget grant."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request

from fake_github_http import FakeHTTPResponse
from review_sensei_github import api_run_requests, guarded_hot_namespace
from review_sensei_github.api_run_requests import ApiRunRequestTransport
from test_api_work_persistence import PLAN, RUN

from review_sensei.errors import ReviewInputError


class GuardedHotNamespaceTests(unittest.TestCase):
    def test_accounting_accesses_match_original_model_contract(self):
        import ast
        import dataclasses
        import inspect
        import textwrap

        from review_sensei.api_work import ApiRunAccounting

        require = inspect.getclosurevars(guarded_hot_namespace.begin).nonlocals[
            "require"
        ]
        fields = {item.name for item in dataclasses.fields(ApiRunAccounting)}
        for function in (guarded_hot_namespace.begin, require):
            tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Attribute):
                    continue
                direct = (
                    isinstance(node.value, ast.Attribute)
                    and node.value.attr == "accounting"
                )
                local = isinstance(node.value, ast.Name) and node.value.id == "state"
                if direct or local:
                    self.assertTrue(
                        node.attr in fields or hasattr(ApiRunAccounting, node.attr),
                        node.attr,
                    )
        self.assertIsInstance(ApiRunAccounting.remaining, property)

    def caller(self, directory, sent):
        def physical(request, timeout):
            sent.append(request.full_url)
            return FakeHTTPResponse(b"{}")

        return ApiRunRequestTransport(
            directory=Path(directory),
            run=RUN,
            plan=PLAN,
            attempt=1,
            opener=physical,
            clock=lambda: 10.0,
        )

    def test_inactive_original_open_keeps_one_dispatch_and_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            sent = []
            caller = self.caller(directory, sent)
            with caller.open(Request("https://fixture.test"), timeout=1) as response:
                self.assertEqual(response.read(100), b"{}")
            self.assertEqual(sent, ["https://fixture.test"])
            self.assertEqual(caller._sequence, 1)
            self.assertEqual(caller.startup_dispatches, 1)
            self.assertEqual(caller.observations[0]["deadline_unix_ms"], 11000)

    def test_replaced_alias_or_export_refuses_before_callback_sequence_or_dispatch(
        self,
    ):
        for module, name in (
            (api_run_requests, "_hot_before_sequence"),
            (guarded_hot_namespace, "before_sequence"),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                sent = []
                caller = self.caller(directory, sent)
                with patch.object(module, name, return_value=None) as fake:
                    with self.assertRaises(ReviewInputError):
                        caller.open(Request("https://fixture.test"), timeout=1)
                    fake.assert_not_called()
                self.assertEqual(sent, [])
                self.assertEqual(caller._sequence, 0)
                self.assertEqual(caller.startup_dispatches, 0)
                self.assertEqual(caller.observations, [])

    def test_external_installation_and_producer_calls_do_not_issue_custody(self):
        with self.assertRaises(ReviewInputError):
            guarded_hot_namespace.install_caller(ApiRunRequestTransport)
        from review_sensei_github.publication import ReviewPublisher

        with self.assertRaises(ReviewInputError):
            guarded_hot_namespace.begin(object.__new__(ReviewPublisher), None, None)

    def test_generic_direct_grouped_publisher_remains_inactive_without_application(
        self,
    ):
        import sys
        from types import SimpleNamespace

        from review_sensei_github.publication_recovery import _publication_transport
        from test_default_human_inventory_publisher import (
            DefaultHumanInventoryPublisherTests,
        )

        fixture = DefaultHumanInventoryPublisherTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        from default_human_inventory_contract import IDENTITY
        from review_sensei_github.checkpoint_result_store import CheckpointResultStore
        from review_sensei_github.publication_recovery import _event_key

        from review_sensei.bounded_evidence import canonical_bytes

        CheckpointResultStore(
            fixture.store.port,
            app_user_id=fixture.context.producer_id,
            http=fixture.http,
            identity=IDENTITY,
            transaction=fixture.result.transaction,
            compact_write=False,
            app_id=None,
        ).write(
            event_key=_event_key(IDENTITY, fixture.result.transaction),
            result=canonical_bytes(fixture.result.to_dict()),
        )
        transport = _publication_transport(fixture.http)
        transport._quarantine_capacity_guard = SimpleNamespace(
            admit_grouped_publication=lambda **kwargs: None,
            admit_comment_batch=lambda *args, **kwargs: None,
            native_old_root_nodes={},
            before_request=lambda *args: None,
            after_response=lambda *args: None,
        )
        returned = []

        class ObservedInactive(Exception):
            pass

        def observe(frame, event, value):
            if (
                frame.f_code is guarded_hot_namespace.begin.__code__
                and event == "return"
            ):
                self.assertIsNone(frame.f_locals["journal"])
                self.assertIsNone(value)
                returned.append(True)
                raise ObservedInactive()

        previous = sys.getprofile()
        try:
            with patch(
                "review_sensei_github.checkpoint_result_store.uses_grouped_publication",
                return_value=True,
            ):
                sys.setprofile(observe)
                with self.assertRaises(ObservedInactive):
                    fixture.publish()
        finally:
            sys.setprofile(previous)
        self.assertEqual(returned, [True])

    def test_forged_loader_never_runs_before_or_after_registration(self):
        import subprocess
        import sys

        script = r"""
import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
import sys, types
from review_sensei_github import guarded_hot_namespace as original
from review_sensei_github import api_run_requests
from review_sensei.errors import ReviewInputError
calls = []
name = original.__name__
path = original.__file__
# Load the actual original module with its original loader, but without any
# API/application/run registration; no fabricated source or installer grant.
spec = importlib.util.spec_from_file_location(name, path)
spec._initializing = True
fresh = importlib.util.module_from_spec(spec)
sys.modules[name] = fresh
spec.loader.exec_module(fresh)
target = "review_sensei_github.api_run_requests"
expected = str(Path(path).with_name("api_run_requests.py"))
original_api = sys.modules[target]
class DescriptorBomb:
    def __getattr__(self, name):
        calls.append(name)
        raise AssertionError("descriptor callback")
class FakeLoader:
    get_code = DescriptorBomb()
for hooks in (fresh, original):
    for kind in ("foreign", "shadow", "wrong-path", "descriptor"):
        original_descriptor = SourceFileLoader.get_code
        loader = FakeLoader() if kind == "foreign" else SourceFileLoader(target, expected)
        if kind == "shadow":
            loader.get_code = lambda name: calls.append(name)
        if kind == "wrong-path":
            loader.path = str(Path(expected).with_name("wrong.py"))
        forged = types.ModuleType(target)
        forged.__file__ = expected
        forged.__spec__ = importlib.util.spec_from_file_location(target, expected, loader=loader)
        forged.__spec__._initializing = True
        if kind == "descriptor":
            SourceFileLoader.get_code = DescriptorBomb()
        forged.install = hooks.install_caller
        forged.ApiRunRequestTransport = api_run_requests.ApiRunRequestTransport
        sys.modules[target] = forged
        try:
            exec(compile("install(ApiRunRequestTransport)", expected, "exec"), vars(forged))
        except ReviewInputError:
            pass
        else:
            raise AssertionError("forged source registered")
        finally:
            if kind == "descriptor":
                del SourceFileLoader.get_code
            sys.modules[target] = original_api
        assert calls == [], calls
sys.modules[name] = original
"""
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class GuardedHotNamespaceEligibleTests(unittest.TestCase):
    def test_genuine_eligible_journal_export_substitution_refuses_before_effects(self):
        import copy
        import sys

        from hot_original_entry import invoke_original
        from review_sensei_github import finding_lifecycle
        from review_sensei_github.quarantine_published_tail import BROKER_QUALIFICATIONS
        from test_quarantine_activation import registered_resource
        from test_quarantine_published_tail_journey import (
            NativePublishedTailJourneyFixture,
        )

        original = finding_lifecycle.FindingLifecycleJournal
        observed = []
        replacement = object()

        def observe(frame, event, value):
            if frame.f_code is not guarded_hot_namespace.begin.__code__:
                return
            if event == "call":
                journal = frame.f_locals["journal"]
                self.assertIs(type(journal), original)
                self.assertEqual(journal.effects, [])
                self.assertTrue(journal.ledger._quarantine_required)
                self.assertIsNotNone(journal._record.quarantine_lineage)
                self.assertEqual(
                    journal.ledger.caller.accounting.configuration.limit, 512
                )
                observed.append(True)
                finding_lifecycle.FindingLifecycleJournal = replacement
            elif event == "return":
                finding_lifecycle.FindingLifecycleJournal = original

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(BROKER_QUALIFICATIONS, {}, clear=True),
        ):
            fixture = NativePublishedTailJourneyFixture(directory)
            fixture.api_url = "https://api.github.com"
            old, registration = fixture.original_origins()
            old_accounting = copy.deepcopy(old.api_run_accounting)
            reviews = copy.deepcopy(fixture.reviews)
            checks = copy.deepcopy(fixture.check_runs)
            fixture.provider_comments = [
                {
                    "path": "src/app.py",
                    "line": 3,
                    "side": "RIGHT",
                    "body": "Current published assessment requires human judgment.",
                    "severity": "high",
                    "blocking": False,
                    "needs_human": True,
                }
            ]
            previous = sys.getprofile()
            try:
                with registered_resource([registration]):
                    sys.setprofile(observe)
                    result, _wire = invoke_original(fixture)
            finally:
                sys.setprofile(previous)
                finding_lifecycle.FindingLifecycleJournal = original
            self.assertEqual(observed, [True])
            self.assertEqual(result, 1)
            self.assertIn(
                "original module source changed: FindingLifecycleJournal", fixture.error
            )
            self.assertEqual(fixture.reviews, reviews)
            self.assertEqual(fixture.check_runs, checks)
            self.assertEqual(old.api_run_accounting, old_accounting)
