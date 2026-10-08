from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
try:
    import setup_release_signing as SETUP
    import sign_release as SIGN
finally:
    sys.path.pop(0)

FINGERPRINT = "A" * 40
EMAIL = "fixture@example.invalid"
SHA = "b" * 40
TAG_OBJECT = "c" * 40
VERSION = "1.2.3"
DATE = "2026-10-08"
IDENTITY = {
    "RELEASE_SIGNING_FINGERPRINT": FINGERPRINT,
    "RELEASE_SIGNING_EMAIL": EMAIL,
    "RELEASE_SIGNING_PRIVATE_KEY": "synthetic-private-placeholder",
    "RELEASE_SIGNING_PASSPHRASE": "synthetic-passphrase-placeholder",
    "RELEASE_DOCS_APP_ID": "12345",
    "RELEASE_DOCS_APP_SLUG": "fixture-release-app",
}


def environment(name):
    return {
        "name": name,
        "protection_rules": [
            {
                "type": "required_reviewers",
                "reviewers": [
                    {
                        "type": "User",
                        "reviewer": {"id": 13791232, "login": "malsabbagh"},
                    }
                ],
            }
        ],
    }


def registered_key():
    return {
        "key_id": FINGERPRINT[-16:],
        "can_sign": True,
        "revoked": False,
        "emails": [{"email": EMAIL, "verified": True}],
    }


class SigningAuthorityTests(unittest.TestCase):
    def test_missing_or_malformed_identity_stops(self):
        for changes in (
            {"RELEASE_SIGNING_FINGERPRINT": ""},
            {"RELEASE_SIGNING_FINGERPRINT": "abc"},
            {"RELEASE_SIGNING_EMAIL": "a\nb@example.invalid"},
        ):
            with (
                patch.dict(os.environ, {**IDENTITY, **changes}),
                self.assertRaises(ValueError),
            ):
                SIGN.signing_identity()

    def test_public_registration_requires_exact_signing_key_verified_email(self):
        with patch.object(SIGN.bot, "api", return_value=[registered_key()]):
            SIGN.registered_identity(FINGERPRINT, EMAIL)
        for field, value in (
            ("can_sign", False),
            ("revoked", True),
            ("key_id", "D" * 16),
            ("emails", [{"email": EMAIL, "verified": False}]),
        ):
            invalid = {**registered_key(), field: value}
            with (
                patch.object(SIGN.bot, "api", return_value=[invalid]),
                self.assertRaises(ValueError),
            ):
                SIGN.registered_identity(FINGERPRINT, EMAIL)

    def test_publication_gate_requires_mo_as_only_reviewer_in_both_environments(self):
        with patch.object(
            SIGN.docs, "api", side_effect=lambda path: environment(path.split("/")[-1])
        ):
            SIGN.publication_gates()
        for mutation in ("missing", "wrong_owner", "extra_reviewer", "wrong_name"):
            invalid = environment("npm")
            if mutation == "missing":
                invalid["protection_rules"] = []
            elif mutation == "wrong_owner":
                invalid["protection_rules"][0]["reviewers"][0]["reviewer"]["id"] = 999
            elif mutation == "extra_reviewer":
                invalid["protection_rules"][0]["reviewers"].append({"type": "Team"})
            else:
                invalid["name"] = "unprotected"
            with (
                patch.object(SIGN.docs, "api", return_value=invalid),
                self.assertRaises(ValueError),
            ):
                SIGN.publication_gates()

    def test_qualification_stops_stale_head_failed_ci_or_changed_receipt(self):
        for main, ci, version in (
            ("d" * 40, {}, VERSION),
            (SHA, None, VERSION),
            (SHA, {}, "1.2.4"),
        ):
            with (
                patch.object(SIGN.prep, "remote_main", return_value=main),
                patch.object(SIGN.prep, "ci_once", return_value=ci),
                patch.object(
                    SIGN.prep,
                    "verify_commit",
                    return_value={"version": version, "release_date": DATE},
                ),
                patch.object(SIGN, "publication_gates") as gates,
                self.assertRaises(ValueError),
            ):
                SIGN.qualify(ROOT, SHA, VERSION, DATE)
            gates.assert_not_called()

    def test_signature_status_requires_single_pinned_key_and_no_expiry_or_revocation(
        self,
    ):
        valid = f"[GNUPG:] VALIDSIG {FINGERPRINT} 2026-10-08 0 0 4 0 1 10 00 {FINGERPRINT}\n"
        SIGN.valid_signature(valid, FINGERPRINT)
        for status in (
            "",
            valid.replace(FINGERPRINT, "D" * 40),
            valid * 2,
            valid + "[GNUPG:] EXPKEYSIG ABC expired\n",
            valid + "[GNUPG:] REVKEYSIG ABC revoked\n",
        ):
            with self.assertRaises(ValueError):
                SIGN.valid_signature(status, FINGERPRINT)

    def test_command_failure_cannot_print_private_diagnostics(self):
        failure = subprocess.CompletedProcess(
            [], 1, "synthetic-private-placeholder", "synthetic-passphrase-placeholder"
        )
        with (
            patch.object(SIGN.subprocess, "run", return_value=failure),
            self.assertRaises(ValueError) as exc,
        ):
            SIGN.run(["gpg", "--import", "key.asc"], env={})
        self.assertNotIn("placeholder", str(exc.exception))


