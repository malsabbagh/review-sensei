import base64
import io
import json
import unittest
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
    ):
        self.requests = []
        self._branch_exists = branch_exists if branch_exists is not None else False
        self.existing_prs = existing_prs or []
        self.created_pr_number = created_pr_number
        self.default_branch = default_branch

    def get_default_branch(self, *, repository, installation_token):
        self.requests.append(("get_default_branch", repository, installation_token))
        return self.default_branch

    def branch_exists(self, *, repository, installation_token, branch):
        self.requests.append(("branch_exists", repository, installation_token, branch))
        return self._branch_exists

    def create_or_update_branch(
        self, *, repository, installation_token, base_branch, branch, files
    ):
        self.requests.append(
            (
                "create_or_update_branch",
                repository,
                installation_token,
                base_branch,
                branch,
                files,
            )
        )

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
        self, *, repository, installation_token, base_branch, branch, files
    ):
        super().create_or_update_branch(
            repository=repository,
            installation_token=installation_token,
            base_branch=base_branch,
            branch=branch,
            files=files,
        )
        self.pr_appeared_after_branch = True


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

    def test_build_plan_contains_immutable_actions_and_no_secret_markers(self):
        plan = SetupPlanBuilder().build("owner/repo")

        self.assertEqual(plan.branch_name, "review-sensei/setup")
        self.assertEqual(len(plan.files), 3)
        workflow = dict((f.path, f.content) for f in plan.files)[
            ".github/workflows/review-sensei-review.yml"
        ]
        self.assertIn(
            "actions/checkout@d23441a48e516b6c34aea4fa41551a30e30af803", workflow
        )
        self.assertIn(
            "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1", workflow
        )
        self.assertIn(
            r're.fullmatch(r"v?[0-9]+\.[0-9]+\.[0-9]+", raw)',
            workflow,
        )
        self.assertIn("REVIEWSENSEI_PROVIDER_MODE", workflow)
        self.assertIn("qwen3.5:4b", workflow)
        self.assertIn("deepseek-v4-flash:cloud", workflow)
        uninstall = dict((f.path, f.content) for f in plan.files)[
            ".github/workflows/review-sensei-uninstall.yml"
        ]
        self.assertIn("Remove ReviewSensei setup", uninstall)
        combined = "\n".join((f.content for f in plan.files)) + "\n" + plan.body
        for marker in (
            "GITHUB_APP_PRIVATE_KEY",
            "GITHUB_APP_WEBHOOK_SECRET",
            "installation_token",
            "raw webhook",
            "Authorization:",
        ):
            self.assertNotIn(marker, combined)

    def test_build_rejects_unsafe_repository(self):
        with self.assertRaises(GitHubSetupError):
            SetupPlanBuilder().build("../outside")

    def test_builder_rejects_invalid_branch_and_title(self):
        with self.assertRaises(GitHubSetupError):
            SetupPlanBuilder(branch_name="")
        with self.assertRaises(GitHubSetupError):
            SetupPlanBuilder(branch_name="../bad")
        with self.assertRaises(GitHubSetupError):
            SetupPlanBuilder(title="")


class SetupPullRequestServiceTests(unittest.TestCase):
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

    def test_existing_branch_still_creates_missing_setup_pr(self):
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

    def test_legacy_setup_refreshes_existing_branch_for_migration_pr(self):
        transport = FileTransport(
            branch_exists=True,
            files={
                ".github/workflows/review-sensei-review.yml": (
                    "name: ReviewSensei review\n"
                    "review_sensei_version: old\n"
                    "prepare-diff\n"
                ),
                ".github/review-sensei/config.yml": (
                    "provider: ollama\nbase_url: http://127.0.0.1:11434/api\n"
                ),
            },
        )

        results = SetupPullRequestService(transport).ensure_setup_pull_requests(
            delivery(),
            installation_token="ghs_opaque",
        )

        self.assertEqual(results[0].status, "created")
        self.assertTrue(
            any(r[0] == "create_or_update_branch" for r in transport.requests)
        )
        self.assertTrue(any(r[0] == "create_pull_request" for r in transport.requests))

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

    def test_client_creates_missing_repository_variables_without_overwriting(self):
        responses = [
            (404, b""),
            (201, b'{"name":"REVIEWSENSEI_PROVIDER_MODE"}'),
            (200, b'{"name":"REVIEWSENSEI_LOCAL_MODEL"}'),
        ]
        client, calls = self.make_client(responses)

        client.ensure_repository_variables(
            repository="owner/repo",
            installation_token="ghs_opaque",
            variables=(
                ("REVIEWSENSEI_PROVIDER_MODE", "local"),
                ("REVIEWSENSEI_LOCAL_MODEL", "qwen3.5:4b"),
            ),
        )

        self.assertEqual(calls[0][0], "GET")
        self.assertIn("actions/variables/REVIEWSENSEI_PROVIDER_MODE", calls[0][1])
        self.assertEqual(calls[1][0], "POST")
        self.assertIn("actions/variables", calls[1][1])
        self.assertEqual(calls[2][0], "GET")

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
            (200, b'{"object":{"sha":"base-sha"}}'),
            (200, b'{"sha":"tree-sha"}'),
            (200, b'{"sha":"commit-sha"}'),
            (201, b'{"ref":"refs/heads/review-sensei/setup"}'),
        ]
        client, calls = self.make_client(responses)

        client.create_or_update_branch(
            repository="owner/repo",
            installation_token="ghs_opaque",
            base_branch="main",
            branch="review-sensei/setup",
            files=[
                SetupFile(
                    path=".github/review-sensei/config.yml",
                    content="provider: ollama\n",
                )
            ],
        )

        self.assertEqual(len(calls), 4)
        self.assertIn("git/trees", calls[1][1])
        self.assertIn("git/commits", calls[2][1])
        self.assertIn("git/refs", calls[3][1])

    def test_client_create_or_update_branch_updates_existing_ref_on_422(self):
        responses = [
            (200, b'{"object":{"sha":"base-sha"}}'),
            (200, b'{"sha":"tree-sha"}'),
            (200, b'{"sha":"commit-sha"}'),
            (422, b""),
            (200, b"{}"),
        ]
        client, calls = self.make_client(responses)

        client.create_or_update_branch(
            repository="owner/repo",
            installation_token="ghs_opaque",
            base_branch="main",
            branch="review-sensei/setup",
            files=[
                SetupFile(
                    path=".github/review-sensei/config.yml",
                    content="provider: ollama\n",
                )
            ],
        )

        self.assertEqual(len(calls), 5)
        self.assertEqual(calls[4][0], "PATCH")

    def test_client_create_or_update_branch_rejects_malformed_responses(self):
        bad_responses = [
            [(200, b"[]")],
            [(200, b"{}")],
            [
                (200, b'{"object":{"sha":"base-sha"}}'),
                (200, b"{}"),
            ],
            [
                (200, b'{"object":{"sha":"base-sha"}}'),
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
