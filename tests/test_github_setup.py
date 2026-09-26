import base64
import hashlib
import io
import json
import re
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError

from review_sensei.hosting.github import (
    GitHubSetupClient,
    GitHubSetupError,
    GitHubSetupTransientError,
    SetupFile,
    SetupPlanBuilder,
    SetupPullRequestService,
    VerifiedDelivery,
)
from review_sensei.hosting.github.setup import (
    BROKER_ACCEPTED_PUBLIC_WORKFLOW_TAGS,
    CONFIG_PATH,
    LEGACY_CONFIG_PATH,
    RELEASED_RUNNER_SWITCH_V4_SHA256,
    SETUP_FILE_PATHS,
    UNINSTALL_WORKFLOW_PATH,
    WORKFLOW_PATH,
    _broker_accepted_public_workflow_tags,
    _current_config_file,
    _historical_provider_parity_workflow,
    _historical_v4_uninstall_workflow,
    _historical_v5_uninstall_workflow,
    _historical_v5_workflow,
    _merge_focused_v4_workflow,
    _provider_parity_workflow,
    _provider_parity_workflow_before_draft_skip,
    _public_workflow_tag_from_job_ref,
    _released_runner_switch_v4_workflow,
    _tagged_workflow,
    _v4_with_review_mode_config_file,
)

BASE_SHA = "b" * 40
PUBLIC_WORKFLOW_SHA = "a" * 40

# Byte-exact managed-v4 recognition contract shared with the Cloudflare Worker:
# deploy/cloudflare/test/setup-content.test.ts asserts these same digests. The
# bytes represent artifacts that already exist in installations, so an edit to
# the current templates that changes them must be a deliberate decision.
MANAGED_V4_RECOGNITION_SHA256 = {
    "caller": "ce69d43119e2573853545edf90e595cda93fc0f4a018ede87aa5b604f6ab7742",
    "uninstall": "e349ede8fa3eca6a303a04d688679b1abc41d13c31ba0d10651c376e9c77a6ec",
    "config": "2a81144f0c22d295b8be49474979f9fa073271b3c763da302ba4f0fcf68cefb0",
}


class FakeHTTPResponse(io.BytesIO):
    def __init__(self, body=b"", status=200):
        super().__init__(body)
        self.status = status
        self.reason = "reason"
        self.headers = {}


class FakeTransport:
    def __init__(
        self,
        *,
        branch_exists=None,
        existing_prs=None,
        created_pr_number=42,
        default_branch="main",
        branch_managed=True,
        ref_collision=False,
    ):
        self.requests = []
        self._branch_exists = branch_exists if branch_exists is not None else False
        self.existing_prs = existing_prs or []
        self.created_pr_number = created_pr_number
        self.default_branch = default_branch
        self.branch_managed = branch_managed
        self.ref_collision = ref_collision
        self.collision_observed = False

    def get_default_branch(self, *, repository, installation_token):
        self.requests.append(("get_default_branch", repository, installation_token))
        return self.default_branch

    def get_default_head_sha(self, *, repository, installation_token, base_branch):
        self.requests.append(
            (
                "get_default_head_sha",
                repository,
                installation_token,
                base_branch,
            )
        )
        return BASE_SHA

    def branch_exists(self, *, repository, installation_token, branch):
        self.requests.append(("branch_exists", repository, installation_token, branch))
        return self._branch_exists or self.collision_observed

    def is_managed_setup_branch(
        self,
        *,
        repository,
        installation_token,
        branch,
        base_sha,
        public_workflow_tag,
    ):
        self.requests.append(
            (
                "is_managed_setup_branch",
                repository,
                installation_token,
                branch,
                base_sha,
                public_workflow_tag,
            )
        )
        return self.branch_managed

    def create_or_update_branch(
        self,
        *,
        repository,
        installation_token,
        base_branch,
        base_sha,
        branch,
        files,
    ):
        self.requests.append(
            (
                "create_or_update_branch",
                repository,
                installation_token,
                base_branch,
                base_sha,
                branch,
                files,
            )
        )
        if self.ref_collision:
            self.collision_observed = True
            return False
        return True

    def list_pull_requests(self, *, repository, installation_token, head_branch):
        self.requests.append(
            ("list_pull_requests", repository, installation_token, head_branch)
        )
        return self.existing_prs

    def create_pull_request(
        self, *, repository, installation_token, base_branch, head_branch, title, body
    ):
        self.requests.append(
            (
                "create_pull_request",
                repository,
                installation_token,
                base_branch,
                head_branch,
                title,
                body,
            )
        )
        return {"number": self.created_pr_number}


class RecheckTransport(FakeTransport):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pr_appeared_after_branch = False

    def list_pull_requests(self, *, repository, installation_token, head_branch):
        self.requests.append(
            ("list_pull_requests", repository, installation_token, head_branch)
        )
        if self.pr_appeared_after_branch:
            return [{"number": 11}]
        return []

    def create_or_update_branch(
        self,
        *,
        repository,
        installation_token,
        base_branch,
        base_sha,
        branch,
        files,
    ):
        created = super().create_or_update_branch(
            repository=repository,
            installation_token=installation_token,
            base_branch=base_branch,
            base_sha=base_sha,
            branch=branch,
            files=files,
        )
        self.pr_appeared_after_branch = True
        return created


class FileTransport(FakeTransport):
    def __init__(self, *, files, **kwargs):
        super().__init__(**kwargs)
        self.files = files

    def get_repository_file(self, *, repository, installation_token, base_branch, path):
        self.requests.append(
            (
                "get_repository_file",
                repository,
                installation_token,
                base_branch,
                path,
            )
        )
        return self.files.get(path)


def delivery(
    action="created",
    *,
    event="installation",
    suspended=False,
    repository="owner/repo",
    repositories=(),
):
    return VerifiedDelivery(
        app_id=123,
        event=event,
        action=action,
        installation_id=7,
        delivery_id="delivery-1",
        digest="0" * 64,
        repository=repository,
        repositories=repositories,
        suspended=suspended,
        permissions={
            "contents": "write",
            "pull_requests": "write",
            "variables": "write",
            "workflows": "write",
        },
    )