class SigningOperationTests(unittest.TestCase):
    def exercise(
        self,
        *,
        fail_verify=False,
        bad_remote=False,
        existing=False,
        bad_key=False,
        fail_push=False,
        late_main=False,
    ):
        calls = []
        secret_locations = []

        def safe_run(args, *, env, cwd=None):
            calls.append(args)
            self.assertNotIn("RELEASE_SIGNING_PRIVATE_KEY", env)
            self.assertNotIn("RELEASE_SIGNING_PASSPHRASE", env)
            self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
            home = Path(env["GNUPGHOME"])
            secret_locations.append(home)
            self.assertEqual(
                (home / "key.asc").read_text(), IDENTITY["RELEASE_SIGNING_PRIVATE_KEY"]
            )
            self.assertNotIn(IDENTITY["RELEASE_SIGNING_PASSPHRASE"], " ".join(args))
            if "--list-secret-keys" in args:
                fingerprint = "D" * 40 if bad_key else FINGERPRINT
                return f"fpr:::::::::{fingerprint}:\nuid:::::::::ReviewSensei Release Marshal <{EMAIL}>:\n"
            return ""

        def subprocess_run(args, **kwargs):
            calls.append(args)
            if "verify-tag" in args:
                return subprocess.CompletedProcess(
                    args,
                    1 if fail_verify else 0,
                    "",
                    f"[GNUPG:] VALIDSIG {FINGERPRINT} date remaining\n",
                )
            return subprocess.CompletedProcess(
                args, 1 if "push" in args and fail_push else 0, "", ""
            )

        def local_git(root, *args):
            calls.append(list(args))
            if args == ("remote", "get-url", "origin"):
                return "https://github.com/malsabbagh/review-sensei.git"
            return TAG_OBJECT

        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, IDENTITY))
            stack.enter_context(patch.object(SIGN.prep, "workflow_context"))
            stack.enter_context(patch.object(SIGN.bot, "check"))
            stack.enter_context(patch.object(SIGN, "registered_identity"))
            stack.enter_context(patch.object(SIGN, "qualify"))
            unused = stack.enter_context(
                patch.object(
                    SIGN.prep,
                    "unused",
                    side_effect=ValueError("tag exists") if existing else None,
                )
            )
            stack.enter_context(
                patch.object(
                    SIGN.prep,
                    "remote_main",
                    return_value="d" * 40 if late_main else SHA,
                )
            )
            stack.enter_context(
                patch.object(SIGN.prep, "ci_once", return_value={"id": 1})
            )
            stack.enter_context(patch.object(SIGN, "git", side_effect=local_git))
            stack.enter_context(patch.object(SIGN, "run", side_effect=safe_run))
            stack.enter_context(
                patch.object(SIGN.subprocess, "run", side_effect=subprocess_run)
            )
            stack.enter_context(
                patch.object(
                    SIGN.docs,
                    "tag_identity",
                    return_value=("d" * 40 if bad_remote else TAG_OBJECT, SHA),
                )
            )
            if any((fail_verify, bad_remote, existing, bad_key, late_main)):
                with self.assertRaises(ValueError):
                    SIGN.sign(ROOT, SHA, VERSION, DATE)
            else:
                self.assertEqual(
                    SIGN.sign(ROOT, SHA, VERSION, DATE)["tag_object_sha"], TAG_OBJECT
                )
            self.assertGreaterEqual(unused.call_count, 1)
        self.assertTrue(all(not path.exists() for path in secret_locations))
        return calls

    def test_success_signs_exact_sha_and_pushes_only_new_tag_object_then_cleans_keyring(
        self,
    ):
        calls = self.exercise()
        signing = next(args for args in calls if "--sign" in args)
        self.assertIn(SHA, signing)
        self.assertIn(f"user.signingkey={FINGERPRINT}!", signing)
        push = next(args for args in calls if "push" in args)
        self.assertEqual(push[-1], f"{TAG_OBJECT}:refs/tags/v{VERSION}")
        self.assertFalse(
            any("--force" in arg or "refs/heads/main" in arg for arg in push)
        )
        self.assertTrue(any(args[0] == "gpgconf" for args in calls))

    def test_invalid_local_signature_or_wrong_imported_key_never_pushes(self):
        for options in ({"fail_verify": True}, {"bad_key": True}):
            calls = self.exercise(**options)
            self.assertFalse(any("push" in args for args in calls))

    def test_existing_tag_is_not_resigned_or_updated(self):
        calls = self.exercise(existing=True)
        self.assertFalse(any("--sign" in args or "push" in args for args in calls))

    def test_main_advance_after_local_signing_stops_before_remote_write(self):
        calls = self.exercise(late_main=True)
        self.assertTrue(any("--sign" in args for args in calls))
        self.assertFalse(any("push" in args for args in calls))

    def test_wrong_remote_object_fails_without_remote_deletion(self):
        calls = self.exercise(bad_remote=True)
        self.assertFalse(any("--delete" in args and "push" in args for args in calls))

    def test_dropped_push_response_reconciles_only_exact_github_verified_object(self):
        self.exercise(fail_push=True)
        self.exercise(fail_push=True, bad_remote=True)


