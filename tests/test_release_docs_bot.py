from __future__ import annotations

import copy
import importlib.util
import io
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    import release_docs_bot as BOT

    SPEC = importlib.util.spec_from_file_location(
        "configure_release_docs_bot", ROOT / "scripts/configure_release_docs_bot.py"
    )
    assert SPEC and SPEC.loader
    SETUP = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(SETUP)
finally:
    sys.path.pop(0)

APP_ID = 123
SLUG = "release-docs-test"


def ruleset(identity=21068957, pr=False):
    return {
        "id": identity,
        "name": BOT.PR_RULESET_NAME if pr else "main",
        "source_type": "Repository",
        "source": BOT.docs.REPOSITORY,
        "target": "branch",
        "enforcement": "active",
        "conditions": copy.deepcopy(BOT.MAIN_CONDITIONS),
        "rules": [copy.deepcopy(BOT.PR_RULE)]
        if pr
        else [{"type": kind} for kind in sorted(BOT.CORE_RULES)],
        "bypass_actors": [copy.deepcopy(BOT.ADMIN_BYPASS)]
        + ([BOT.app_bypass(APP_ID)] if pr else []),
        "current_user_can_bypass": "always" if pr else "never",
    }


def active_rules(values):
    return [
        {
            **copy.deepcopy(rule),
            "ruleset_source_type": value["source_type"],
            "ruleset_source": value["source"],
            "ruleset_id": value["id"],
        }
        for value in values
        for rule in value["rules"]
    ]


class DocsAppGuardTests(unittest.TestCase):
    def setUp(self):
        self.app = {
            "id": APP_ID,
            "slug": SLUG,
            "owner": {"login": "malsabbagh"},
            "permissions": dict(BOT.APP_PERMISSIONS),
        }
        self.repositories = {
            "total_count": 1,
            "repositories": [{"full_name": BOT.docs.REPOSITORY}],
        }
        self.user = {"type": "Bot", "login": SLUG + "[bot]", "id": 456}
        self.core = ruleset()
        self.pr = ruleset(99, pr=True)

    def api(self, path):
        return {
            f"apps/{SLUG}": self.app,
            "installation/repositories?per_page=100": self.repositories,
            f"users/{SLUG}%5Bbot%5D": self.user,
        }[path]

    def check(self):
        values = [self.core, self.pr]
        with (
            patch.object(BOT, "api", side_effect=self.api),
            patch.object(BOT.docs, "api_pages", return_value=active_rules(values)),
            patch.object(
                BOT.docs,
                "api",
                side_effect=lambda path: {f"rulesets/{v['id']}": v for v in values}[
                    path
                ],
            ),
        ):
            return BOT.check(str(APP_ID), SLUG)

    def test_effective_pr_only_exception_accepts_hidden_actor_lists(self):
        self.assertEqual(self.check()["name"], SLUG + "[bot]")
        del self.core["bypass_actors"]
        del self.pr["bypass_actors"]
        self.assertEqual(
            self.check()["email"], f"456+{SLUG}[bot]@users.noreply.github.com"
        )

    def test_wrong_app_owner_identity_and_extra_authority_are_rejected(self):
        original = copy.deepcopy(self.app)
        for key, value in (
            ("id", 999),
            ("slug", "other-app"),
            ("owner", {"login": "attacker"}),
            ("permissions", {**BOT.APP_PERMISSIONS, "administration": "write"}),
            ("permissions", {**BOT.APP_PERMISSIONS, "workflows": "write"}),
            ("permissions", {**BOT.APP_PERMISSIONS, "pull_requests": "write"}),
            ("permissions", {**BOT.APP_PERMISSIONS, "actions": "write"}),
        ):
            with self.subTest(key=key, value=value):
                self.app = {**original, key: value}
                with self.assertRaisesRegex(ValueError, "identity/owner/permissions"):
                    self.check()

    def test_broad_empty_or_wrong_repository_token_is_rejected(self):
        for value in (
            {"total_count": 0, "repositories": []},
            {"total_count": 2, "repositories": [{"full_name": BOT.docs.REPOSITORY}]},
            {"total_count": 1, "repositories": [{"full_name": "other/repo"}]},
        ):
            with self.subTest(value=value):
                self.repositories = value
                with self.assertRaisesRegex(
                    ValueError, "exactly the release repository"
                ):
                    self.check()

    def test_app_mismatch_reports_exact_public_fields_without_response_secrets(self):
        self.app.update(
            {
                "owner": {"login": "wrong-owner"},
                "permissions": {**BOT.APP_PERMISSIONS, "actions": "write"},
                "client_secret": "SENSITIVE_CLIENT_SECRET",
                "pem": "SENSITIVE_PRIVATE_KEY",
            }
        )
        with self.assertRaises(ValueError) as raised:
            self.check()
        text = str(raised.exception)
        self.assertIn('"owner_login"', text)
        self.assertIn('"observed": "wrong-owner"', text)
        self.assertIn('"actions": "write"', text)
        self.assertIn('"actions": "read"', text)
        for excluded in (
            "SENSITIVE_CLIENT_SECRET",
            "SENSITIVE_PRIVATE_KEY",
            '"client_secret"',
            '"pem"',
            '"slug"',
            '"id"',
        ):
            self.assertNotIn(excluded, text)

    def test_missing_metadata_permission_is_diagnosed_without_relaxing_guard(self):
        del self.app["permissions"]["metadata"]
        with self.assertRaises(ValueError) as raised:
            self.check()
        text = str(raised.exception)
        self.assertIn('"permissions"', text)
        self.assertIn('"metadata": "read"', text)
        self.assertIn('"observed": {"actions": "read", "contents": "write"}', text)

    def test_unknown_bypass_or_non_pr_exception_is_rejected(self):
        for target in ("core", "pr"):
            for mode in (None, "always", "never", "pull_requests_only", "exempt"):
                expected = "never" if target == "core" else "always"
                if mode == expected:
                    continue
                with self.subTest(target=target, mode=mode):
                    self.core, self.pr = ruleset(), ruleset(99, pr=True)
                    value = self.core if target == "core" else self.pr
                    value["current_user_can_bypass"] = mode
                    with self.assertRaises(ValueError):
                        self.check()
        self.core, self.pr = ruleset(), ruleset(99, pr=True)
        self.core["bypass_actors"].append(BOT.app_bypass(APP_ID))
        with self.assertRaisesRegex(ValueError, "non-PR"):
            self.check()

    def test_mixed_rules_wrong_actor_or_weakened_pr_are_rejected(self):
        original = copy.deepcopy(self.pr)
        for key, value in (
            ("rules", [BOT.PR_RULE, {"type": "non_fast_forward"}]),
            (
                "rules",
                [
                    {
                        "type": "pull_request",
                        "parameters": {
                            **BOT.PR_PARAMETERS,
                            "required_approving_review_count": 0,
                        },
                    }
                ],
            ),
            ("bypass_actors", [BOT.ADMIN_BYPASS, BOT.app_bypass(999)]),
            ("conditions", {"ref_name": {"include": ["refs/heads/*"], "exclude": []}}),
        ):
            with self.subTest(key=key):
                self.pr = {**original, key: value}
                with self.assertRaisesRegex(ValueError, "requires a pull request"):
                    self.check()

    def test_missing_protections_or_untrusted_rule_provenance_is_rejected(self):
        self.core["rules"].pop()
        with self.assertRaisesRegex(ValueError, "retain deletion"):
            self.check()
        self.core = ruleset()
        self.pr["source"] = "attacker/repo"
        with self.assertRaisesRegex(ValueError, "unverifiable"):
            self.check()

    def test_global_api_only_reads_and_never_logs_token(self):
        with patch.object(
            BOT.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, '{"id":123}', ""),
        ) as run:
            self.assertEqual(BOT.api(f"apps/{SLUG}"), {"id": 123})
            self.assertEqual(run.call_args.args[0], ["gh", "api", f"apps/{SLUG}"])


class DocsAppConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.original = ruleset()
        self.original["rules"].append(copy.deepcopy(BOT.PR_RULE))
        self.values = {21068957: self.original}
        self.writes = []
        self.advance_policy = False
        self.app = {
            "id": APP_ID,
            "owner": {"login": "malsabbagh"},
            "permissions": dict(BOT.APP_PERMISSIONS),
        }

    def api(self, path):
        if path == "":
            return {"default_branch": "main"}
        value = copy.deepcopy(self.values[int(path.split("/")[1])])
        if self.advance_policy and self.writes and path == "rulesets/21068957":
            value["name"] = "changed"
        return value

    def write(self, path, method, data):
        self.writes.append((path, method, copy.deepcopy(data)))
        if method == "POST":
            self.values[99] = {**ruleset(99, pr=True), **copy.deepcopy(data)}
            return self.values[99]
        self.values[21068957].update(copy.deepcopy(data))
        return self.values[21068957]

    def configure(self, apply=True, exclusive_owner_setup=True):
        with (
            patch.object(BOT, "api", return_value=self.app),
            patch.object(BOT.docs, "api", side_effect=self.api),
            patch.object(
                BOT.docs, "api_pages", side_effect=lambda _: list(self.values.values())
            ),
            patch.object(SETUP, "write", side_effect=self.write),
        ):
            return SETUP.configure(
                APP_ID, SLUG, apply, exclusive_owner_setup=exclusive_owner_setup
            )

    def test_apply_requires_exclusive_owner_confirmation_before_any_api_call(self):
        before = copy.deepcopy(self.values)
        with (
            patch.object(BOT, "api") as api,
            patch.object(BOT.docs, "api") as repository_api,
            patch.object(BOT.docs, "api_pages") as pages,
            patch.object(SETUP, "write") as write,
            self.assertRaisesRegex(ValueError, "--exclusive-owner-setup"),
        ):
            SETUP.configure(APP_ID, SLUG, True)
        for operation in (api, repository_api, pages, write):
            operation.assert_not_called()
        self.assertEqual(self.values, before)

    def test_apply_cli_without_exclusive_window_fails_without_api_or_writes(self):
        with (
            patch.object(
                sys,
                "argv",
                ["configure", "--app-id", str(APP_ID), "--app-slug", SLUG, "--apply"],
            ),
            patch.object(BOT, "api") as api,
            patch.object(SETUP, "write") as write,
            patch.object(sys, "stderr", io.StringIO()) as stderr,
        ):
            self.assertEqual(SETUP.main(), 1)
            self.assertIn("updates are not atomic", stderr.getvalue())
        api.assert_not_called()
        write.assert_not_called()

    def test_plan_without_exclusive_confirmation_does_not_write(self):
        self.configure(False, exclusive_owner_setup=False)
        self.assertEqual(self.writes, [])

    def test_edit_violating_exclusive_window_can_still_race_the_put(self):
        # The confirmation is operational, not CAS. Readback cannot identify
        # an edit overwritten between the final read and the actual PUT.
        original_write = self.write
        injected = []

        def violating_write(path, method, data):
            if method == "PUT":
                self.values[21068957]["rules"].append({"type": "required_signatures"})
                injected.append(True)
            return original_write(path, method, data)

        with patch.object(self, "write", side_effect=violating_write):
            self.configure()
        self.assertEqual(injected, [True])
        self.assertNotIn(
            {"type": "required_signatures"}, self.values[21068957]["rules"]
        )

    def test_plan_preserves_source_reviews_core_rules_and_existing_admin(self):
        before = copy.deepcopy(self.original)
        change = self.configure(False)
        self.assertEqual(self.original, before)
        self.assertEqual(self.writes, [])
        self.assertEqual(change["protect_main"]["bypass_actors"], [BOT.ADMIN_BYPASS])
        self.assertEqual(change["review_source"]["rules"], [BOT.PR_RULE])
        self.assertEqual(
            {r["type"] for r in change["protect_main"]["rules"]}, BOT.CORE_RULES
        )

    def test_apply_creates_review_first_and_retry_makes_no_changes(self):
        self.configure()
        self.assertEqual(
            [(p, m) for p, m, _ in self.writes],
            [("rulesets", "POST"), ("rulesets/21068957", "PUT")],
        )
        before = copy.deepcopy(self.values)
        self.writes.clear()
        self.configure()
        self.assertEqual(self.writes, [])
        self.assertEqual(self.values, before)

    def test_concurrent_owner_policy_edit_does_not_remove_original_review(self):
        self.advance_policy = True
        with self.assertRaisesRegex(ValueError, "policy advanced"):
            self.configure()
        self.assertEqual(len(self.writes), 1)
        self.assertIn(BOT.PR_RULE, self.values[21068957]["rules"])

    def test_unfamiliar_policy_or_existing_exception_is_not_overwritten(self):
        for kind in ("weakened-pr", "broad-bypass", "conflicting-existing"):
            self.setUp()
            if kind == "weakened-pr":
                self.original["rules"][-1]["parameters"][
                    "require_code_owner_review"
                ] = False
            elif kind == "broad-bypass":
                self.original["bypass_actors"].append(BOT.app_bypass(APP_ID))
            else:
                self.values[99] = ruleset(99, pr=True)
                self.values[99]["rules"].append({"type": "non_fast_forward"})
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                self.configure()
            self.assertEqual(self.writes, [])

    def test_first_write_failure_keeps_all_existing_rules(self):
        before = copy.deepcopy(self.values)
        with patch.object(
            self, "write", side_effect=subprocess.CalledProcessError(1, "gh")
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                self.configure()
        self.assertEqual(self.values, before)

    def test_other_branch_app_grants_require_owner_reconciliation(self):
        other = ruleset(88)
        other["name"] = "another-branch"
        other["bypass_actors"].append(BOT.app_bypass(APP_ID))
        self.values[88] = other
        with self.assertRaisesRegex(ValueError, "another active ruleset"):
            self.configure()
        self.assertEqual(self.writes, [])


class DocsBotWorkflowTests(unittest.TestCase):
    def test_readiness_is_main_only_secret_environment_and_has_no_release_or_push(self):
        text = (ROOT / ".github/workflows/release-docs-bot-check.yml").read_text()
        self.assertIn("workflow_dispatch:", text)
        self.assertIn("if: github.ref == 'refs/heads/main'", text)
        self.assertIn("environment: release-docs-main", text)
        self.assertIn("sync_release_docs.py --check-bot", text)
        for forbidden in (
            "pull_request:",
            "push:",
            "\n      contents: write\n",
            "pages: write",
            "id-token:",
            "permission-administration",
            "permission-workflows",
            "--bundle",
            "--apply",
        ):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