class SetupPlanTests(unittest.TestCase):
    def test_setup_file_rejects_unsafe_paths(self):
        with self.assertRaises(GitHubSetupError):
            SetupFile(path="", content="x")
        with self.assertRaises(GitHubSetupError):
            SetupFile(path="/abs", content="x")
        with self.assertRaises(GitHubSetupError):
            SetupFile(path="../escape", content="x")

    def test_build_plan_contains_pinned_workflow_and_no_secret_markers(self):
        plan = SetupPlanBuilder().build("owner/repo")

        self.assertEqual(plan.branch_name, "review-sensei/setup")
        self.assertEqual(len(plan.files), 3)
        workflow = dict((f.path, f.content) for f in plan.files)[
            ".github/workflows/review-sensei-review.yml"
        ]
        self.assertIn("# ReviewSensei setup version: 5", workflow)
        self.assertIn(
            "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@" + "v5",
            workflow,
        )
        self.assertIn(
            "types: [opened, reopened, synchronize, ready_for_review]", workflow
        )
        self.assertIn(
            "source_kind:\n        description: Source kind for manual dispatch",
            workflow,
        )
        self.assertIn(
            "root_comment_id:\n        description: Root comment ID for manual reply thread",
            workflow,
        )
        self.assertIn("OLLAMA_API_KEY: ${{ secrets.OLLAMA_API_KEY }}", workflow)
        self.assertIn("OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}", workflow)
        self.assertIn("OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}", workflow)
        config = dict((f.path, f.content) for f in plan.files)[CONFIG_PATH]
        self.assertEqual(config, _current_config_file())
        self.assertIn("  backend: local-ollama", config)
        # The generated caller is an invocation-only bridge. Policy lives in
        # .reviewsensei.yml, the two optional product overrides are read by the
        # reusable workflow, and the caller grants no write scope of its own.
        self.assertNotIn("vars.", workflow)
        self.assertNotIn("github.event.pull_request.draft", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn("pull-requests: read", workflow)
        self.assertIn("issues: read", workflow)
        self.assertNotIn("pull-requests: write", workflow)
        self.assertNotIn("issues: write", workflow)
        self.assertIn("github.event.comment.author_association == 'OWNER'", workflow)
        self.assertIn("github.event.comment.user.type != 'Bot'", workflow)
        self.assertIn("github.event.issue.pull_request", workflow)
        self.assertIn("resolve-trigger:", workflow)
        self.assertIn(
            "operation: ${{ needs.resolve-trigger.outputs.operation }}",
            workflow,
        )
        self.assertEqual(workflow, _tagged_workflow("v5"))
        self.assertEqual(
            workflow,
            (
                Path(__file__).parent.parent
                / "examples/github-actions/review-sensei-review.yml"
            ).read_text(encoding="utf-8"),
        )
        uninstall = dict((f.path, f.content) for f in plan.files)[
            ".github/workflows/review-sensei-uninstall.yml"
        ]
        self.assertIn("Remove ReviewSensei setup", uninstall)
        self.assertIn(".reviewsensei.yml", uninstall)
        self.assertIn(".github/review-sensei/config.yml", uninstall)
        combined = "\n".join((f.content for f in plan.files)) + "\n" + plan.body
        for marker in (
            "GITHUB_APP_PRIVATE_KEY",
            "GITHUB_APP_WEBHOOK_SECRET",
            "installation_token",
            "raw webhook",
            "Authorization:",
        ):
            self.assertNotIn(marker, combined)

        dynamic = SetupPlanBuilder().build(
            "owner/repo",
            base_branch="main",
            base_sha=BASE_SHA,
        )
        self.assertEqual(
            dynamic.branch_name,
            "review-sensei/setup-v5-bbbbbbbbbbbb-v5",
        )

    def test_build_plan_uses_the_supplied_tag(self):
        plan = SetupPlanBuilder(
            public_workflow_tag="stable",
        ).build(
            "owner/repo",
            base_branch="main",
            base_sha=BASE_SHA,
        )
        files = {file.path: file.content for file in plan.files}
        workflow = files[".github/workflows/review-sensei-review.yml"]
        self.assertEqual(workflow.count("review-sensei-run.yml@stable"), 1)
        self.assertNotIn("vars.", workflow)
        self.assertNotIn("default: main", workflow)
        self.assertIn("# ReviewSensei setup version: 5", workflow)
        self.assertEqual(workflow, _tagged_workflow("stable"))
        self.assertEqual(
            workflow,
            (
                Path(__file__).parent.parent
                / "examples/github-actions/review-sensei-review.yml"
            )
            .read_text(encoding="utf-8")
            .replace("@v5", "@stable"),
        )
        self.assertEqual(files[CONFIG_PATH], _current_config_file())
        self.assertEqual(
            plan.branch_name,
            "review-sensei/setup-v5-bbbbbbbbbbbb-stable",
        )

    def test_generated_caller_forwards_command_context_for_the_command_operation(self):
        workflow = SetupPlanBuilder().build("owner/repo").files[0].content

        forwarding = {
            "comment_body": "github.event.comment.body || ''",
            "comment_actor": "github.event.comment.user.login || ''",
            "comment_actor_type": "github.event.comment.user.type || 'User'",
            "comment_association": "github.event.comment.author_association || ''",
        }
        for name, expression in forwarding.items():
            matches = [
                line.strip()
                for line in workflow.splitlines()
                if line.strip().startswith(f"{name}: ")
            ]
            self.assertEqual(
                matches,
                [f"{name}: ${{{{ {expression} }}}}"],
                f"the generated caller must forward {name} exactly once",
            )

        self.assertIn(
            "      (github.event_name == 'issue_comment' &&\n"
            "      github.event.action == 'created' &&\n"
            "      github.event.issue.pull_request &&\n"
            "      contains(github.event.comment.body, '@sensei') &&\n"
            "      (github.event.comment.author_association == 'OWNER' ||\n"
            "      github.event.comment.author_association == 'MEMBER' ||\n"
            "      github.event.comment.author_association == 'COLLABORATOR') &&\n"
            "      github.event.comment.user.type != 'Bot') ||\n",
            workflow,
            "the resolver job condition must keep the mention, association, and "
            "user-type checks on the comment arm; the invocation job has no "
            "policy condition of its own, so the command operation reaches the "
            "runner without any variables read",
        )
        self.assertNotIn("@@", workflow)

    def test_builder_rejects_unsafe_public_workflow_tags(self):
        for tag in ("", "refs/tags/v4", "v4..next", "v4.", "v4.lock", "v4\n"):
            with self.subTest(tag=tag), self.assertRaises(GitHubSetupError):
                SetupPlanBuilder(public_workflow_tag=tag)

    def test_broker_accepts_v4_and_v5_during_channel_migration(self):
        self.assertEqual(BROKER_ACCEPTED_PUBLIC_WORKFLOW_TAGS, ("v4", "v5"))
        self.assertEqual(_broker_accepted_public_workflow_tags("v4"), ("v4", "v5"))
        self.assertEqual(_broker_accepted_public_workflow_tags("v5"), ("v5", "v4"))
        self.assertEqual(
            _public_workflow_tag_from_job_ref(
                "malsabbagh/review-sensei/.github/workflows/"
                "review-sensei-run.yml@refs/tags/v5"
            ),
            "v5",
        )
        self.assertIsNone(
            _public_workflow_tag_from_job_ref(
                "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@v5"
            )
        )

    def test_builder_rejects_sha_configuration_for_current_v4(self):
        with self.assertRaises(GitHubSetupError):
            SetupPlanBuilder(public_workflow_sha=PUBLIC_WORKFLOW_SHA)

    def test_build_rejects_unsafe_repository(self):
        with self.assertRaises(GitHubSetupError):
            SetupPlanBuilder().build("../outside")

    def test_builder_rejects_invalid_branch_and_title(self):
        with self.assertRaises(GitHubSetupError):
            SetupPlanBuilder(branch_name="")
        with self.assertRaises(GitHubSetupError):
            SetupPlanBuilder(branch_name="../bad")
        with self.assertRaises(GitHubSetupError):
            SetupPlanBuilder(branch_name="bad?branch")
        with self.assertRaises(GitHubSetupError):
            SetupPlanBuilder(title="")

    def test_build_rejects_invalid_base_sha_for_tagged_setup(self):
        with self.assertRaises(GitHubSetupError):
            SetupPlanBuilder().build("owner/repo", base_sha="not-a-sha")

    def test_legacy_v3_build_with_base_sha_keeps_migration_branch(self):
        plan = SetupPlanBuilder(
            public_workflow_sha=PUBLIC_WORKFLOW_SHA,
            legacy_v3=True,
        ).build("owner/repo", base_sha=BASE_SHA)

        self.assertEqual(
            plan.branch_name,
            "review-sensei/setup-v3-bbbbbbbbbbbb-aaaaaaaaaaaa",
        )


class SetupPullRequestServiceTests(unittest.TestCase):
    @staticmethod
    def historical_fixture(name):
        return (Path(__file__).parent / "fixtures" / "setup-legacy" / name).read_text(
            encoding="utf-8"
        )

    @staticmethod
    def historical_v3_files(public_workflow_sha="c" * 40):
        plan = SetupPlanBuilder(
            public_workflow_sha=public_workflow_sha,
            legacy_v3=True,
        ).build("owner/repo")
        files = {file.path: file.content for file in plan.files}
        files[".github/workflows/review-sensei-review.yml"] = (
            files[".github/workflows/review-sensei-review.yml"]
            .replace(
                "      source_kind:\n"
                "        description: Source kind for manual dispatch (issue or inline)\n"
                "        required: false\n"
                "      source_comment_id:\n"
                "        description: Source comment ID for manual reply\n"
                "        required: false\n"
                "      source_updated_at:\n"
                "        description: Timestamp of source comment\n"
                "        required: false\n"
                "      root_comment_id:\n"
                "        description: Root comment ID for manual reply thread\n"
                "        required: false\n",
                "",
            )
            .replace(
                "  trusted-local-manual:\n",
                "  manual-or-trusted-local:\n",
            )
            .replace(
                "      head_ref: ${{ inputs.head_ref || '' }}\n",
                "      head_ref: ${{ inputs.head_ref || github.event.pull_request.head.ref || '' }}\n",
            )
        )
        files[".github/workflows/review-sensei-uninstall.yml"] = files[
            ".github/workflows/review-sensei-uninstall.yml"
        ].replace(
            '              "--body", "Remove generated ReviewSensei setup files; '
            'learnings and secrets remain untouched.",\n',
            '              "--body", "Remove the ReviewSensei workflow, cleanup '
            "workflow, and generated configuration. ReviewSensei learnings and "
            'repository secrets are left untouched.",\n',
        )
        return files

    def test_installation_created_creates_one_setup_pr(self):
        transport = FakeTransport()
        service = SetupPullRequestService(transport)

        results = service.ensure_setup_pull_requests(
            delivery(),
            installation_token="ghs_opaque",
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "created")
        self.assertEqual(results[0].pull_request_number, 42)
        self.assertTrue(any(r[0] == "create_pull_request" for r in transport.requests))

    def test_installation_repositories_added_creates_one_pr_per_repo(self):
        transport = FakeTransport()
        service = SetupPullRequestService(transport)

        results = service.ensure_setup_pull_requests(
            delivery(
                event="installation_repositories",
                action="added",
                repository="owner/repo",
            ),
            installation_token="ghs_opaque",
            repositories=["owner/other"],
            permissions={
                "contents": "write",
                "pull_requests": "write",
                "variables": "write",
                "workflows": "write",
            },
        )

        self.assertEqual([r.repository for r in results], ["owner/repo", "owner/other"])
        self.assertEqual([r.status for r in results], ["created", "created"])

    def test_delivery_repositories_create_one_pr_per_repo_without_explicit_arg(self):
        transport = FakeTransport()
        service = SetupPullRequestService(transport)

        results = service.ensure_setup_pull_requests(
            delivery(
                event="installation_repositories",
                action="added",
                repository="owner/repo",
                repositories=("owner/one", "owner/two"),
            ),
            installation_token="ghs_opaque",
        )

        self.assertEqual(
            [r.repository for r in results],
            ["owner/repo", "owner/one", "owner/two"],
        )
        self.assertEqual([r.status for r in results], ["created", "created", "created"])

    def test_existing_canonical_content_addressed_branch_is_reused(self):
        transport = FakeTransport(branch_exists=True)
        results = SetupPullRequestService(transport).ensure_setup_pull_requests(
            delivery(),
            installation_token="ghs_opaque",
        )

        self.assertEqual(results[0].status, "created")
        self.assertTrue(any(r[0] == "create_pull_request" for r in transport.requests))
        self.assertFalse(
            any(r[0] == "create_or_update_branch" for r in transport.requests)
        )

    def test_customer_owned_setup_branch_is_not_overwritten(self):
        transport = FakeTransport(branch_exists=True, branch_managed=False)

        results = SetupPullRequestService(transport).ensure_setup_pull_requests(
            delivery(),
            installation_token="ghs_opaque",
        )

        self.assertEqual(results[0].status, "skipped_branch_conflict")
        self.assertFalse(
            any(r[0] == "create_or_update_branch" for r in transport.requests)
        )
        self.assertFalse(any(r[0] == "create_pull_request" for r in transport.requests))

    def test_create_only_branch_race_does_not_overwrite_customer_ref(self):
        transport = FakeTransport(ref_collision=True, branch_managed=False)

        results = SetupPullRequestService(transport).ensure_setup_pull_requests(
            delivery(),
            installation_token="ghs_opaque",
        )

        self.assertEqual(results[0].status, "skipped_branch_conflict")
        self.assertFalse(any(r[0] == "create_pull_request" for r in transport.requests))

    def test_setup_never_touches_repository_variables(self):
        # Setup creates no behavioral variables: policy lives in
        # .reviewsensei.yml, and the only Actions variables anything reads are
        # the two optional product overrides, which setup never creates. The
        # current and customized skip states return before any repository write
        # either way, so nothing here can disturb an operator's values.
        plan = SetupPlanBuilder().build("owner/repo")
        current = FileTransport(files={file.path: file.content for file in plan.files})
        customized = FileTransport(
            files={SETUP_FILE_PATHS[0]: "name: Customer ReviewSensei review\n"}
        )

        for transport, expected in (
            (FakeTransport(), "created"),
            (current, "skipped_current"),
            (customized, "skipped_unknown_setup"),
        ):
            with self.subTest(status=expected):
                results = SetupPullRequestService(transport).ensure_setup_pull_requests(
                    delivery(),
                    installation_token="ghs_opaque",
                )
                self.assertEqual(results[0].status, expected)
                for request in transport.requests:
                    self.assertNotIn("variable", request[0])

    def test_legacy_setup_reuses_existing_content_addressed_branch(self):
        transport = FileTransport(
            branch_exists=True,
            files={
                ".github/review-sensei/config.yml": (
                    "# ReviewSensei setup version: 2\n"
                    "setup_version: 2\n"
                    "provider: ollama\n"
                    "provider_mode: local\n"
                    "base_url: http://127.0.0.1:11434/api\n"
                    "cloud_base_url: https://ollama.com/api\n"
                    "local_model: qwen3.5:4b\n"
                    "cloud_model: deepseek-v4-flash:cloud\n"
                ),
            },
        )

        results = SetupPullRequestService(transport).ensure_setup_pull_requests(
            delivery(),
            installation_token="ghs_opaque",
        )

        self.assertEqual(results[0].status, "created")
        self.assertFalse(
            any(r[0] == "create_or_update_branch" for r in transport.requests)
        )
        self.assertTrue(any(r[0] == "create_pull_request" for r in transport.requests))

    def test_hand_edited_retired_configuration_is_imported_into_the_setup(self):
        transport = FileTransport(
            files={
                LEGACY_CONFIG_PATH: (
                    "setup_version: 5\n"
                    "provider: ollama\n"
                    "provider_mode: cloud\n"
                    "model: ''\n"
                    "cloud_model: deepseek-v4.1-flash:cloud\n"
                    "github_writes: true\n"
                    "auto_approve: false\n"
                ),
            },
        )

        results = SetupPullRequestService(transport).ensure_setup_pull_requests(
            delivery(),
            installation_token="ghs_opaque",
        )

        self.assertEqual(results[0].status, "created")
        branch_request = next(
            r for r in transport.requests if r[0] == "create_or_update_branch"
        )
        written = {file.path: file.content for file in branch_request[6]}
        self.assertNotIn(LEGACY_CONFIG_PATH, written)
        config = written[CONFIG_PATH]
        self.assertIn("# One-time import of the retired", config)
        self.assertIn("backend: cloud-ollama", config)
        self.assertIn("model: deepseek-v4.1-flash:cloud", config)
        self.assertIn("writes: true", config)
        self.assertIn("reviews: advisory", config)
        pull_request = next(
            r for r in transport.requests if r[0] == "create_pull_request"
        )
        body = pull_request[6]
        self.assertIn("carried these settings into .reviewsensei.yml:", body)
        self.assertIn("github.writes", body)
        self.assertIn("github.reviews: auto-approve", body)

    def test_released_bytes_at_the_retired_path_are_replaced_not_imported(self):
        plan = SetupPlanBuilder().build("owner/repo")
        files = {file.path: file.content for file in plan.files}
        files[WORKFLOW_PATH] = _merge_focused_v4_workflow("v5")
        files[UNINSTALL_WORKFLOW_PATH] = _historical_v4_uninstall_workflow()
        files.pop(CONFIG_PATH)
        files[LEGACY_CONFIG_PATH] = _v4_with_review_mode_config_file()
        transport = FileTransport(files=files)

        results = SetupPullRequestService(transport).ensure_setup_pull_requests(
            delivery(),
            installation_token="ghs_opaque",
        )

        self.assertEqual(results[0].status, "created")
        branch_request = next(
            r for r in transport.requests if r[0] == "create_or_update_branch"
        )
        written = {file.path: file.content for file in branch_request[6]}
        self.assertEqual(written[CONFIG_PATH], _current_config_file())
        pull_request = next(
            r for r in transport.requests if r[0] == "create_pull_request"
        )
        body = pull_request[6]
        self.assertIn("backend choice and nothing else", body)
        self.assertNotIn("carried these settings", body)

    def test_current_setup_does_not_create_another_pr(self):
        plan = SetupPlanBuilder().build("owner/repo")
        transport = FileTransport(
            branch_exists=True,
            files={file.path: file.content for file in plan.files},
        )

        results = SetupPullRequestService(transport).ensure_setup_pull_requests(
            delivery(),
            installation_token="ghs_opaque",
        )

        self.assertEqual(results[0].status, "skipped_current")
        self.assertFalse(any(r[0] == "create_pull_request" for r in transport.requests))
        self.assertFalse(
            any(r[0] == "create_or_update_branch" for r in transport.requests)
        )

    def test_stale_v3_setup_with_unexpected_public_workflow_sha_is_migrated(self):
        plan = SetupPlanBuilder(
            public_workflow_sha="e" * 40,
            legacy_v3=True,
        ).build("owner/repo")
        transport = FileTransport(
            files={file.path: file.content for file in plan.files},
        )

        results = SetupPullRequestService(transport).ensure_setup_pull_requests(
            delivery(),
            installation_token="ghs_opaque",
        )

        self.assertEqual(results[0].status, "created")
        self.assertTrue(any(r[0] == "create_pull_request" for r in transport.requests))
        branch_request = next(
            r for r in transport.requests if r[0] == "create_or_update_branch"
        )
        self.assertEqual(
            branch_request[5],
            "review-sensei/setup-v5-bbbbbbbbbbbb-v5",
        )

    def test_stale_v5_setup_following_another_tag_is_migrated(self):
        plan = SetupPlanBuilder(public_workflow_tag="old-v4").build("owner/repo")
        files = {file.path: file.content for file in plan.files}
        transport = FileTransport(
            files=files,
        )

        results = SetupPullRequestService(transport).ensure_setup_pull_requests(
            delivery(),
            installation_token="ghs_opaque",
        )

        self.assertEqual(results[0].status, "created")
        branch_request = next(
            r for r in transport.requests if r[0] == "create_or_update_branch"
        )
        self.assertEqual(
            branch_request[5],
            "review-sensei/setup-v5-bbbbbbbbbbbb-v5",
        )

    def test_managed_v4_recognition_bytes_are_frozen_across_implementations(self):
        for name, content in (
            ("caller", _merge_focused_v4_workflow("v5")),
            ("uninstall", _historical_v4_uninstall_workflow()),
            ("config", _v4_with_review_mode_config_file()),
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    hashlib.sha256(content.encode()).hexdigest(),
                    MANAGED_V4_RECOGNITION_SHA256[name],
                )

    def test_frozen_digests_match_the_worker_sources_and_fixture_bytes(self):
        # The frozen digests are declared in the Python module, the Worker
        # sources, and this suite. Nothing keeps the copies in sync
        # automatically, so this guard reads the Worker sources and the
        # rendered fixture bytes and fails on any drift between the three.
        root = Path(__file__).resolve().parents[1]
        managed_source = (
            root / "deploy/cloudflare/src/managed-v4-recognition-artifacts.ts"
        ).read_text(encoding="utf-8")
        released_source = (
            root / "deploy/cloudflare/src/released-runner-switch-v4-caller.ts"
        ).read_text(encoding="utf-8")

        def declared(source: str, name: str) -> str:
            match = re.search(rf'{name}_SHA256\s*=\s*"([0-9a-f]{{64}})"', source)
            self.assertIsNotNone(match, f"{name}_SHA256 is missing from {source!r}")
            return match.group(1)

        worker_constants = {
            "caller": declared(managed_source, "MERGE_FOCUSED_V4_CALLER"),
            "uninstall": declared(managed_source, "HISTORICAL_V4_UNINSTALL"),
            "config": declared(managed_source, "MERGE_FOCUSED_V4_CONFIG"),
        }
        self.assertEqual(set(worker_constants), set(MANAGED_V4_RECOGNITION_SHA256))
        for name, content in (
            ("caller", _merge_focused_v4_workflow("v5")),
            ("uninstall", _historical_v4_uninstall_workflow()),
            ("config", _v4_with_review_mode_config_file()),
        ):
            digest = hashlib.sha256(content.encode()).hexdigest()
            with self.subTest(name=name):
                self.assertEqual(digest, MANAGED_V4_RECOGNITION_SHA256[name])
                self.assertEqual(digest, worker_constants[name])
        released = _released_runner_switch_v4_workflow("v4")
        self.assertEqual(
            hashlib.sha256(released.encode()).hexdigest(),
            declared(released_source, "RELEASED_RUNNER_SWITCH_V4"),
        )
        self.assertEqual(
            hashlib.sha256(released.encode()).hexdigest(),
            RELEASED_RUNNER_SWITCH_V4_SHA256,
        )

    def test_managed_v4_recognition_does_not_read_the_current_templates(self):
        from unittest.mock import patch

        from review_sensei.hosting.github import setup as setup_module

        cases = (
            ("caller", lambda: _merge_focused_v4_workflow("v5"), "_tagged_workflow"),
            ("uninstall", _historical_v4_uninstall_workflow, "_uninstall_workflow"),
            ("config", _v4_with_review_mode_config_file, "_current_config_file"),
        )
        for name, renderer, live_source in cases:
            with self.subTest(name=name):
                expected = renderer()
                with patch.object(
                    setup_module,
                    live_source,
                    side_effect=AssertionError("live setup template was consulted"),
                ):
                    self.assertEqual(renderer(), expected)

    def test_frozen_caller_reference_is_derived_from_the_fixture(self):
        # The frozen bytes carry the run-workflow reference exactly once; the
        # retag target must come from those bytes, never a parallel constant.
        # The Cloudflare twin asserts the same substitution contract
        # (deploy/cloudflare/test/setup-content.test.ts).
        reference = "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml"
        caller = _merge_focused_v4_workflow("v5")
        self.assertEqual(caller.count(reference + "@v5"), 1)
        self.assertEqual(_merge_focused_v4_workflow("v5"), caller)
        self.assertEqual(
            _merge_focused_v4_workflow("stable"),
            caller.replace(reference + "@v5", reference + "@stable"),
        )
        released = _released_runner_switch_v4_workflow("v4")
        self.assertEqual(released.count(reference + "@v4"), 1)
        self.assertEqual(_released_runner_switch_v4_workflow("v4"), released)
        self.assertEqual(
            _released_runner_switch_v4_workflow("stable"),
            released.replace(reference + "@v4", reference + "@stable"),
        )

    def test_frozen_caller_reference_guard_rejects_ambiguous_fixtures(self):
        from unittest.mock import patch

        from review_sensei.hosting.github import setup as setup_module

        reference = "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml"
        caller = _merge_focused_v4_workflow("v5")
        cases = (
            caller.replace(reference + "@v5", "example.invalid/other.yml@v5"),
            caller + reference + "@v5\n",
        )
        for content in cases:
            with self.subTest(references=content.count(reference)):
                with patch.object(
                    setup_module,
                    "_merge_focused_v4_caller_bytes",
                    return_value=content,
                ):
                    with self.assertRaises(GitHubSetupError):
                        _merge_focused_v4_workflow("stable")

    def test_marker_reverted_current_workflow_is_not_recognized_as_managed_v4(self):
        # A manually reverted marker must not turn live setup-v5 bytes into
        # managed-v4 content: the marker==4 branch accepts only the frozen
        # historical templates, and no live renderer's bytes are among them.
        from review_sensei.hosting.github import setup as setup_module

        tag = setup_module.DEFAULT_PUBLIC_WORKFLOW_TAG
        current = _tagged_workflow(tag)
        self.assertEqual(current, setup_module._resolve_trigger_workflow(tag))
        reverted = current.replace(
            "# ReviewSensei setup version: 5",
            "# ReviewSensei setup version: 4",
            1,
        )
        self.assertNotEqual(reverted, current)
        self.assertFalse(
            setup_module._looks_like_managed_v4_setup(
                WORKFLOW_PATH,
                reverted,
            )
        )
        self.assertNotIn(
            current,
            {
                _merge_focused_v4_workflow(tag),
                _released_runner_switch_v4_workflow(tag),
                _provider_parity_workflow(tag),
                _provider_parity_workflow_before_draft_skip(tag),
                setup_module._historical_tagged_v4_workflow(tag),
                setup_module._previous_provider_parity_workflow(tag),
                _historical_provider_parity_workflow(tag),
            },
        )
        self.assertFalse(
            setup_module._looks_like_managed_v4_setup(
                CONFIG_PATH,
                setup_module._current_config_file().replace(
                    "# ReviewSensei setup version: 5",
                    "# ReviewSensei setup version: 4",
                    1,
                ),
            )
        )
        # The uninstall's frozen v4 bytes predate the root configuration path,
        # so the current uninstall reverted to marker 4 matches nothing:
        # recognition there fails closed as well.
        reverted_uninstall = setup_module._current_uninstall_workflow().replace(
            "# ReviewSensei setup version: 5",
            "# ReviewSensei setup version: 4",
            1,
        )
        self.assertNotEqual(reverted_uninstall, _historical_v4_uninstall_workflow())
        self.assertFalse(
            setup_module._looks_like_managed_v4_setup(
                UNINSTALL_WORKFLOW_PATH, reverted_uninstall
            )
        )
        self.assertFalse(
            setup_module._looks_like_managed_v5_setup(
                UNINSTALL_WORKFLOW_PATH, reverted_uninstall
            )
        )
        plan = SetupPlanBuilder().build("owner/repo")
        files = {file.path: file.content for file in plan.files}
        files[WORKFLOW_PATH] = reverted
        self.assertEqual(setup_module._classify_setup_files(files), "unknown")

    def test_immediate_pre_cutover_v4_setup_is_migrated(self):
        plan = SetupPlanBuilder().build("owner/repo")
        files = {file.path: file.content for file in plan.files}
        files[WORKFLOW_PATH] = _merge_focused_v4_workflow("v5")
        files[UNINSTALL_WORKFLOW_PATH] = _historical_v4_uninstall_workflow()
        # A pre-cutover repository carries the retired v4 configuration
        # location and no root configuration file. The retired path is not
        # among the inspected paths, so the classifier sees only managed v4
        # workflow bytes and migrates.
        files.pop(CONFIG_PATH)
        files[LEGACY_CONFIG_PATH] = _v4_with_review_mode_config_file()
        transport = FileTransport(files=files)

        results = SetupPullRequestService(transport).ensure_setup_pull_requests(
            delivery(),
            installation_token="ghs_opaque",
        )

        self.assertEqual(results[0].status, "created")
        branch_request = next(
            r for r in transport.requests if r[0] == "create_or_update_branch"
        )
        written = {file.path: file.content for file in branch_request[6]}
        self.assertEqual(written, {file.path: file.content for file in plan.files})

    def test_missing_v4_companions_are_repaired_by_the_migration(self):
        # The pre-cutover classifier (origin/main) already allowed a missing
        # companion file; the migration PR writes only the three generated
        # paths, so a companion that is absent is added, never overwritten.
        transport = FileTransport(
            files={WORKFLOW_PATH: _merge_focused_v4_workflow("v5")}
        )

        results = SetupPullRequestService(transport).ensure_setup_pull_requests(
            delivery(),
            installation_token="ghs_opaque",
        )

        self.assertEqual(results[0].status, "created")
        self.assertTrue(any(r[0] == "create_pull_request" for r in transport.requests))

    def test_foreign_v4_companion_content_blocks_the_migration(self):
        # A v4-managed workflow must not lift a foreign file at one of the
        # known paths into the migration: the classifier fails closed.
        transport = FileTransport(
            files={
                WORKFLOW_PATH: _merge_focused_v4_workflow("v5"),
                CONFIG_PATH: "provider: custom\n",
            }
        )

        results = SetupPullRequestService(transport).ensure_setup_pull_requests(
            delivery(),
            installation_token="ghs_opaque",
        )

        self.assertEqual(results[0].status, "skipped_unknown_setup")
        self.assertFalse(any(r[0] == "create_pull_request" for r in transport.requests))

    def test_released_provider_parity_v4_setup_with_reply_default_is_migrated(self):
        plan = SetupPlanBuilder().build("owner/repo")
        files = {file.path: file.content for file in plan.files}
        files[WORKFLOW_PATH] = _historical_provider_parity_workflow("v4")
        transport = FileTransport(files=files)

        results = SetupPullRequestService(transport).ensure_setup_pull_requests(
            delivery(),
            installation_token="ghs_opaque",
        )

        self.assertEqual(results[0].status, "created")
        self.assertTrue(any(r[0] == "create_pull_request" for r in transport.requests))

    def test_released_v4_setup_without_draft_skip_is_migrated(self):
        plan = SetupPlanBuilder().build("owner/repo")
        files = {file.path: file.content for file in plan.files}
        files[WORKFLOW_PATH] = _provider_parity_workflow_before_draft_skip("v4")
        self.assertNotIn(
            "github.event.pull_request.draft != true", files[WORKFLOW_PATH]
        )
        transport = FileTransport(files=files)

        results = SetupPullRequestService(transport).ensure_setup_pull_requests(
            delivery(),
            installation_token="ghs_opaque",
        )

        self.assertEqual(results[0].status, "created")
        self.assertTrue(any(r[0] == "create_pull_request" for r in transport.requests))

    def test_released_v5_setup_is_migrated(self):
        plan = SetupPlanBuilder().build("owner/repo")
        files = {file.path: file.content for file in plan.files}
        # A repository set up by the released v5 installer carries the frozen
        # caller and uninstall bytes and no root configuration file.
        files[WORKFLOW_PATH] = _historical_v5_workflow("v5")
        files[UNINSTALL_WORKFLOW_PATH] = _historical_v5_uninstall_workflow()
        files.pop(CONFIG_PATH)
        transport = FileTransport(files=files)

        results = SetupPullRequestService(transport).ensure_setup_pull_requests(
            delivery(),
            installation_token="ghs_opaque",
        )

        self.assertEqual(results[0].status, "created")
        branch_request = next(
            r for r in transport.requests if r[0] == "create_or_update_branch"
        )
        written = {file.path: file.content for file in branch_request[6]}
        self.assertEqual(written, {file.path: file.content for file in plan.files})

    def test_unreleased_caller_edition_is_not_overwritten(self):
        # A caller that carries the release marker but matches neither the
        # current bytes nor a frozen released shape (an unreleased edition, or
        # a hand edit) must fail closed: no branch and no pull request.
        plan = SetupPlanBuilder().build("owner/repo")
        files = {file.path: file.content for file in plan.files}
        current = files[WORKFLOW_PATH]
        edition = current.replace(
            "  issues: read\n  id-token: write\n",
            "  issues: write\n  id-token: write\n",
            1,
        )
        self.assertNotEqual(edition, current)
        self.assertIn("resolve-trigger:", edition)
        files[WORKFLOW_PATH] = edition
        transport = FileTransport(files=files)

        results = SetupPullRequestService(transport).ensure_setup_pull_requests(
            delivery(),
            installation_token="ghs_opaque",
        )

        self.assertEqual(results[0].status, "skipped_unknown_setup")
        self.assertFalse(any(r[0] == "create_pull_request" for r in transport.requests))

    def test_released_runner_switch_v4_caller_is_migrated(self):
        released = self.historical_fixture(
            "released-v4-resolve-trigger-runner-switch.yml"
        )
        self.assertEqual(
            hashlib.sha256(released.encode()).hexdigest(),
            RELEASED_RUNNER_SWITCH_V4_SHA256,
        )
        plan = SetupPlanBuilder().build("owner/repo")
        files = {file.path: file.content for file in plan.files}
        files[WORKFLOW_PATH] = released
        transport = FileTransport(files=files)

        results = SetupPullRequestService(transport).ensure_setup_pull_requests(
            delivery(),
            installation_token="ghs_opaque",
        )

        self.assertEqual(results[0].status, "created")
        self.assertTrue(any(r[0] == "create_pull_request" for r in transport.requests))
        branch_request = next(
            r for r in transport.requests if r[0] == "create_or_update_branch"
        )
        migrated = next(
            file.content for file in branch_request[6] if file.path == WORKFLOW_PATH
        )
        self.assertEqual(migrated, _tagged_workflow("v5"))
        self.assertEqual(_released_runner_switch_v4_workflow("v4"), released)

    def test_released_provider_parity_v4_caller_without_resolve_trigger_is_migrated(
        self,
    ):
        plan = SetupPlanBuilder().build("owner/repo")
        files = {file.path: file.content for file in plan.files}
        files[WORKFLOW_PATH] = _provider_parity_workflow("v4")
        self.assertNotIn("resolve-trigger:", files[WORKFLOW_PATH])
        transport = FileTransport(files=files)

        results = SetupPullRequestService(transport).ensure_setup_pull_requests(
            delivery(),
            installation_token="ghs_opaque",
        )

        self.assertEqual(results[0].status, "created")
        self.assertTrue(any(r[0] == "create_pull_request" for r in transport.requests))
        branch_request = next(
            r for r in transport.requests if r[0] == "create_or_update_branch"
        )
        migrated = next(
            file.content for file in branch_request[6] if file.path == WORKFLOW_PATH
        )
        self.assertIn("resolve-trigger:", migrated)
        combined = "\n".join(file.content for file in branch_request[6])
        for marker in (
            "GITHUB_APP_PRIVATE_KEY",
            "GITHUB_APP_WEBHOOK_SECRET",
            "installation_token",
            "raw webhook",
            "Authorization:",
        ):
            self.assertNotIn(marker, combined)

    def test_released_v3_setup_with_stale_public_workflow_sha_is_migrated(self):
        files = self.historical_v3_files()
        self.assertEqual(
            hashlib.sha256(
                files[".github/workflows/review-sensei-review.yml"].encode()
            ).hexdigest(),
            "18cb5eee42fd6acf10764e37eb5aba99d60abe77cace83a8b2db6150b1e73af3",
        )
        self.assertEqual(
            hashlib.sha256(
                files[".github/workflows/review-sensei-uninstall.yml"].encode()
            ).hexdigest(),
            "fdf0b34c76cd8e0ec7330d307315f7579a4b1fac5a8c5c0cc5f627788c398df8",
        )
        transport = FileTransport(files=files)

        results = SetupPullRequestService(transport).ensure_setup_pull_requests(
            delivery(),
            installation_token="ghs_opaque",
        )

        self.assertEqual(results[0].status, "created")
        self.assertTrue(any(r[0] == "create_pull_request" for r in transport.requests))

    def test_unknown_existing_setup_is_not_overwritten(self):
        transport = FileTransport(
            files={
                ".github/workflows/review-sensei-review.yml": "name: Custom review\n",
            }
        )

        results = SetupPullRequestService(transport).ensure_setup_pull_requests(
            delivery(),
            installation_token="ghs_opaque",
        )

        self.assertEqual(results[0].status, "skipped_unknown_setup")
        self.assertFalse(any(r[0] == "create_pull_request" for r in transport.requests))

    def test_invalid_v4_tag_setup_is_not_overwritten(self):
        workflow = SetupPlanBuilder().build("owner/repo").files[0].content
        transport = FileTransport(
            files={
                ".github/workflows/review-sensei-review.yml": workflow.replace(
                    "@v5", "@v5.lock"
                )
            }
        )

        results = SetupPullRequestService(transport).ensure_setup_pull_requests(
            delivery(),
            installation_token="ghs_opaque",
        )

        self.assertEqual(results[0].status, "skipped_unknown_setup")
        self.assertFalse(any(r[0] == "create_pull_request" for r in transport.requests))

    def test_v5_recognition_ignores_a_second_run_workflow_reference(self):
        # Recognition is byte-exact against the rendered single-reference
        # caller, so a file that mentions the run-workflow reference again (a
        # comment, a duplicated `uses:` line) is a customer edit: it stays
        # `unknown`, writes nothing, and the scoped reference matcher must not
        # turn a mention into a live tag.
        plan = SetupPlanBuilder().build("owner/repo")
        caller = next(file.content for file in plan.files if file.path == WORKFLOW_PATH)
        reference = (
            "malsabbagh/review-sensei/.github/workflows/review-sensei-run.yml@v5"
        )
        variants = {
            # Positive control: the unmodified rendered caller is recognized.
            "current": (caller, "skipped_current"),
            "comment_mention": (
                caller + f"# pinned via {reference}\n",
                "skipped_unknown_setup",
            ),
            "duplicated_uses": (
                caller + f"      uses: {reference}\n",
                "skipped_unknown_setup",
            ),
        }
        for name, (content, expected) in variants.items():
            with self.subTest(variant=name):
                files = {file.path: file.content for file in plan.files}
                files[WORKFLOW_PATH] = content
                transport = FileTransport(branch_exists=True, files=files)

                results = SetupPullRequestService(transport).ensure_setup_pull_requests(
                    delivery(),
                    installation_token="ghs_opaque",
                )

                self.assertEqual(results[0].status, expected)
                self.assertFalse(
                    any(r[0] == "create_pull_request" for r in transport.requests)
                )

    def test_marker_only_v3_setup_is_treated_as_custom(self):
        transport = FileTransport(
            files={
                ".github/workflows/review-sensei-review.yml": (
                    "# ReviewSensei setup version: 3\nname: ReviewSensei review\n"
                ),
            }
        )

        results = SetupPullRequestService(transport).ensure_setup_pull_requests(
            delivery(),
            installation_token="ghs_opaque",
        )

        self.assertEqual(results[0].status, "skipped_unknown_setup")
        self.assertFalse(any(r[0] == "create_pull_request" for r in transport.requests))

    def test_marker_only_v2_setup_is_treated_as_custom(self):
        transport = FileTransport(
            files={
                ".github/workflows/review-sensei-review.yml": (
                    "# ReviewSensei setup version: 2\nname: ReviewSensei review\n"
                ),
            }
        )

        results = SetupPullRequestService(transport).ensure_setup_pull_requests(
            delivery(),
            installation_token="ghs_opaque",
        )

        self.assertEqual(results[0].status, "skipped_unknown_setup")
        self.assertFalse(any(r[0] == "create_pull_request" for r in transport.requests))

    def test_released_pre_marker_clients_are_migrated(self):
        for fixture in (
            "pre-marker-worker-review.yml",
            "pre-marker-python-review.yml",
        ):
            with self.subTest(fixture=fixture):
                transport = FileTransport(
                    files={
                        ".github/workflows/review-sensei-review.yml": (
                            self.historical_fixture(fixture)
                        ),
                        ".github/workflows/review-sensei-uninstall.yml": None,
                        ".github/review-sensei/config.yml": self.historical_fixture(
                            "pre-marker-config.yml"
                        ),
                    }
                )

                results = SetupPullRequestService(transport).ensure_setup_pull_requests(
                    delivery(),
                    installation_token="ghs_opaque",
                )

                self.assertEqual(results[0].status, "created")
                self.assertTrue(
                    any(r[0] == "create_pull_request" for r in transport.requests)
                )

    def test_complete_cross_runtime_v2_clients_are_migrated(self):
        for review, uninstall in (
            ("v2-worker-review.yml", "v2-worker-uninstall.yml"),
            ("v2-python-review.yml", "v2-python-uninstall.yml"),
        ):
            with self.subTest(review=review):
                transport = FileTransport(
                    files={
                        ".github/workflows/review-sensei-review.yml": (
                            self.historical_fixture(review)
                        ),
                        ".github/workflows/review-sensei-uninstall.yml": (
                            self.historical_fixture(uninstall)
                        ),
                        ".github/review-sensei/config.yml": self.historical_fixture(
                            "v2-config.yml"
                        ),
                    }
                )

                results = SetupPullRequestService(transport).ensure_setup_pull_requests(
                    delivery(),
                    installation_token="ghs_opaque",
                )

                self.assertEqual(results[0].status, "created")

    def test_released_intermediate_pre_marker_setup_is_migrated(self):
        transport = FileTransport(
            files={
                ".github/workflows/review-sensei-review.yml": (
                    self.historical_fixture("intermediate-pre-marker-worker-review.yml")
                ),
                ".github/workflows/review-sensei-uninstall.yml": (
                    self.historical_fixture(
                        "intermediate-pre-marker-worker-uninstall.yml"
                    )
                ),
                ".github/review-sensei/config.yml": self.historical_fixture(
                    "intermediate-pre-marker-config.yml"
                ),
            }
        )

        results = SetupPullRequestService(transport).ensure_setup_pull_requests(
            delivery(),
            installation_token="ghs_opaque",
        )

        self.assertEqual(results[0].status, "created")
        self.assertTrue(any(r[0] == "create_pull_request" for r in transport.requests))

    def test_customized_partial_v3_setup_is_not_overwritten(self):
        plan = SetupPlanBuilder().build("owner/repo")
        workflow = next(
            file.content for file in plan.files if file.path.endswith("review.yml")
        )
        transport = FileTransport(
            files={
                ".github/workflows/review-sensei-review.yml": workflow.replace(
                    "name: ReviewSensei review",
                    "name: Customer ReviewSensei review",
                )
            }
        )

        results = SetupPullRequestService(transport).ensure_setup_pull_requests(
            delivery(),
            installation_token="ghs_opaque",
        )

        self.assertEqual(results[0].status, "skipped_unknown_setup")
        self.assertFalse(any(r[0] == "create_pull_request" for r in transport.requests))

    def test_future_setup_version_is_not_overwritten(self):
        transport = FileTransport(
            files={
                ".github/workflows/review-sensei-review.yml": (
                    "# ReviewSensei setup version: 99\n"
                ),
            }
        )

        results = SetupPullRequestService(transport).ensure_setup_pull_requests(
            delivery(),
            installation_token="ghs_opaque",
        )

        self.assertEqual(results[0].status, "skipped_unknown_setup")
        self.assertFalse(any(r[0] == "create_pull_request" for r in transport.requests))

    def test_existing_branch_and_existing_pull_request_skips_duplicate(self):
        transport = FakeTransport(branch_exists=True, existing_prs=[{"number": 10}])
        results = SetupPullRequestService(transport).ensure_setup_pull_requests(
            delivery(),
            installation_token="ghs_opaque",
        )

        self.assertEqual(results[0].status, "skipped_pull_request_exists")
        self.assertEqual(results[0].pull_request_number, 10)
        self.assertFalse(any(r[0] == "create_pull_request" for r in transport.requests))

    def test_existing_pull_request_skips_duplicate(self):
        transport = FakeTransport(existing_prs=[{"number": 10}])
        results = SetupPullRequestService(transport).ensure_setup_pull_requests(
            delivery(),
            installation_token="ghs_opaque",
        )

        self.assertEqual(results[0].status, "skipped_pull_request_exists")
        self.assertEqual(results[0].pull_request_number, 10)
        self.assertFalse(any(r[0] == "create_pull_request" for r in transport.requests))

    def test_rechecks_existing_pull_request_before_creating(self):
        transport = RecheckTransport()
        results = SetupPullRequestService(transport).ensure_setup_pull_requests(
            delivery(),
            installation_token="ghs_opaque",
        )

        self.assertEqual(results[0].status, "skipped_pull_request_exists")
        self.assertEqual(results[0].pull_request_number, 11)
        self.assertTrue(
            any(r[0] == "create_or_update_branch" for r in transport.requests)
        )
        self.assertFalse(any(r[0] == "create_pull_request" for r in transport.requests))

    def test_suspended_delivery_does_no_writes(self):
        transport = FakeTransport()
        results = SetupPullRequestService(transport).ensure_setup_pull_requests(
            delivery(suspended=True),
            installation_token="ghs_opaque",
        )

        self.assertEqual(results, [])
        self.assertEqual(transport.requests, [])

    def test_removed_and_deleted_actions_do_no_writes(self):
        for action in ("removed", "deleted", "unsuspend", "suspend"):
            with self.subTest(action=action):
                transport = FakeTransport()
                results = SetupPullRequestService(transport).ensure_setup_pull_requests(
                    delivery(action=action),
                    installation_token="ghs_opaque",
                )
                self.assertEqual(results, [])
                self.assertEqual(transport.requests, [])

    def test_permission_insufficient_returns_skipped(self):
        results = SetupPullRequestService(FakeTransport()).ensure_setup_pull_requests(
            delivery(),
            installation_token="ghs_opaque",
            permissions={"contents": "read", "pull_requests": "write"},
        )

        self.assertEqual(results[0].status, "skipped_permissions")

    def test_actions_variables_permission_alias_is_accepted(self):
        permissions = {
            "contents": "write",
            "pull_requests": "write",
            "actions_variables": "write",
            "workflows": "write",
        }
        results = SetupPullRequestService(FakeTransport()).ensure_setup_pull_requests(
            delivery(),
            installation_token="ghs_opaque",
            permissions=permissions,
        )

        self.assertEqual(results[0].status, "created")

    def test_permissions_from_verified_delivery_are_enforced(self):
        bad = delivery()
        object.__setattr__(
            bad,
            "permissions",
            {"contents": "read", "pull_requests": "write"},
        )
        results = SetupPullRequestService(FakeTransport()).ensure_setup_pull_requests(
            bad,
            installation_token="ghs_opaque",
        )

        self.assertEqual(results[0].status, "skipped_permissions")

    def test_unsupported_event_and_invalid_repository_fail_closed(self):
        bad = delivery()
        object.__setattr__(bad, "event", "push")
        results = SetupPullRequestService(FakeTransport()).ensure_setup_pull_requests(
            bad,
            installation_token="ghs_opaque",
        )
        self.assertEqual(results, [])

        bad_repo = delivery()
        object.__setattr__(bad_repo, "repository", "../outside")
        with self.assertRaises(GitHubSetupError):
            SetupPullRequestService(FakeTransport()).ensure_setup_pull_requests(
                bad_repo,
                installation_token="ghs_opaque",
            )

    def test_new_permissions_accepted_creates_setup_pr(self):
        transport = FakeTransport()
        results = SetupPullRequestService(transport).ensure_setup_pull_requests(
            delivery(action="new_permissions_accepted"),
            installation_token="ghs_opaque",
        )
        self.assertEqual(results[0].status, "created")

    def test_new_permissions_accepted_creates_one_pr_per_delivery_repo(self):
        transport = FakeTransport()
        results = SetupPullRequestService(transport).ensure_setup_pull_requests(
            delivery(
                action="new_permissions_accepted",
                repositories=("owner/one", "owner/two"),
            ),
            installation_token="ghs_opaque",
        )

        self.assertEqual(
            [r.repository for r in results], ["owner/repo", "owner/one", "owner/two"]
        )
        self.assertEqual([r.status for r in results], ["created", "created", "created"])

    def test_generated_files_and_pr_body_have_no_secret_markers(self):
        transport = FakeTransport()
        results = SetupPullRequestService(transport).ensure_setup_pull_requests(
            delivery(),
            installation_token="ghs_opaque",
        )
        self.assertEqual(results[0].status, "created")
        create_request = next(
            r for r in transport.requests if r[0] == "create_pull_request"
        )
        body = create_request[5]
        for marker in (
            "ghs_opaque",
            "GITHUB_APP_PRIVATE_KEY",
            "GITHUB_APP_WEBHOOK_SECRET",
            "Bearer ",
            "webhook body",
        ):
            self.assertNotIn(marker, body)


class GitHubSetupClientTests(unittest.TestCase):
    def make_client(self, responses):
        calls = []

        def opener(request, timeout):
            calls.append((request.method, request.full_url, request.headers))
            if isinstance(responses, list):
                response = responses.pop(0)
            else:
                response = responses
            if isinstance(response, Exception):
                raise response
            status, body = response
            return FakeHTTPResponse(body, status)

        return GitHubSetupClient(
            api_url="https://api.github.test", opener=opener
        ), calls

    def test_client_get_default_branch_request_shape(self):
        client, calls = self.make_client((200, b'{"default_branch":"main"}'))

        self.assertEqual(
            client.get_default_branch(
                repository="owner/repo",
                installation_token="ghs_opaque",
            ),
            "main",
        )
        method, url, headers = calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, "https://api.github.test/repos/owner/repo")
        self.assertEqual(headers["Authorization"], "Bearer ghs_opaque")

    def test_client_branch_exists_returns_false_on_404(self):
        client, _ = self.make_client((404, b""))
        self.assertFalse(
            client.branch_exists(
                repository="owner/repo",
                installation_token="ghs_opaque",
                branch="review-sensei/setup",
            )
        )

    def test_client_reads_bounded_repository_file(self):
        content = "# ReviewSensei setup version: 2\n"
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        client, calls = self.make_client(
            (
                200,
                json.dumps(
                    {"type": "file", "encoding": "base64", "content": encoded}
                ).encode("utf-8"),
            )
        )

        self.assertEqual(
            client.get_repository_file(
                repository="owner/repo",
                installation_token="ghs_opaque",
                base_branch="feature/main",
                path=".github/review-sensei/config.yml",
            ),
            content,
        )
        self.assertIn(
            "/contents/.github/review-sensei/config.yml?ref=feature%2Fmain",
            calls[0][1],
        )

    def test_client_reads_missing_repository_file_as_none(self):
        client, _ = self.make_client((404, b""))
        self.assertIsNone(
            client.get_repository_file(
                repository="owner/repo",
                installation_token="ghs_opaque",
                base_branch="main",
                path=".github/review-sensei/config.yml",
            )
        )

    def test_client_create_pull_request_payload(self):
        captured = {}

        def opener(request, timeout):
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["url"] = request.full_url
            return FakeHTTPResponse(b'{"number":42}')

        client = GitHubSetupClient(api_url="https://api.github.test", opener=opener)
        result = client.create_pull_request(
            repository="owner/repo",
            installation_token="ghs_opaque",
            base_branch="main",
            head_branch="review-sensei/setup",
            title="ReviewSensei review setup",
            body="body",
        )

        self.assertEqual(result["number"], 42)
        self.assertEqual(
            captured["url"],
            "https://api.github.test/repos/owner/repo/pulls",
        )
        self.assertEqual(captured["body"]["head"], "review-sensei/setup")
        self.assertEqual(captured["body"]["base"], "main")

    def test_client_maps_transient_and_sanitized_errors(self):
        error = HTTPError(
            "https://api.github.test/repos/owner/repo",
            500,
            "error",
            {},
            FakeHTTPResponse(b'{"message":"PRIVATE_BODY"}', 500),
        )
        client, _ = self.make_client(error)

        with self.assertRaises(GitHubSetupTransientError) as raised:
            client.get_default_branch(
                repository="owner/repo",
                installation_token="ghs_opaque",
            )
        self.assertEqual(raised.exception.error_category, "github_setup_transient")
        self.assertNotIn("PRIVATE_BODY", str(raised.exception))

    def test_client_constructor_rejects_invalid_values(self):
        with self.assertRaises(GitHubSetupError):
            GitHubSetupClient(api_url="")
        with self.assertRaises(GitHubSetupError):
            GitHubSetupClient(timeout=0)

    def test_client_create_or_update_branch_request_sequence(self):
        responses = [
            (200, b'{"sha":"tree-sha"}'),
            (200, b'{"sha":"commit-sha"}'),
            (201, b'{"ref":"refs/heads/review-sensei/setup"}'),
        ]
        client, calls = self.make_client(responses)

        created = client.create_or_update_branch(
            repository="owner/repo",
            installation_token="ghs_opaque",
            base_branch="main",
            base_sha=BASE_SHA,
            branch="review-sensei/setup",
            files=[
                SetupFile(
                    path=".github/review-sensei/config.yml",
                    content="provider: ollama\n",
                )
            ],
        )

        self.assertTrue(created)
        self.assertEqual(len(calls), 3)
        self.assertIn("git/trees", calls[0][1])
        self.assertIn("git/commits", calls[1][1])
        self.assertIn("git/refs", calls[2][1])

    def test_client_verifies_managed_setup_branch_shape(self):
        plan = SetupPlanBuilder().build("owner/repo")
        responses = [
            (
                200,
                json.dumps(
                    {
                        "commit": {
                            "sha": "c" * 40,
                            "commit": {
                                "message": "Add ReviewSensei review setup files"
                            },
                            "parents": [{"sha": "b" * 40}],
                            "author": {"login": "reviewsensei[bot]", "type": "Bot"},
                        }
                    }
                ).encode("utf-8"),
            ),
            (
                200,
                b'{"files":[{"filename":".reviewsensei.yml"}]}',
            ),
            *[
                (
                    200,
                    json.dumps(
                        {
                            "type": "file",
                            "encoding": "base64",
                            "truncated": False,
                            "content": base64.b64encode(
                                next(
                                    file.content
                                    for file in plan.files
                                    if file.path == path
                                ).encode("utf-8")
                            ).decode("ascii"),
                        }
                    ).encode("utf-8"),
                )
                for path in (
                    ".github/workflows/review-sensei-review.yml",
                    ".github/workflows/review-sensei-uninstall.yml",
                    ".reviewsensei.yml",
                )
            ],
        ]
        client, calls = self.make_client(responses)

        self.assertTrue(
            client.is_managed_setup_branch(
                repository="owner/repo",
                installation_token="ghs_opaque",
                branch="review-sensei/setup",
                base_sha=BASE_SHA,
                public_workflow_tag="v5",
            )
        )
        self.assertIn("/compare/", calls[1][1])

        client, calls = self.make_client(
            (
                200,
                json.dumps(
                    {
                        "commit": {
                            "sha": "c" * 40,
                            "commit": {"message": "Customer branch"},
                            "parents": [{"sha": "b" * 40}],
                        }
                    }
                ).encode("utf-8"),
            )
        )
        self.assertFalse(
            client.is_managed_setup_branch(
                repository="owner/repo",
                installation_token="ghs_opaque",
                branch="review-sensei/setup",
                base_sha=BASE_SHA,
                public_workflow_tag="v5",
            )
        )
        self.assertEqual(len(calls), 1)

    def test_client_create_only_branch_reports_ref_collision_without_patch(self):
        responses = [
            (200, b'{"sha":"tree-sha"}'),
            (200, b'{"sha":"commit-sha"}'),
            (422, b""),
        ]
        client, calls = self.make_client(responses)

        created = client.create_or_update_branch(
            repository="owner/repo",
            installation_token="ghs_opaque",
            base_branch="main",
            base_sha=BASE_SHA,
            branch="review-sensei/setup",
            files=[
                SetupFile(
                    path=".github/review-sensei/config.yml",
                    content="provider: ollama\n",
                )
            ],
        )

        self.assertFalse(created)
        self.assertEqual(len(calls), 3)
        self.assertNotIn("PATCH", [call[0] for call in calls])

    def test_client_create_or_update_branch_rejects_malformed_responses(self):
        bad_responses = [
            [(200, b"[]")],
            [(200, b"{}")],
            [
                (200, b"{}"),
                (200, b"{}"),
            ],
            [
                (200, b'{"sha":"tree-sha"}'),
                (200, b"{}"),
            ],
        ]
        for responses in bad_responses:
            with self.subTest(responses=responses):
                client, _ = self.make_client(responses)
                with self.assertRaises(GitHubSetupError):
                    client.create_or_update_branch(
                        repository="owner/repo",
                        installation_token="ghs_opaque",
                        base_branch="main",
                        base_sha=BASE_SHA,
                        branch="review-sensei/setup",
                        files=[
                            SetupFile(
                                path=".github/review-sensei/config.yml",
                                content="x",
                            )
                        ],
                    )

    def test_client_rejects_non_list_pull_request_response(self):
        client, _ = self.make_client((200, b"{}"))
        with self.assertRaises(GitHubSetupError):
            client.list_pull_requests(
                repository="owner/repo",
                installation_token="ghs_opaque",
                head_branch="review-sensei/setup",
            )

    def test_client_rejects_non_dict_pull_request_response(self):
        client, _ = self.make_client((200, b"[]"))
        with self.assertRaises(GitHubSetupError):
            client.create_pull_request(
                repository="owner/repo",
                installation_token="ghs_opaque",
                base_branch="main",
                head_branch="review-sensei/setup",
                title="title",
                body="body",
            )

    def test_client_rejects_invalid_utf8_and_json(self):
        for raw in (b"\xff", b"{"):
            with self.subTest(raw=raw):
                client, _ = self.make_client((200, raw))
                with self.assertRaises(GitHubSetupError):
                    client.get_default_branch(
                        repository="owner/repo",
                        installation_token="ghs_opaque",
                    )

    def test_client_rejects_non_bytes_and_oversized_responses(self):
        class BadResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def read(self, size):
                return None

        client = GitHubSetupClient(
            api_url="https://api.github.test",
            opener=lambda request, timeout: BadResponse(),
        )
        with self.assertRaises(GitHubSetupError):
            client.get_default_branch(
                repository="owner/repo",
                installation_token="ghs_opaque",
            )

        oversized = b"x" * (512 * 1024 + 1)
        client2 = GitHubSetupClient(
            api_url="https://api.github.test",
            opener=lambda request, timeout: FakeHTTPResponse(oversized, 200),
        )
        with self.assertRaises(GitHubSetupError):
            client2.get_default_branch(
                repository="owner/repo",
                installation_token="ghs_opaque",
            )

    def test_client_maps_urlerror_and_oserror_timeout(self):
        client = GitHubSetupClient(
            api_url="https://api.github.test",
            opener=lambda request, timeout: (_ for _ in ()).throw(URLError("dns")),
        )
        with self.assertRaises(GitHubSetupTransientError):
            client.get_default_branch(
                repository="owner/repo",
                installation_token="ghs_opaque",
            )

        client2 = GitHubSetupClient(
            api_url="https://api.github.test",
            opener=lambda request, timeout: (_ for _ in ()).throw(
                TimeoutError("timed out")
            ),
        )
        with self.assertRaises(GitHubSetupTransientError):
            client2.get_default_branch(
                repository="owner/repo",
                installation_token="ghs_opaque",
            )

    def test_client_maps_status_categories(self):
        for status in (401, 403, 404, 429, 400, 500):
            with self.subTest(status=status):
                client, _ = self.make_client((status, b"{}"))
                with self.assertRaises((GitHubSetupError, GitHubSetupTransientError)):
                    client.get_default_branch(
                        repository="owner/repo",
                        installation_token="ghs_opaque",
                    )


if __name__ == "__main__":
    unittest.main()