@unittest.skipUnless(os.name == "posix", "secure local key setup targets macOS/Linux")
class UserSetupTests(unittest.TestCase):
    def test_backup_refuses_repository_symlinks_and_incomplete_or_public_storage(self):
        with self.assertRaises(ValueError):
            SETUP.safe_backup(ROOT / "signing-backup")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            linked = root / "linked"
            linked.symlink_to(root, target_is_directory=True)
            with self.assertRaises(ValueError):
                SETUP.safe_backup(linked / "backup")
            insecure = root / "insecure"
            insecure.mkdir(mode=0o755)
            with self.assertRaises(ValueError):
                SETUP.safe_backup(insecure)

    def test_setup_checks_main_only_policy_and_both_publication_reviewers(self):
        def response(path):
            if path.endswith("deployment-branch-policies"):
                return {
                    "total_count": 1,
                    "branch_policies": [{"name": "main", "type": "branch"}],
                }
            if path.endswith("release-docs-main"):
                return {
                    "deployment_branch_policy": {
                        "protected_branches": False,
                        "custom_branch_policies": True,
                    }
                }
            return environment(path.rsplit("/", 1)[-1])

        with patch.object(SETUP, "api", side_effect=response):
            SETUP.check_gates()
        for policy in (
            {"name": "*", "type": "branch"},
            {"name": "main", "type": "tag"},
        ):

            def invalid(path):
                if path.endswith("deployment-branch-policies"):
                    return {"total_count": 1, "branch_policies": [policy]}
                return response(path)

            with (
                patch.object(SETUP, "api", side_effect=invalid),
                self.assertRaises(ValueError),
            ):
                SETUP.check_gates()

    def test_verify_only_reads_public_metadata_and_secret_names_without_key_files(self):
        def response(path):
            if "/actions/variables?" in path:
                return {
                    "variables": [
                        {"name": name, "value": value}
                        for name, value in zip(
                            SETUP.PUBLIC_VARIABLES, (FINGERPRINT, EMAIL)
                        )
                    ]
                }
            if "/secrets?" in path:
                return {"secrets": [{"name": name} for name in SETUP.SECRET_NAMES]}
            if path.startswith("users/"):
                return [registered_key()]
            raise AssertionError(path)

        with (
            patch.object(SETUP, "check_gates"),
            patch.object(SETUP, "api", side_effect=response),
            patch.object(
                Path,
                "open",
                side_effect=AssertionError("private files must not be read"),
            ),
        ):
            self.assertEqual(SETUP.verify_configuration()["fingerprint"], FINGERPRINT)

    def test_secret_upload_uses_stdin_file_and_captured_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            private = Path(temporary) / "private"
            private.write_text("synthetic-private-placeholder")
            with patch.object(
                SETUP.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(
                    [], 1, b"secret stdout", b"secret stderr"
                ),
            ) as invocation:
                with self.assertRaises(ValueError) as exc:
                    SETUP.command(["gh", "secret", "set", "NAME"], stdin=private)
                self.assertNotIn("secret stdout", str(exc.exception))
                self.assertTrue(invocation.call_args.kwargs["capture_output"])
                self.assertNotIn("placeholder", str(invocation.call_args.args))

    def test_wrong_account_or_unverified_email_stops_before_generation(self):
        for user, emails in (
            ({"login": "other", "id": 42}, []),
            (
                {"login": "malsabbagh", "id": 13791232},
                [[{"email": EMAIL, "verified": False}]],
            ),
        ):
            with (
                patch.object(SETUP.shutil, "which", return_value="/fixture/bin"),
                patch.object(SETUP, "api", return_value=user),
                patch.object(
                    SETUP, "command", return_value=json.dumps(emails)
                ) as execute,
                self.assertRaises(ValueError),
            ):
                SETUP.setup(Path("/fixture/backup"), EMAIL)
            self.assertFalse(
                any(
                    "--quick-generate-key" in call.args[0]
                    for call in execute.call_args_list
                )
            )

    def test_idempotent_setup_uses_one_key_registers_public_only_and_never_prints_private_bytes(
        self,
    ):
        commands = []
        variables = {}
        secret_names = set()
        registered = []

        def api(path):
            if path == "user":
                return {"login": "malsabbagh", "id": 13791232}
            if "/actions/variables?" in path:
                return {
                    "total_count": len(variables),
                    "variables": [
                        {"name": k, "value": v} for k, v in variables.items()
                    ],
                }
            if "/secrets?" in path:
                return {
                    "total_count": len(secret_names),
                    "secrets": [{"name": k} for k in secret_names],
                }
            raise AssertionError(path)

        def execute(args, *, stdin=None, env=None):
            commands.append((args, stdin))
            if "user/emails" in args:
                return json.dumps(
                    [[{"email": EMAIL, "verified": True, "primary": True}]]
                )
            if "user/gpg_keys" in args:
                return json.dumps([registered])
            if "--list-secret-keys" in args:
                return f"fpr:::::::::{FINGERPRINT}:\n"
            if "--output" in args:
                Path(args[args.index("--output") + 1]).write_text(
                    "synthetic-export-placeholder"
                )
            if args[:3] == ["gh", "gpg-key", "add"]:
                self.assertEqual(Path(args[3]).name, "public.asc")
                registered.append(registered_key())
            if args[:3] == ["gh", "variable", "set"]:
                variables[args[3]] = args[-1]
            if args[:3] == ["gh", "secret", "set"]:
                self.assertIsNotNone(stdin)
                self.assertNotIn("synthetic-export-placeholder", " ".join(args))
                secret_names.add(args[3])
            return ""

        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(SETUP.shutil, "which", return_value="/fixture/bin"),
            patch.object(SETUP, "api", side_effect=api),
            patch.object(SETUP, "command", side_effect=execute),
            patch.object(SETUP, "check_gates"),
        ):
            backup = Path(temporary).resolve() / "backup"
            old_mask = os.umask(0o077)
            try:
                first = SETUP.setup(backup, None)
                second = SETUP.setup(backup, EMAIL)
            finally:
                os.umask(old_mask)
            self.assertEqual(first, second)
            self.assertEqual(
                sum("--quick-generate-key" in args for args, _ in commands), 1
            )
            self.assertEqual(
                sum(args[:3] == ["gh", "gpg-key", "add"] for args, _ in commands), 1
            )
            self.assertEqual(secret_names, set(SETUP.SECRET_NAMES))
            self.assertEqual((backup / "private.asc").stat().st_mode & 0o777, 0o600)
            self.assertNotIn("private-placeholder", json.dumps(first))
            (backup / "private.asc").write_text("changed-synthetic-export")
            before = len(commands)
            with self.assertRaisesRegex(ValueError, "backup bytes changed"):
                SETUP.setup(backup, EMAIL)
            self.assertFalse(
                any(
                    args[:3] == ["gh", "secret", "set"] for args, _ in commands[before:]
                )
            )
            (backup / "private.asc").write_text("synthetic-export-placeholder")
            variables[SETUP.PUBLIC_VARIABLES[0]] = "D" * 40
            before = len(commands)
            with self.assertRaises(ValueError):
                SETUP.setup(backup, EMAIL)
            self.assertFalse(
                any(
                    args[:3] == ["gh", "secret", "set"] for args, _ in commands[before:]
                )
            )


