"""Tests for benign PR/base/policy lifecycle races, terminal reruns, and duplicate reply status."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from fake_github_http import json_response
from review_sensei_github import cli as package_cli
from review_sensei_github.review_guidance import (
    _BENIGN_RACE_NOTICES,
    write_benign_race_step_summary,
)
from review_sensei_github.review_run import _is_benign_terminal_rerun
from review_sensei_github.session_ledger import parse_session_comment
from test_action_reply_entry import ReplyEntryFixture
from test_action_run import CommandEntryFixture
from test_api_work_persistence import BASE, HEAD, IDENTITY
from test_policy_commit_drift_g21 import _advance_remote_default_branch
from test_review_lifecycle_diagnostics import outcome as lifecycle_outcome
from test_review_run import EntryFixture

from review_sensei.api_work import (
    ApiRunAccounting,
    ApiRunIdentity,
    CallerRequestBlock,
    PendingApiRequest,
)
from review_sensei.errors import (
    BENIGN_LIFECYCLE_RACE_REASONS,
    BenignLifecycleRace,
    ReviewInputError,
)
from review_sensei.models import ReviewTransaction


def _wrap_cli_main_adding_skip_flag(
    *,
    target_subcommand: str,
    step_summary: Path | None = None,
):
    """Inject ``--skip-benign-races`` (and optional step summary env) into a subcommand call."""
    original_main = package_cli.main

    def wrapped(argv=None):
        args = list(argv or [])
        if args and args[0] == target_subcommand and "--" in args:
            sep = args.index("--")
            if "--skip-benign-races" not in args[:sep]:
                args = [*args[:sep], "--skip-benign-races", *args[sep:]]
        if step_summary is None:
            return original_main(args)
        had_summary = "GITHUB_STEP_SUMMARY" in os.environ
        previous_summary = os.environ.get("GITHUB_STEP_SUMMARY")
        os.environ["GITHUB_STEP_SUMMARY"] = str(step_summary)
        try:
            return original_main(args)
        finally:
            if had_summary and previous_summary is not None:
                os.environ["GITHUB_STEP_SUMMARY"] = previous_summary
            else:
                os.environ.pop("GITHUB_STEP_SUMMARY", None)

    return patch.object(package_cli, "main", side_effect=wrapped)


class _RaceEntryFixture(EntryFixture):
    def __init__(self, directory: str) -> None:
        super().__init__(directory)
        self.pull_overrides: dict[str, object] = {}
        self.policy_ref_override: dict[str, object] | None = None
        self.store_pull_override: dict[str, object] | None = None
        self.store_pull_status: int = 200
        self._in_publication = False

    def external(self, request, timeout):
        path = urlsplit(request.full_url).path
        if (
            self.policy_ref_override is not None
            and request.get_method() == "GET"
            and path == "/repos/owner/repo/git/ref/heads/main"
        ):
            self.trace.append((self.attempt, request.get_method(), path))
            return json_response(self.policy_ref_override)
        response = super().external(request, timeout)
        if request.get_method() == "GET" and path.endswith("/pulls/7"):
            if self._in_publication and self.store_pull_status != 200:
                return json_response({"message": "unavailable"}, self.store_pull_status)
            pull = json.loads(response.read(524288))
            overrides = (
                self.store_pull_override
                if self._in_publication and self.store_pull_override is not None
                else self.pull_overrides
            )
            for key, value in overrides.items():
                if isinstance(value, dict) and isinstance(pull.get(key), dict):
                    merged = copy.deepcopy(pull[key])
                    for sub_k, sub_v in value.items():
                        if isinstance(sub_v, dict) and isinstance(
                            merged.get(sub_k), dict
                        ):
                            merged[sub_k].update(sub_v)
                        else:
                            merged[sub_k] = sub_v
                    pull[key] = merged
                else:
                    pull[key] = copy.deepcopy(value)
            return json_response(pull)
        return response


class _RaceReplyFixture(ReplyEntryFixture):
    def __init__(self, directory: str) -> None:
        super().__init__(directory)
        self.pull_overrides: dict[str, object] = {}
        self.policy_ref_override: dict[str, object] | None = None

    def external(self, request, timeout):
        path = urlsplit(request.full_url).path
        if (
            self.policy_ref_override is not None
            and request.get_method() == "GET"
            and path == "/repos/owner/repo/git/ref/heads/main"
        ):
            self.trace.append((self.attempt, request.get_method(), path))
            return json_response(self.policy_ref_override)
        response = super().external(request, timeout)
        if request.get_method() == "GET" and path.endswith("/pulls/7"):
            pull = json.loads(response.read(524288))
            for key, value in self.pull_overrides.items():
                if isinstance(value, dict) and isinstance(pull.get(key), dict):
                    merged = copy.deepcopy(pull[key])
                    for sub_k, sub_v in value.items():
                        if isinstance(sub_v, dict) and isinstance(
                            merged.get(sub_k), dict
                        ):
                            merged[sub_k].update(sub_v)
                        else:
                            merged[sub_k] = sub_v
                    pull[key] = merged
                else:
                    pull[key] = copy.deepcopy(value)
            return json_response(pull)
        return response


class _RaceCommandFixture(CommandEntryFixture):
    def __init__(self, directory: str) -> None:
        super().__init__(directory)
        self.pull_overrides: dict[str, object] = {}
        self.policy_ref_override: dict[str, object] | None = None

    def external(self, request, timeout):
        path = urlsplit(request.full_url).path
        if (
            self.policy_ref_override is not None
            and request.get_method() == "GET"
            and path == "/repos/owner/repo/git/ref/heads/main"
        ):
            self.trace.append((self.attempt, request.get_method(), path))
            return json_response(self.policy_ref_override)
        response = super().external(request, timeout)
        if request.get_method() == "GET" and path.endswith("/pulls/7"):
            pull = json.loads(response.read(524288))
            for key, value in self.pull_overrides.items():
                if isinstance(value, dict) and isinstance(pull.get(key), dict):
                    merged = copy.deepcopy(pull[key])
                    for sub_k, sub_v in value.items():
                        if isinstance(sub_v, dict) and isinstance(
                            merged.get(sub_k), dict
                        ):
                            merged[sub_k].update(sub_v)
                        else:
                            merged[sub_k] = sub_v
                    pull[key] = merged
                else:
                    pull[key] = copy.deepcopy(value)
            return json_response(pull)
        return response


class BenignLifecycleRacesTests(unittest.TestCase):
    def test_exception_contract_and_reason_validation(self) -> None:
        err = BenignLifecycleRace(
            "trusted policy commit changed before admission",
            reason="policy_commit_superseded",
        )
        self.assertIsInstance(err, ReviewInputError)
        self.assertEqual(err.reason, "policy_commit_superseded")
        self.assertIsNone(err.reply_status)
        with self.assertRaises(ValueError):
            BenignLifecycleRace("bad", reason="not_a_real_reason")
        self.assertEqual(set(_BENIGN_RACE_NOTICES), set(BENIGN_LIFECYCLE_RACE_REASONS))
        with self.assertRaises(ValueError):
            write_benign_race_step_summary(operation="review", reason="unknown_reason")

    def test_restart_run_rejects_skip_benign_races_flag(self) -> None:
        with self.assertRaises(SystemExit) as exited:
            package_cli.main(
                [
                    "restart-run",
                    "--policy-commit",
                    "a" * 40,
                    "--run-directory",
                    "/tmp",
                    "--publication-outcome",
                    "/tmp/out.json",
                    "--restart-id",
                    "b" * 64,
                    "--skip-benign-races",
                    "--",
                ]
            )
        self.assertEqual(exited.exception.code, 2)

    def test_site1_policy_commit_superseded_skips_across_lanes_with_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _RaceEntryFixture(directory)
            summary = Path(directory) / "step-summary.md"
            frozen_p = fixture.policy
            live_p = _advance_remote_default_branch(
                fixture.root,
                frozen_commit=frozen_p,
                filename="README.md",
                content=b"# updated\n",
                message="docs: advance",
            )
            fixture.policy = frozen_p
            fixture.policy_ref = live_p

            # Without flag: fails with exit 1.
            self.assertEqual(fixture.invoke(), 1, fixture.error)
            self.assertIn(
                "trusted policy commit changed before admission", fixture.error
            )

            # With --skip-benign-races (new run_id so startup slot is unused): exits 0.
            fixture.run_id += 1
            fixture.trace.clear()
            fixture.transports.clear()
            with _wrap_cli_main_adding_skip_flag(
                target_subcommand="review-run", step_summary=summary
            ):
                code = fixture.invoke()
            self.assertEqual(code, 0, fixture.error)
            self.assertEqual(fixture.provider_calls, 0)
            self.assertIsNone(fixture.comment)
            out = json.loads((fixture.root / "publication.json").read_text())
            self.assertEqual(out["status"], "skipped_stale")
            self.assertEqual(out["reason"], "policy_commit_superseded")
            self.assertFalse(out["accounting_terminal"])
            diag = lifecycle_outcome(self, fixture.error)
            self.assertEqual(diag["reason"], "skipped_benign_race")
            self.assertEqual(diag["benign_race_reason"], "policy_commit_superseded")
            self.assertEqual(
                diag["scope"]["transition_reason"], "policy_commit_superseded"
            )
            self.assertEqual(diag["step"], "default_policy_ref")
            self.assertIn("policy_commit_superseded", summary.read_text())

        # Reply lane + Command lane with Site 1 benign race:
        with tempfile.TemporaryDirectory() as directory:
            reply_fx = _RaceReplyFixture(directory)
            frozen_p = reply_fx.policy
            live_p = _advance_remote_default_branch(
                reply_fx.root,
                frozen_commit=frozen_p,
                filename="README.md",
                content=b"# updated\n",
                message="docs: advance",
            )
            reply_fx.policy = frozen_p
            reply_fx.policy_ref = live_p
            with _wrap_cli_main_adding_skip_flag(target_subcommand="reply-run"):
                code = reply_fx.invoke_reply()
            self.assertEqual(code, 0, reply_fx.error)
            self.assertEqual(reply_fx.output.strip(), "skipped_stale_head")
            diag = lifecycle_outcome(self, reply_fx.error)
            self.assertEqual(diag["reason"], "skipped_benign_race")
            self.assertEqual(diag["step"], "default_policy_ref")

        with tempfile.TemporaryDirectory() as directory:
            cmd_fx = _RaceCommandFixture(directory)
            summary = Path(directory) / "cmd-summary.md"
            frozen_p = cmd_fx.policy
            live_p = _advance_remote_default_branch(
                cmd_fx.root,
                frozen_commit=frozen_p,
                filename="README.md",
                content=b"# updated\n",
                message="docs: advance",
            )
            cmd_fx.policy = frozen_p
            cmd_fx.policy_ref = live_p
            with _wrap_cli_main_adding_skip_flag(
                target_subcommand="command-run", step_summary=summary
            ):
                code = cmd_fx.invoke_command()
            self.assertEqual(code, 0, cmd_fx.error)
            self.assertIn("policy_commit_superseded", cmd_fx.stdout)
            self.assertIn("policy_commit_superseded", summary.read_text())

    def test_site2_before_admission_races_skip_cleanly_for_review_reply_and_command(
        self,
    ) -> None:
        cases = (
            (
                "closed",
                {"state": "closed"},
                "pr_closed_before_admission",
                "skipped_pr_state",
            ),
            ("draft", {"draft": True}, "pr_draft_before_admission", "skipped_pr_state"),
            (
                "superseded_head",
                {"head": {"sha": "e" * 40}},
                "pr_revision_superseded_before_admission",
                "skipped_stale_head",
            ),
            (
                "superseded_base",
                {"base": {"sha": "f" * 40}},
                "pr_revision_superseded_before_admission",
                "skipped_stale_head",
            ),
        )
        for label, overrides, expected_reason, expected_reply_token in cases:
            with (
                self.subTest(lane="review", case=label),
                tempfile.TemporaryDirectory() as d,
            ):
                fx = _RaceEntryFixture(d)
                fx.pull_overrides = overrides
                summary = Path(d) / "summary.md"
                with _wrap_cli_main_adding_skip_flag(
                    target_subcommand="review-run", step_summary=summary
                ):
                    code = fx.invoke()
                self.assertEqual(code, 0, fx.error)
                self.assertEqual(fx.provider_calls, 0)
                out = json.loads((fx.root / "publication.json").read_text())
                self.assertEqual(out["status"], "skipped_stale")
                self.assertEqual(out["reason"], expected_reason)
                self.assertFalse(out["accounting_terminal"])
                diag = lifecycle_outcome(self, fx.error)
                self.assertEqual(diag["reason"], "skipped_benign_race")
                self.assertEqual(diag["step"], "live_pr")
                self.assertIn(expected_reason, summary.read_text())

            with (
                self.subTest(lane="reply", case=label),
                tempfile.TemporaryDirectory() as d,
            ):
                rfx = _RaceReplyFixture(d)
                rfx.pull_overrides = overrides
                with _wrap_cli_main_adding_skip_flag(target_subcommand="reply-run"):
                    code = rfx.invoke_reply()
                self.assertEqual(code, 0, rfx.error)
                if label == "superseded_base":
                    # Reply omits --base-sha on CLI, so live base advance is adopted and reply proceeds.
                    self.assertEqual(rfx.output.strip(), "replied")
                    self.assertEqual(rfx.provider_calls, 1)
                    self.assertEqual(rfx.selected.base_sha, "f" * 40)
                else:
                    self.assertEqual(rfx.output.strip(), expected_reply_token)
                    diag = lifecycle_outcome(self, rfx.error)
                    self.assertEqual(diag["reason"], "skipped_benign_race")
                    self.assertEqual(diag["benign_race_reason"], expected_reason)
                    self.assertEqual(diag["step"], "live_pr")

        # Command lane closed PR before admission:
        with tempfile.TemporaryDirectory() as d:
            cfx = _RaceCommandFixture(d)
            cfx.pull_overrides = {"state": "closed"}
            summary = Path(d) / "cmd-summary.md"
            with _wrap_cli_main_adding_skip_flag(
                target_subcommand="command-run", step_summary=summary
            ):
                code = cfx.invoke_command()
            self.assertEqual(code, 0, cfx.error)
            self.assertIn("pr_closed_before_admission", cfx.stdout)
            self.assertIn("pr_closed_before_admission", summary.read_text())

    def test_site3_during_review_races_skip_without_fabricating_terminal_transaction(
        self,
    ) -> None:
        for label, overrides, expected_reason in (
            ("closed", {"state": "closed"}, "pr_closed_during_review"),
            (
                "head_moved",
                {"head": {"sha": "e" * 40}},
                "pr_revision_superseded_during_review",
            ),
            (
                "base_moved",
                {"base": {"sha": "f" * 40}},
                "pr_revision_superseded_during_review",
            ),
        ):
            with self.subTest(case=label), tempfile.TemporaryDirectory() as d:
                fx = _RaceEntryFixture(d)
                fx.store_pull_override = overrides
                fx.before_publication = lambda f=fx: setattr(f, "_in_publication", True)
                summary = Path(d) / "summary.md"
                with _wrap_cli_main_adding_skip_flag(
                    target_subcommand="review-run", step_summary=summary
                ):
                    code = fx.invoke()
                self.assertEqual(code, 0, fx.error)
                self.assertEqual(fx.provider_calls, 1)
                self.assertEqual(fx.reviews, [])
                out = json.loads((fx.root / "publication.json").read_text())
                self.assertEqual(out["status"], "skipped_stale")
                self.assertEqual(out["reason"], expected_reason)
                self.assertFalse(out["accounting_terminal"])
                diag = lifecycle_outcome(self, fx.error)
                self.assertEqual(diag["reason"], "skipped_benign_race")
                self.assertEqual(diag["benign_race_reason"], expected_reason)
                self.assertEqual(diag["step"], "publication")
                self.assertIn(expected_reason, summary.read_text())
                assert fx.comment is not None
                rec = parse_session_comment(fx.comment["body"], identity=IDENTITY)
                assert rec is not None and rec.api_run_accounting is not None
                self.assertIsNone(rec.api_run_accounting.get("terminal_transaction"))

    def test_site4_same_run_terminal_rerun_skips_cleanly_across_all_three_lanes(
        self,
    ) -> None:
        # 1. Review lane attempt=2 after terminal review:
        with tempfile.TemporaryDirectory() as d:
            fx = _RaceEntryFixture(d)
            self.assertEqual(fx.invoke(), 0, fx.error)
            self.assertEqual(fx.provider_calls, 1)
            fx.attempt = 2
            fx.trace.clear()
            fx.transports.clear()
            summary = Path(d) / "summary.md"
            with _wrap_cli_main_adding_skip_flag(
                target_subcommand="review-run", step_summary=summary
            ):
                code = fx.invoke()
            self.assertEqual(code, 0, fx.error)
            self.assertEqual(fx.provider_calls, 1)
            out = json.loads((fx.root / "publication.json").read_text())
            self.assertEqual(out["status"], "already_published")
            self.assertEqual(out["reason"], "caller_run_already_terminal")
            self.assertTrue(out["accounting_terminal"])
            diag = lifecycle_outcome(self, fx.error)
            self.assertEqual(diag["reason"], "skipped_benign_race")
            self.assertEqual(diag["step"], "predecessor_admission")
            self.assertIsNotNone(diag["predecessor"]["terminal_transaction"])
            self.assertIn("caller_run_already_terminal", summary.read_text())

        # 2. Reply lane attempt=2 after terminal reply:
        with tempfile.TemporaryDirectory() as d:
            rfx = _RaceReplyFixture(d)
            self.assertEqual(rfx.invoke_reply(), 0, rfx.error)
            self.assertEqual(rfx.provider_calls, 1)
            rfx.attempt = 2
            rfx.trace.clear()
            rfx.transports.clear()
            with _wrap_cli_main_adding_skip_flag(target_subcommand="reply-run"):
                code = rfx.invoke_reply()
            self.assertEqual(code, 0, rfx.error)
            self.assertEqual(rfx.provider_calls, 1)
            self.assertEqual(rfx.output.strip(), "already_replied")
            diag = lifecycle_outcome(self, rfx.error)
            self.assertEqual(diag["reason"], "skipped_benign_race")
            self.assertEqual(diag["step"], "predecessor_admission")
            self.assertIsNotNone(diag["predecessor"])

        # 3. Command lane attempt=2 after terminal command:
        with tempfile.TemporaryDirectory() as d:
            cfx = _RaceCommandFixture(d)
            self.assertEqual(cfx.invoke_command(), 0, cfx.error)
            cfx.attempt = 2
            cfx.trace.clear()
            cfx.transports.clear()
            summary = Path(d) / "cmd-summary.md"
            with _wrap_cli_main_adding_skip_flag(
                target_subcommand="command-run", step_summary=summary
            ):
                code = cfx.invoke_command()
            self.assertEqual(code, 0, cfx.error)
            self.assertIn("caller_run_already_terminal", cfx.stdout)
            self.assertIn("caller_run_already_terminal", summary.read_text())
            diag = lifecycle_outcome(self, cfx.error)
            self.assertEqual(diag["reason"], "skipped_benign_race")
            self.assertEqual(diag["step"], "predecessor_admission")

    def test_security_tamper_and_fail_closed_guards_still_exit_1_with_flag(
        self,
    ) -> None:
        # Site 1 security: malformed / non-commit / invalid SHA
        for label, ref_payload in (
            ("non_dict_obj", {"object": "not-a-dict"}),
            ("wrong_type", {"object": {"type": "tag", "sha": "a" * 40}}),
            ("invalid_sha", {"object": {"type": "commit", "sha": "not-40-hex"}}),
        ):
            with self.subTest(site=1, case=label), tempfile.TemporaryDirectory() as d:
                fx = _RaceEntryFixture(d)
                fx.policy_ref_override = ref_payload
                with _wrap_cli_main_adding_skip_flag(target_subcommand="review-run"):
                    code = fx.invoke()
                self.assertEqual(code, 1, fx.error)
                self.assertIn(
                    "trusted policy commit changed before admission", fx.error
                )

        # Site 2 security: fork / ownership / tamper / invalid state / invalid draft / invalid sha
        site2_cases = (
            ("fork_pr", {"head": {"repo": {"fork": True}}}),
            (
                "fork_even_when_closed",
                {"state": "closed", "head": {"repo": {"fork": True}}},
            ),
            ("head_repo_mismatch", {"head": {"repo": {"full_name": "other/repo"}}}),
            ("base_repo_id_mismatch", {"base": {"repo": {"id": 999}}}),
            (
                "base_repo_name_mismatch",
                {"base": {"repo": {"full_name": "other/repo"}}},
            ),
            ("pull_number_mismatch", {"number": 8}),
            ("bad_base_ref", {"base": {"ref": "bad..ref"}}),
            ("invalid_state", {"state": "merged"}),
            ("non_bool_draft", {"draft": "true"}),
            (
                "invalid_head_sha_even_when_closed",
                {"state": "closed", "head": {"sha": "bad"}},
            ),
        )
        for label, overrides in site2_cases:
            with self.subTest(site=2, case=label), tempfile.TemporaryDirectory() as d:
                fx = _RaceEntryFixture(d)
                fx.pull_overrides = overrides
                with _wrap_cli_main_adding_skip_flag(target_subcommand="review-run"):
                    code = fx.invoke()
                self.assertEqual(code, 1, fx.error)
                self.assertIn("live reviewed base or head conflicts", fx.error)

        # Site 3 security: HTTP != 200, wrong repo id/name, invalid state/sha
        # Force `_in_publication` after `api_run_host` head check (when publication_recovery runs)
        site3_cases = (
            ("http_500", 500, {}),
            ("repo_id_mismatch", 200, {"base": {"repo": {"id": 999}}}),
            (
                "repo_name_mismatch",
                200,
                {"base": {"repo": {"full_name": "other/repo"}}},
            ),
            ("invalid_state", 200, {"state": "merged"}),
            ("invalid_sha", 200, {"head": {"sha": "bad"}}),
        )
        for label, status_code, overrides in site3_cases:
            with self.subTest(site=3, case=label), tempfile.TemporaryDirectory() as d:
                fx = _RaceEntryFixture(d)
                fx.store_pull_status = status_code
                fx.store_pull_override = overrides
                fx.before_publication = lambda f=fx: setattr(f, "_in_publication", True)
                with _wrap_cli_main_adding_skip_flag(target_subcommand="review-run"):
                    code = fx.invoke()
                self.assertEqual(code, 1, fx.error)
                if label in {"http_500", "invalid_sha"}:
                    self.assertTrue(
                        "retention current pull request identity mismatches" in fx.error
                        or "caller workflow publication head changed" in fx.error,
                        fx.error,
                    )
                else:
                    self.assertIn(
                        "retention current pull request identity mismatches", fx.error
                    )

        # Site 4 security: mismatched transaction base/head -> fail closed!
        with tempfile.TemporaryDirectory() as d:
            fx = _RaceEntryFixture(d)
            self.assertEqual(fx.invoke(), 0, fx.error)
            fx.attempt = 2
            fx.live_head = fx.requested_head = "c" * 40
            fx.trace.clear()
            fx.transports.clear()
            with _wrap_cli_main_adding_skip_flag(target_subcommand="review-run"):
                code = fx.invoke()
            self.assertEqual(code, 1, fx.error)
            self.assertIn("caller run is already terminal", fx.error)

        # Site 4 fail-closed guard matrix: quarantine_lineage, restart_registration,
        # uncertain != 0, non-terminal/mismatched transaction, canceled_unstarted, approve-media
        with tempfile.TemporaryDirectory() as d:
            fx = _RaceEntryFixture(d)
            self.assertEqual(fx.invoke(), 0, fx.error)
            assert fx.comment is not None
            rec = parse_session_comment(fx.comment["body"], identity=IDENTITY)
            assert (
                rec is not None
                and rec.api_run_accounting is not None
                and rec.transaction is not None
            )
            accounting = ApiRunAccounting.from_document(rec.api_run_accounting)
            caller = fx.accounted
            frozen = accounting.configuration
            review_args = Namespace(base_sha=BASE, head_sha=HEAD)

            self.assertTrue(
                _is_benign_terminal_rerun(
                    purpose="review",
                    record=rec,
                    previous=accounting,
                    caller=caller,
                    frozen=frozen,
                    args=review_args,
                    restart_registration=None,
                    command_action=None,
                    source=None,
                )
            )
            self.assertFalse(
                _is_benign_terminal_rerun(
                    purpose="review",
                    record=Namespace(
                        quarantine_lineage={"schema_version": "quarantine-lineage-v1"},
                        transaction=rec.transaction,
                    ),
                    previous=accounting,
                    caller=caller,
                    frozen=frozen,
                    args=review_args,
                    restart_registration=None,
                    command_action=None,
                    source=None,
                )
            )
            self.assertFalse(
                _is_benign_terminal_rerun(
                    purpose="review",
                    record=rec,
                    previous=accounting,
                    caller=caller,
                    frozen=frozen,
                    args=review_args,
                    restart_registration={"job": "hosted"},
                    command_action=None,
                    source=None,
                )
            )
            uncertain_accounting = ApiRunAccounting(
                identity=ApiRunIdentity(123, 456),
                configuration=frozen,
                owner=accounting.owner,
                startup=accounting.startup,
                startup_used=accounting.startup_used,
                blocks=(
                    CallerRequestBlock(
                        "b1",
                        accounting.owner,
                        0,
                        (PendingApiRequest("req-1", 1_900_000_000_000),),
                    ),
                ),
                terminal_transaction=accounting.terminal_transaction,
            )
            self.assertFalse(
                _is_benign_terminal_rerun(
                    purpose="review",
                    record=rec,
                    previous=uncertain_accounting,
                    caller=caller,
                    frozen=frozen,
                    args=review_args,
                    restart_registration=None,
                    command_action=None,
                    source=None,
                )
            )
            self.assertFalse(
                _is_benign_terminal_rerun(
                    purpose="review",
                    record=Namespace(
                        quarantine_lineage=None,
                        transaction=ReviewTransaction.from_dict(
                            {
                                **rec.transaction.to_dict(),
                                "phase": "analysis",
                                "result_sha256": None,
                            }
                        ),
                    ),
                    previous=accounting,
                    caller=caller,
                    frozen=frozen,
                    args=review_args,
                    restart_registration=None,
                    command_action=None,
                    source=None,
                )
            )

        # Reply lane with edited source (mismatched intent) on attempt=2 -> fail closed!
        with tempfile.TemporaryDirectory() as d:
            rfx = _RaceReplyFixture(d)
            self.assertEqual(rfx.invoke_reply(), 0, rfx.error)
            rfx.attempt = 2
            rfx.source["body"] = "@reviewsensei edited question"
            rfx.trace.clear()
            rfx.transports.clear()
            with _wrap_cli_main_adding_skip_flag(target_subcommand="reply-run"):
                code = rfx.invoke_reply()
            self.assertEqual(code, 1, rfx.error)
            self.assertIn("caller run is already terminal", rfx.error)

        # Command lane with approve-media on attempt=2 -> fail closed!
        with tempfile.TemporaryDirectory() as d:
            cfx = _RaceCommandFixture(d)
            self.assertEqual(
                cfx.invoke_command("@reviewsensei review pause"), 0, cfx.error
            )
            cfx.attempt = 2
            cfx.command_body = "@reviewsensei I reviewed the media files and I approve"
            cfx.source["body"] = cfx.command_body
            cfx.trace.clear()
            cfx.transports.clear()
            with _wrap_cli_main_adding_skip_flag(target_subcommand="command-run"):
                code = cfx.invoke_command()
            self.assertEqual(code, 1, cfx.error)
            self.assertIn("caller run is already terminal", cfx.error)

    def test_cross_run_duplicate_terminal_delivery_prints_already_replied(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            rfx = _RaceReplyFixture(d)
            self.assertEqual(rfx.invoke_reply(), 0, rfx.error)
            self.assertEqual(rfx.output.strip(), "replied")

            # Cross-run duplicate webhook for the same comment (new run_id, attempt=1, action=created):
            rfx.run_id += 1
            rfx.attempt = 1
            rfx.trace.clear()
            rfx.transports.clear()
            with patch(
                "review_sensei_github.action_run._read_event_payload",
                return_value={
                    "action": "created",
                    "issue": {"number": 7, "pull_request": {}},
                    "comment": rfx.source,
                    "repository": {"id": 123, "full_name": "owner/repo"},
                    "sender": rfx.source["user"],
                },
            ):
                code = rfx.invoke_reply()
            self.assertEqual(code, 0, rfx.error)
            self.assertEqual(rfx.output.strip(), "already_replied")

            # When event action is not "created", allow_duplicate_terminal is False -> exit 1:
            rfx.run_id += 1
            rfx.trace.clear()
            rfx.transports.clear()
            with patch(
                "review_sensei_github.action_run._read_event_payload",
                return_value={
                    "action": "edited",
                    "issue": {"number": 7, "pull_request": {}},
                    "comment": rfx.source,
                    "repository": {"id": 123, "full_name": "owner/repo"},
                    "sender": rfx.source["user"],
                },
            ):
                code_edited = rfx.invoke_reply()
            self.assertEqual(code_edited, 1, rfx.error)

    def test_workflow_passes_skip_benign_races_except_on_restart_run(self) -> None:
        workflow = Path(".github/workflows/review-sensei-run.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("--skip-benign-races", workflow)
        self.assertEqual(
            workflow.count(
                'if [[ -z "${CONTROLLED_RESTART_ID:-}" ]]; then\n            lifecycle_args+=(--skip-benign-races)'
            ),
            2,
        )
        self.assertEqual(
            workflow.count(
                'command-run \\\n            --policy-commit "$POLICY_COMMIT" --run-directory "$RUNNER_TEMP" --skip-benign-races --'
            ),
            1,
        )
        self.assertEqual(
            workflow.count(
                'reply-run \\\n            --policy-commit "$POLICY_COMMIT" --run-directory "$RUNNER_TEMP" --skip-benign-races --'
            ),
            2,
        )


if __name__ == "__main__":
    unittest.main()