class SigningWorkflowTests(unittest.TestCase):
    def test_secret_is_available_only_in_owner_main_tag_job_after_generated_ci(self):
        text = (ROOT / ".github/workflows/prepare-release.yml").read_text()
        before, job = text.split("  sign-tag:\n", 1)
        self.assertNotIn("secrets.RELEASE_SIGNING", before)
        self.assertIn("needs: [prepare, qualify-prepared]", job)
        self.assertIn("inputs.sign_tag &&", job)
        self.assertIn("github.triggering_actor == 'malsabbagh'", job)
        self.assertIn("environment: release-docs-main", job)
        self.assertIn("ref: ${{ needs.prepare.outputs.source_sha }}", job)
        self.assertNotIn("permission-workflows", job)
        self.assertNotIn("permission-administration", job)
        self.assertNotIn("id-token: write", job)
        self.assertIn("skip-token-revoke: false", job)

    def test_release_keeps_protected_publish_jobs_and_waits_for_both_before_github_release(
        self,
    ):
        text = (ROOT / ".github/workflows/release.yml").read_text()
        self.assertIn("sign_release.py --check-publication-gates", text)
        self.assertIn(
            "needs: [build, publish, publish-npm]",
            text.split("  github-release:\n", 1)[1],
        )
        self.assertIn("name: npm", text)
        self.assertIn("name: pypi", text)
        self.assertNotIn("RELEASE_SIGNING_PRIVATE_KEY", text)


if __name__ == "__main__":
    unittest.main()
