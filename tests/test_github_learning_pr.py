import base64
import copy
import json
import unittest
from itertools import count
from unittest.mock import patch
from urllib.parse import urlsplit

from review_sensei.hosting.github import (
    GitHubHTTPError,
    GitHubHTTPResponseTooLargeError,
    GitHubLearningProposalError,
    GitHubLearningProposalTransientError,
    LearningPRPublisher,
)
from review_sensei.hosting.github.learning_pr import (
    _branch_for_pr,
    _decode_base64,
    _has_marker,
    _is_sha,
    _is_sha256,
    _marker_line,
    _normalized_proposals,
    _parse_github_timestamp,
    _parse_marker_line,
    _parse_provenance,
    _raise_response,
    _sanitize_display_text,
    _sha256_text,
    _truncate_bytes,
    batch_digest,
    learning_path,
    proposal_digest,
)
from review_sensei.models import LearningProposal


class FakeGitHub:
    """Small stateful REST fake exercising the publisher's object protocol."""

    def __init__(self):
        self.repository = "owner/repo"
        self.repository_id = 1
        self.base_sha = "a" * 40
        self.source_sha = "b" * 40
        self.source_title = "Improve review boundaries"
        self.source_title_events = []
        self.next_sha = count(3)
        self.calls = []
        self.refs = {}
        self.blobs = {}
        self.base_contents = {}
        self.trees = {"f" * 40: []}
        self.commits = {self.base_sha: {"tree": {"sha": "f" * 40}, "parents": []}}
        self.prs = {}

    def repository_path(self, repository, suffix=""):
        return f"/repos/{repository}{suffix}"

    def _sha(self):
        value = format(next(self.next_sha), "x")
        return (value * 40)[:40]

    def _source(self):
        return {
            "number": 1,
            "title": self.source_title,
            "html_url": "https://github.com/owner/repo/pull/1",
            "state": "open",
            "draft": False,
            "head": {
                "sha": self.source_sha,
                "ref": "feature",
                "repo": {"id": 1, "full_name": self.repository, "fork": False},
            },
            "base": {
                "ref": "main",
                "sha": self.base_sha,
                "repo": {"id": 1, "full_name": self.repository, "fork": False},
            },
        }

    def _pr_summary(self, number, detail):
        return {
            "number": number,
            "state": detail["state"],
            "draft": detail["draft"],
            "body": detail["body"],
            "head": {"ref": detail["head"]["ref"], "sha": detail["head"]["sha"]},
            "base": {"ref": detail["base"]["ref"]},
        }

    def request(self, method, path, *, token, body=None):
        self.calls.append((method, path, body))
        parsed = urlsplit(path)
        route = parsed.path
        if method == "GET" and route == "/repos/owner/repo":
            return 200, {
                "id": self.repository_id,
                "full_name": self.repository,
                "fork": False,
                "default_branch": "main",
            }
        if method == "GET" and route == "/repos/owner/repo/pulls/1":
            return 200, self._source()
        if method == "GET" and route == "/repos/owner/repo/issues/1/timeline":
            return 200, list(self.source_title_events)
        if method == "GET" and route.startswith("/repos/owner/repo/pulls/"):
            number = int(route.rsplit("/", 1)[-1])
            detail = self.prs.get(number)
            return (200, detail) if detail else (404, {})
        if method == "GET" and route == "/repos/owner/repo/pulls":
            state = dict(
                pair.split("=") for pair in parsed.query.split("&") if "=" in pair
            ).get("state")
            values = [
                self._pr_summary(number, detail)
                for number, detail in sorted(self.prs.items())
                if state == "all" or detail["state"] == state
            ]
            return 200, values
        if method == "GET" and route == "/repos/owner/repo/git/ref/heads/main":
            return 200, {"object": {"sha": self.base_sha}}
        if method == "GET" and route.startswith("/repos/owner/repo/git/ref/heads/"):
            branch = route.split("/git/ref/heads/", 1)[1]
            sha = self.refs.get(branch)
            return (200, {"object": {"sha": sha}}) if sha else (404, {})
        if method == "GET" and route.startswith("/repos/owner/repo/git/commits/"):
            sha = route.rsplit("/", 1)[-1]
            commit = self.commits.get(sha)
            if not commit:
                return 404, {}
            return 200, {"sha": sha, **commit}
        if method == "GET" and route.startswith("/repos/owner/repo/git/trees/"):
            tree_sha = route.rsplit("/", 1)[-1]
            return 200, {"truncated": False, "tree": self.trees.get(tree_sha, [])}
        if method == "GET" and route.startswith("/repos/owner/repo/git/blobs/"):
            sha = route.rsplit("/", 1)[-1]
            content = self.blobs.get(sha)
            return (
                (
                    200,
                    {
                        "encoding": "base64",
                        "content": base64.b64encode(content.encode()).decode(),
                    },
                )
                if content is not None
                else (404, {})
            )
        if method == "GET" and route.startswith("/repos/owner/repo/compare/"):
            commit_sha = route.rsplit("...", 1)[-1]
            commit = self.commits.get(commit_sha, {})
            tree = self.trees.get(commit.get("tree", {}).get("sha"), [])
            return 200, {
                "files": [
                    {"filename": item["path"], "status": "added"} for item in tree
                ]
            }
        if method == "GET" and route.startswith("/repos/owner/repo/contents/"):
            path = route.split("/contents/", 1)[1]
            content = self.base_contents.get(path)
            if content is not None:
                return 200, {
                    "encoding": "base64",
                    "content": base64.b64encode(content.encode()).decode(),
                }
            return 404, {}
        if method == "POST" and route == "/repos/owner/repo/git/blobs":
            sha = self._sha()
            self.blobs[sha] = body["content"]
            return 201, {"sha": sha}
        if method == "POST" and route == "/repos/owner/repo/git/trees":
            sha = self._sha()
            self.trees[sha] = list(body["tree"])
            return 201, {"sha": sha}
        if method == "POST" and route == "/repos/owner/repo/git/commits":
            sha = self._sha()
            self.commits[sha] = {
                "message": body["message"],
                "parents": [{"sha": parent} for parent in body["parents"]],
                "tree": {"sha": body["tree"]},
            }
            return 201, {"sha": sha}
        if method == "POST" and route == "/repos/owner/repo/git/refs":
            branch = body["ref"].removeprefix("refs/heads/")
            if branch in self.refs:
                return 422, {}
            self.refs[branch] = body["sha"]
            return 201, {}
        if method == "PATCH" and route.startswith("/repos/owner/repo/git/refs/heads/"):
            branch = route.split("/git/refs/heads/", 1)[1]
            if branch not in self.refs:
                return 404, {}
            self.refs[branch] = body["sha"]
            for detail in self.prs.values():
                if detail["head"]["ref"] == branch:
                    detail["head"]["sha"] = body["sha"]
            return 200, {}
        if method == "POST" and route == "/repos/owner/repo/pulls":
            number = max(self.prs, default=1) + 1
            self.prs[number] = {
                "number": number,
                "title": body["title"],
                "body": body["body"],
                "state": "open",
                "draft": True,
                "merged": False,
                "created_at": "2026-09-02T00:00:00Z",
                "head": {
                    "ref": body["head"],
                    "sha": self.refs[body["head"]],
                    "repo": {
                        "id": self.repository_id,
                        "full_name": self.repository,
                        "fork": False,
                    },
                },
                "base": {
                    "ref": body["base"],
                    "sha": self.base_sha,
                    "repo": {"id": 1, "full_name": self.repository, "fork": False},
                },
            }
            return 201, {"number": number}
        if method == "PATCH" and route.startswith("/repos/owner/repo/pulls/"):
            number = int(route.rsplit("/", 1)[-1])
            detail = self.prs[number]
            detail.update({"title": body["title"], "body": body["body"], "draft": True})
            return 200, detail
        raise AssertionError(f"unhandled fake request: {method} {path}")


def proposal(title="T", rule="R", **kwargs):
    return LearningProposal(title=title, rule=rule, **kwargs)


def _content(value):
    digest = proposal_digest(value)
    return (
        json.dumps(
            value.to_entry(id=f"sensei-{digest[:16]}").to_dict(),
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )


class LearningPRPublisherTests(unittest.TestCase):
    def publish(self, fake, proposals, *, head_sha=None):
        return LearningPRPublisher(http=fake).propose_batch(
            token="token",
            repository="owner/repo",
            repository_id=1,
            pull_request=1,
            head_sha=head_sha or fake.source_sha,
            base_branch="main",
            base_sha=fake.base_sha,
            proposals=proposals,
        )

    def test_creates_descriptive_single_draft_and_provenance(self):
        fake = FakeGitHub()
        first, second = proposal("First"), proposal("Second")
        result = self.publish(fake, (second, first))
        self.assertEqual(result.status, "created")
        self.assertEqual(result.pull_request_number, 2)
        pr = fake.prs[2]
        self.assertTrue(pr["draft"])
        self.assertEqual(pr["head"]["ref"], "review-sensei/learnings/pr-1")
        self.assertIn(
            "ReviewSensei learnings from #1: Improve review boundaries", pr["title"]
        )
        self.assertIn("Proposal count: 2", pr["body"])
        self.assertRegex(
            pr["body"].splitlines()[-1],
            r"reviewsensei:learning:v2 repo=1 pr=1 batch=[a-f0-9]{64} head=b{40}",
        )
        commits = [value for value in fake.commits.values() if "message" in value]
        self.assertTrue(
            any(
                "reviewsensei-learning:v2 repo=1 pr=1" in value["message"]
                for value in commits
            )
        )

    def test_historical_candidate_discovery_retries_with_smaller_pages(self):
        attempted_page_sizes = []

        class ResponseBudgetGitHub(FakeGitHub):
            def request(self, method, path, *, token, body=None):
                parsed = urlsplit(path)
                if method == "GET" and parsed.path == "/repos/owner/repo/pulls":
                    query = dict(
                        pair.split("=")
                        for pair in parsed.query.split("&")
                        if "=" in pair
                    )
                    page_size = int(query.get("per_page", "100"))
                    attempted_page_sizes.append(page_size)
                    if page_size > 5:
                        raise GitHubHTTPResponseTooLargeError(
                            "GitHub response exceeded the configured size limit"
                        )
                return super().request(method, path, token=token, body=body)

        fake = ResponseBudgetGitHub()

        result = self.publish(fake, (proposal("First"),))

        self.assertEqual(result.status, "created")
        self.assertEqual(attempted_page_sizes, [20, 10, 5, 20, 10, 5])

    def test_smaller_page_retry_resumes_at_the_same_item_offset(self):
        class OffsetGitHub:
            def __init__(self):
                self.attempts = []

            @staticmethod
            def repository_path(repository, suffix=""):
                return f"/repos/{repository}{suffix}"

            def request(self, method, path, *, token, body=None):
                parsed = urlsplit(path)
                query = dict(
                    pair.split("=") for pair in parsed.query.split("&") if "=" in pair
                )
                page_size = int(query["per_page"])
                page = int(query["page"])
                self.attempts.append((page_size, page))
                if page_size == 20 and page == 2:
                    raise GitHubHTTPResponseTooLargeError(
                        "GitHub response exceeded the configured size limit"
                    )
                start = (page - 1) * page_size
                return 200, list(range(start, min(start + page_size, 25)))

        fake = OffsetGitHub()
        publisher = LearningPRPublisher(http=fake)

        items = publisher._list_pull_requests(
            token="token", repository="owner/repo", state="all"
        )

        self.assertEqual(items, list(range(25)))
        self.assertEqual(fake.attempts, [(20, 1), (20, 2), (10, 3)])

    def test_non_size_http_error_is_not_retried(self):
        class InvalidGitHub:
            def __init__(self):
                self.calls = 0

            @staticmethod
            def repository_path(repository, suffix=""):
                return f"/repos/{repository}{suffix}"

            def request(self, method, path, *, token, body=None):
                self.calls += 1
                raise GitHubHTTPError("GitHub response was not valid UTF-8 JSON")

        fake = InvalidGitHub()

        with self.assertRaises(GitHubHTTPError):
            LearningPRPublisher(http=fake)._list_pull_requests(
                token="token", repository="owner/repo", state="all"
            )

        self.assertEqual(fake.calls, 1)

    def test_oversized_single_item_page_still_fails_closed(self):
        class OversizedGitHub:
            def __init__(self):
                self.page_sizes = []

            @staticmethod
            def repository_path(repository, suffix=""):
                return f"/repos/{repository}{suffix}"

            def request(self, method, path, *, token, body=None):
                parsed = urlsplit(path)
                query = dict(
                    pair.split("=") for pair in parsed.query.split("&") if "=" in pair
                )
                self.page_sizes.append(int(query["per_page"]))
                raise GitHubHTTPResponseTooLargeError(
                    "GitHub response exceeded the configured size limit"
                )

        fake = OversizedGitHub()

        with self.assertRaises(GitHubHTTPResponseTooLargeError):
            LearningPRPublisher(http=fake)._list_pull_requests(
                token="token", repository="owner/repo", state="all"
            )

        self.assertEqual(fake.page_sizes, [20, 10, 5, 1])

    def test_batch_digest_is_order_independent_and_paths_are_stable(self):
        first, second = proposal("First"), proposal("Second")
        self.assertEqual(
            batch_digest((first, second)), batch_digest((second, first, second))
        )
        self.assertEqual(learning_path(first), learning_path(first))
        self.assertEqual(len(proposal_digest(first)), 64)

    def test_identical_rerun_reuses_open_draft_without_duplicate(self):
        fake = FakeGitHub()
        self.publish(fake, (proposal(),))
        before = len(fake.prs)
        result = self.publish(fake, (proposal(),))
        self.assertEqual(result.status, "skipped_pull_request_exists")
        self.assertEqual(len(fake.prs), before)
        self.assertEqual(
            sum(
                method == "POST" and path.endswith("/pulls")
                for method, path, _ in fake.calls
            ),
            1,
        )

    def test_open_draft_with_base_identical_files_is_a_noop(self):
        fake = FakeGitHub()
        first = proposal("First")
        self.publish(fake, (first,))
        fake.base_contents[learning_path(first)] = _content(first)
        result = self.publish(fake, (first,))
        self.assertEqual(result.status, "skipped_identical")
        self.assertEqual(result.pull_request_number, 2)

    def test_legacy_candidate_uses_immutable_commit_parent_after_base_advance(self):
        fake = FakeGitHub()
        first = proposal("First")
        self.publish(fake, (first,))
        stable = "review-sensei/learnings/pr-1"
        legacy = f"review-sensei/learnings/pr-1-{proposal_digest(first)[:16]}"
        fake.refs[legacy] = fake.refs.pop(stable)
        detail = fake.prs[2]
        detail["head"]["ref"] = legacy
        detail["title"] = "Propose ReviewSensei learnings"
        detail["body"] = "\n".join(
            [
                "Proposed ReviewSensei learning files:",
                f"- `{learning_path(first)}`",
                "",
                f"<!-- reviewsensei:learning:v1 pr=1 batch={proposal_digest(first)} -->",
            ]
        )
        old_learning_head = fake.refs[legacy]
        fake.base_sha = "c" * 40
        fake.commits[fake.base_sha] = {"tree": {"sha": "f" * 40}, "parents": []}
        detail["base"]["sha"] = fake.base_sha
        result = self.publish(fake, (first, proposal("Second")))
        self.assertEqual(result.status, "updated")
        refreshed = fake.commits[fake.refs[legacy]]
        self.assertEqual(refreshed["parents"][0]["sha"], old_learning_head)
        self.assertEqual(refreshed["parents"][1]["sha"], fake.base_sha)

    def test_generation_orphan_is_recovered_before_stable_reuse(self):
        class PullCreationUnavailable(FakeGitHub):
            fail_creation = False

            def request(self, method, path, *, token, body=None):
                if (
                    self.fail_creation
                    and method == "POST"
                    and path == "/repos/owner/repo/pulls"
                ):
                    return 503, {}
                return super().request(method, path, token=token, body=body)

        fake = PullCreationUnavailable()
        self.publish(fake, (proposal("First"),))
        fake.prs[2]["state"] = "closed"
        fake.prs[2]["merged"] = True
        fake.prs[2]["merged_at"] = "2026-09-02T00:00:00Z"
        fake.fail_creation = True
        with self.assertRaises(GitHubLearningProposalTransientError):
            self.publish(fake, (proposal("Second"),))
        generation = next(branch for branch in fake.refs if "-g-" in branch)
        suffixed_generation = f"{generation}-2"
        fake.refs[suffixed_generation] = fake.refs.pop(generation)
        fake.refs.pop("review-sensei/learnings/pr-1")
        fake.fail_creation = False
        result = self.publish(fake, (proposal("Second"),))
        self.assertEqual(result.status, "created")
        self.assertEqual(fake.prs[3]["head"]["ref"], suffixed_generation)

    def test_changed_batch_refreshes_same_open_pr(self):
        fake = FakeGitHub()
        self.publish(fake, (proposal("First"),))
        result = self.publish(fake, (proposal("First"), proposal("Second")))
        self.assertEqual(result.status, "updated")
        self.assertEqual(result.pull_request_number, 2)
        self.assertEqual(len(fake.prs), 1)
        self.assertIn("Proposal count: 2", fake.prs[2]["body"])

    def test_source_title_change_refreshes_same_open_pr(self):
        fake = FakeGitHub()
        self.publish(fake, (proposal("First"),))
        fake.source_title = "Clarify the review boundaries"
        fake.source_title_events = [
            {
                "event": "renamed",
                "rename": {
                    "from": "Improve review boundaries",
                    "to": "Clarify the review boundaries",
                },
                "created_at": "2026-09-02T00:00:01Z",
            }
        ]
        result = self.publish(fake, (proposal("First"), proposal("Second")))
        self.assertEqual(result.status, "updated")
        self.assertEqual(result.pull_request_number, 2)
        self.assertEqual(len(fake.prs), 1)
        self.assertIn(
            "ReviewSensei learnings from #1: Clarify the review boundaries",
            fake.prs[2]["title"],
        )

    def test_source_title_reread_before_creation_retries_with_current_metadata(self):
        class RenamesBeforeFinalAuthority(FakeGitHub):
            source_reads = 0

            def request(self, method, path, *, token, body=None):
                if (
                    method == "GET"
                    and urlsplit(path).path == "/repos/owner/repo/pulls/1"
                ):
                    self.source_reads += 1
                    if self.source_reads == 2:
                        self.source_title = "Clarify the review boundaries"
                return super().request(method, path, token=token, body=body)

        fake = RenamesBeforeFinalAuthority()
        result = self.publish(fake, (proposal("First"),))
        self.assertEqual(result.status, "created")
        self.assertEqual(result.pull_request_number, 2)
        self.assertGreaterEqual(fake.source_reads, 3)
        self.assertIn(
            "ReviewSensei learnings from #1: Clarify the review boundaries",
            fake.prs[2]["title"],
        )

    def test_long_source_title_round_trips_through_bounded_title(self):
        fake = FakeGitHub()
        fake.source_title = "Long source title " + ("x" * 500)
        self.publish(fake, (proposal("First"),))
        result = self.publish(fake, (proposal("First"), proposal("Second")))
        self.assertEqual(result.status, "updated")
        self.assertLessEqual(len(fake.prs[2]["title"].encode("utf-8")), 256)
        self.assertIn(f"Source title: {fake.source_title}", fake.prs[2]["body"])

    def test_unproven_source_title_transition_fails_closed(self):
        fake = FakeGitHub()
        self.publish(fake, (proposal("First"),))
        detail = fake.prs[2]
        detail["title"] = "ReviewSensei learnings from #1: Forged title"
        detail["body"] = detail["body"].replace(
            "Source title: Improve review boundaries", "Source title: Forged title"
        )
        commit = fake.commits[fake.refs["review-sensei/learnings/pr-1"]]
        commit["message"] = (
            commit["message"]
            .replace(
                f"title={_sha256_text('ReviewSensei learnings from #1: Improve review boundaries')}",
                f"title={_sha256_text(detail['title'])}",
            )
            .replace(
                f"body={_sha256_text(fake.prs[2]['body'].replace('Source title: Forged title', 'Source title: Improve review boundaries'))}",
                f"body={_sha256_text(detail['body'])}",
            )
        )
        before = len(fake.calls)
        with self.assertRaises(GitHubLearningProposalError):
            self.publish(fake, (proposal("First"), proposal("Second")))
        self.assertEqual(
            detail["title"], "ReviewSensei learnings from #1: Forged title"
        )
        self.assertTrue(
            any(
                "/issues/1/timeline" in path
                for _method, path, _body in fake.calls[before:]
            )
        )
        self.assertFalse(
            any(method != "GET" for method, _path, _body in fake.calls[before:])
        )

    def test_historical_source_title_transition_before_candidate_is_unproven(self):
        fake = FakeGitHub()
        fake.source_title = "A"
        self.publish(fake, (proposal("First"),))
        detail = fake.prs[2]
        detail["title"] = "ReviewSensei learnings from #1: Historical"
        detail["body"] = detail["body"].replace(
            "Source title: A", "Source title: Historical"
        )
        commit = fake.commits[fake.refs["review-sensei/learnings/pr-1"]]
        commit["message"] = (
            commit["message"]
            .replace(
                f"title={_sha256_text('ReviewSensei learnings from #1: A')}",
                f"title={_sha256_text(detail['title'])}",
            )
            .replace(
                f"body={_sha256_text(fake.prs[2]['body'].replace('Source title: Historical', 'Source title: A'))}",
                f"body={_sha256_text(detail['body'])}",
            )
        )
        fake.source_title = "Current"
        fake.source_title_events = [
            {
                "event": "renamed",
                "rename": {"from": "Historical", "to": "A"},
                "created_at": "2026-09-01T23:59:00Z",
            },
            {
                "event": "renamed",
                "rename": {"from": "A", "to": "Current"},
                "created_at": "2026-09-02T00:00:01Z",
            },
        ]
        before = len(fake.calls)
        with self.assertRaises(GitHubLearningProposalError):
            self.publish(fake, (proposal("First"), proposal("Second")))
        self.assertFalse(
            any(method != "GET" for method, _path, _body in fake.calls[before:])
        )

    def test_malformed_source_title_history_fails_closed(self):
        fake = FakeGitHub()
        self.publish(fake, (proposal("First"),))
        fake.source_title = "Clarify the review boundaries"
        fake.source_title_events = [{"event": "renamed"}]
        before = len(fake.calls)
        with self.assertRaises(GitHubLearningProposalError):
            self.publish(fake, (proposal("First"), proposal("Second")))
        self.assertFalse(
            any(method != "GET" for method, _path, _body in fake.calls[before:])
        )

    def test_noncontiguous_source_title_history_fails_closed(self):
        fake = FakeGitHub()
        self.publish(fake, (proposal("First"),))
        fake.source_title = "Current title"
        fake.source_title_events = [
            {
                "event": "renamed",
                "rename": {"from": "Improve review boundaries", "to": "Middle"},
                "created_at": "2026-09-02T00:00:02Z",
            },
            {
                "event": "renamed",
                "rename": {"from": "Middle", "to": "Current title"},
                "created_at": "2026-09-02T00:00:01Z",
            },
        ]
        before = len(fake.calls)
        with self.assertRaises(GitHubLearningProposalError):
            self.publish(fake, (proposal("First"), proposal("Second")))
        self.assertFalse(
            any(method != "GET" for method, _path, _body in fake.calls[before:])
        )

    def test_equal_source_title_event_timestamps_fail_closed(self):
        fake = FakeGitHub()
        self.publish(fake, (proposal("First"),))
        fake.source_title = "Current title"
        fake.source_title_events = [
            {
                "event": "renamed",
                "rename": {"from": "Improve review boundaries", "to": "Middle"},
                "created_at": "2026-09-02T00:00:01Z",
            },
            {
                "event": "renamed",
                "rename": {"from": "Middle", "to": "Current title"},
                "created_at": "2026-09-02T00:00:01Z",
            },
        ]
        before = len(fake.calls)
        with self.assertRaises(GitHubLearningProposalError):
            self.publish(fake, (proposal("First"), proposal("Second")))
        self.assertFalse(
            any(method != "GET" for method, _path, _body in fake.calls[before:])
        )

    def test_learning_pr_close_race_returns_without_git_mutation(self):
        class ClosesDuringRevalidation(FakeGitHub):
            detail_reads = 0

            def request(self, method, path, *, token, body=None):
                if (
                    method == "GET"
                    and urlsplit(path).path == "/repos/owner/repo/pulls/2"
                ):
                    self.detail_reads += 1
                    if self.detail_reads == 2:
                        self.prs[2]["state"] = "closed"
                return super().request(method, path, token=token, body=body)

        fake = ClosesDuringRevalidation()
        self.publish(fake, (proposal("First"),))
        before = len(fake.calls)
        result = self.publish(fake, (proposal("First"), proposal("Second")))
        self.assertEqual(result.status, "skipped_closed_pull_request")
        self.assertFalse(
            any(method != "GET" for method, path, _body in fake.calls[before:])
        )
        self.assertEqual(fake.detail_reads, 2)

    def test_learning_pr_close_during_reconciliation_reads_returns_without_git_mutation(
        self,
    ):
        class ClosesDuringBaseRead(FakeGitHub):
            close_on_contents = False

            def request(self, method, path, *, token, body=None):
                if (
                    self.close_on_contents
                    and method == "GET"
                    and urlsplit(path).path.startswith("/repos/owner/repo/contents/")
                ):
                    self.prs[2]["state"] = "closed"
                    self.close_on_contents = False
                return super().request(method, path, token=token, body=body)

        fake = ClosesDuringBaseRead()
        self.publish(fake, (proposal("First"),))
        fake.close_on_contents = True
        before = len(fake.calls)
        commit_count = len(fake.commits)
        result = self.publish(fake, (proposal("First"), proposal("Second")))
        self.assertEqual(result.status, "skipped_closed_pull_request")
        self.assertEqual(len(fake.commits), commit_count)
        self.assertFalse(
            any(method != "GET" for method, _path, _body in fake.calls[before:])
        )

    def test_learning_pr_close_before_ref_update_does_not_advance_branch(self):
        class ClosesAfterCommit(FakeGitHub):
            close_on_commit = False

            def request(self, method, path, *, token, body=None):
                result = super().request(method, path, token=token, body=body)
                if (
                    self.close_on_commit
                    and method == "POST"
                    and urlsplit(path).path == "/repos/owner/repo/git/commits"
                ):
                    self.prs[2]["state"] = "closed"
                    self.close_on_commit = False
                return result

        fake = ClosesAfterCommit()
        self.publish(fake, (proposal("First"),))
        old_ref = fake.refs["review-sensei/learnings/pr-1"]
        fake.close_on_commit = True
        before = len(fake.calls)
        result = self.publish(fake, (proposal("First"), proposal("Second")))
        self.assertEqual(result.status, "skipped_closed_pull_request")
        self.assertEqual(fake.refs["review-sensei/learnings/pr-1"], old_ref)
        self.assertFalse(
            any(
                method == "PATCH" and "/git/refs/" in path
                for method, path, _body in fake.calls[before:]
            )
        )

    def test_source_head_mismatch_writes_nothing(self):
        fake = FakeGitHub()
        result = self.publish(fake, (proposal(),), head_sha="c" * 40)
        self.assertEqual(result.status, "skipped_stale_source")
        self.assertFalse(any(method != "GET" for method, _path, _body in fake.calls))

    def test_invalid_marker_or_manual_metadata_fails_closed(self):
        fake = FakeGitHub()
        self.publish(fake, (proposal(),))
        fake.prs[2]["body"] += "\nmanual note"
        with self.assertRaises(GitHubLearningProposalError):
            self.publish(fake, (proposal("Second"),))

    def test_matching_metadata_hashes_do_not_authorize_manual_overwrite(self):
        fake = FakeGitHub()
        self.publish(fake, (proposal(),))
        detail = fake.prs[2]
        old_title = detail["title"]
        old_body = detail["body"]
        marker = old_body.splitlines()[-1]
        detail["title"] = f"{old_title} (maintainer edit)"
        detail["body"] = old_body.replace(f"\n{marker}", f"\nmaintainer note\n{marker}")
        commit = fake.commits[fake.refs["review-sensei/learnings/pr-1"]]
        commit["message"] = commit["message"].replace(
            f"title={_sha256_text(old_title)} body={_sha256_text(old_body)}",
            f"title={_sha256_text(detail['title'])} body={_sha256_text(detail['body'])}",
        )
        with self.assertRaises(GitHubLearningProposalError):
            self.publish(fake, (proposal("Second"),))
        self.assertEqual(detail["title"], f"{old_title} (maintainer edit)")
        self.assertIn("maintainer note", detail["body"])
        self.assertFalse(
            any(
                method == "PATCH" and "/git/refs/heads/" in path
                for method, path, _body in fake.calls
            )
        )

    def test_title_only_manual_edit_with_matching_hash_fails_closed(self):
        fake = FakeGitHub()
        self.publish(fake, (proposal(),))
        detail = fake.prs[2]
        old_title = detail["title"]
        edited_title = "ReviewSensei learnings from #1: Unrelated title"
        detail["title"] = edited_title
        commit = fake.commits[fake.refs["review-sensei/learnings/pr-1"]]
        commit["message"] = commit["message"].replace(
            f"title={_sha256_text(old_title)}",
            f"title={_sha256_text(edited_title)}",
        )
        with self.assertRaises(GitHubLearningProposalError):
            self.publish(fake, (proposal("Second"),))
        self.assertEqual(detail["title"], edited_title)

    def test_incomplete_pull_request_summary_fails_closed(self):
        class IncompleteList(FakeGitHub):
            incomplete = False

            def request(self, method, path, *, token, body=None):
                if (
                    self.incomplete
                    and method == "GET"
                    and urlsplit(path).path == "/repos/owner/repo/pulls"
                ):
                    return 200, [{"number": 2}]
                return super().request(method, path, token=token, body=body)

        fake = IncompleteList()
        self.publish(fake, (proposal(),))
        fake.incomplete = True
        with self.assertRaises(GitHubLearningProposalError):
            self.publish(fake, (proposal("Second"),))
        self.assertEqual(len(fake.prs), 1)

    def test_closed_merged_generation_uses_a_fresh_branch(self):
        fake = FakeGitHub()
        self.publish(fake, (proposal("First"),))
        fake.prs[2]["state"] = "closed"
        fake.prs[2]["merged"] = True
        fake.prs[2]["merged_at"] = "2026-09-02T00:00:00Z"
        result = self.publish(fake, (proposal("Second"),))
        self.assertEqual(result.status, "created")
        self.assertEqual(len(fake.prs), 2)
        self.assertTrue(
            fake.prs[3]["head"]["ref"].startswith("review-sensei/learnings/pr-1-g-")
        )

    def test_closed_unmerged_generation_blocks_a_duplicate(self):
        fake = FakeGitHub()
        self.publish(fake, (proposal("First"),))
        fake.prs[2]["state"] = "closed"
        result = self.publish(fake, (proposal("Second"),))
        self.assertEqual(result.status, "skipped_closed_pull_request")
        self.assertEqual(len(fake.prs), 1)

    def test_deleted_merged_branch_is_proven_from_immutable_commit(self):
        fake = FakeGitHub()
        self.publish(fake, (proposal("First"),))
        fake.prs[2]["state"] = "closed"
        fake.prs[2]["merged"] = True
        fake.prs[2]["merged_at"] = "2026-09-02T00:00:00Z"
        fake.refs.pop("review-sensei/learnings/pr-1")
        result = self.publish(fake, (proposal("Second"),))
        self.assertEqual(result.status, "created")
        self.assertEqual(fake.prs[3]["head"]["ref"], "review-sensei/learnings/pr-1")

    def test_display_text_is_bounded_and_markdown_escaped(self):
        fake = FakeGitHub()
        special = proposal(
            "Title `with` [markup]",
            "Rule\\with\nnewline",
            rationale="Why\tthis matters",
            category="category",
            scope=("src/[x]",),
        )
        self.publish(fake, (special,))
        body = fake.prs[2]["body"]
        self.assertIn("Title \\`with\\` \\[markup\\]", body)
        self.assertNotIn("\nnewline", body)

    def test_existing_base_file_is_retired_from_a_new_generation(self):
        fake = FakeGitHub()
        first = proposal("First")
        path = learning_path(first)
        fake.base_contents[path] = _content(first)
        pending = LearningPRPublisher(http=fake)._pending_against_base(
            token="token",
            repository="owner/repo",
            base_sha=fake.base_sha,
            proposals=(first,),
        )
        self.assertEqual(pending, ())

    def test_forked_source_is_skipped(self):
        fake = FakeGitHub()
        source = fake._source()
        source["head"]["repo"]["fork"] = True
        fake._source = lambda: source
        result = self.publish(fake, (proposal(),))
        self.assertEqual(result.status, "skipped_fork")

    def test_input_and_parser_boundaries_fail_closed(self):
        fake = FakeGitHub()
        publisher = LearningPRPublisher(http=fake)
        common = {
            "token": "token",
            "repository": "owner/repo",
            "pull_request": 1,
            "head_sha": fake.source_sha,
            "base_branch": "main",
            "base_sha": fake.base_sha,
            "proposals": (proposal(),),
        }
        for field, value in (
            ("repository_id", 0),
            ("pull_request", 0),
            ("head_sha", "x"),
            ("base_branch", "../main"),
        ):
            args = {"repository_id": 1, **common}
            args[field] = value
            with (
                self.subTest(field=field),
                self.assertRaises(GitHubLearningProposalError),
            ):
                publisher.propose_batch(**args)
        with self.assertRaises(GitHubLearningProposalError):
            publisher.propose_batch(
                repository_id=1,
                **{key: value for key, value in common.items() if key != "proposals"},
                proposals=(),
            )
        self.assertIsNone(_parse_marker_line("not a marker"))
        self.assertIsNone(
            _parse_marker_line(
                "<!-- reviewsensei:learning:v2 repo="
                + "1" * 21
                + " pr=1 batch="
                + "a" * 64
                + " head="
                + "b" * 40
                + " -->"
            )
        )
        self.assertIsNone(_parse_marker_line("\r\n" + "x"))
        self.assertIsNone(
            _parse_provenance("chore: propose ReviewSensei learnings\r\n\r\n")
        )
        self.assertEqual(_sanitize_display_text(None, limit=4), "")
        with self.assertRaises(GitHubLearningProposalError):
            _decode_base64({"encoding": "base64", "content": "%%%"})
        with self.assertRaises(GitHubLearningProposalTransientError):
            _raise_response(500, "temporary")

    def test_base_conflict_and_transient_repository_statuses(self):
        fake = FakeGitHub()
        first = proposal("First")
        fake.base_contents[learning_path(first)] = _content(proposal("Different"))
        with self.assertRaises(GitHubLearningProposalError):
            LearningPRPublisher(http=fake)._pending_against_base(
                token="token",
                repository="owner/repo",
                base_sha=fake.base_sha,
                proposals=(first,),
            )

        class Unavailable(FakeGitHub):
            def request(self, method, path, *, token, body=None):
                if method == "GET" and path == "/repos/owner/repo":
                    return 503, {}
                return super().request(method, path, token=token, body=body)

        with self.assertRaises(GitHubLearningProposalTransientError):
            self.publish(Unavailable(), (proposal(),))

    def test_generation_collision_is_not_overwritten(self):
        fake = FakeGitHub()
        self.publish(fake, (proposal("First"),))
        fake.prs[2]["state"] = "closed"
        fake.prs[2]["merged"] = True
        fake.prs[2]["merged_at"] = "2026-09-02T00:00:00Z"
        generation = (
            f"review-sensei/learnings/pr-1-g-{batch_digest((proposal('Second'),))[:16]}"
        )
        fake.refs[generation] = fake.base_sha
        with self.assertRaises(GitHubLearningProposalError):
            self.publish(fake, (proposal("Second"),))

    def test_multiple_open_learning_prs_are_ambiguous(self):
        fake = FakeGitHub()
        self.publish(fake, (proposal("First"),))
        duplicate = copy.deepcopy(fake.prs[2])
        duplicate["number"] = 3
        fake.prs[3] = duplicate
        with self.assertRaises(GitHubLearningProposalError):
            self.publish(fake, (proposal("Second"),))

    def test_pr_creation_retry_recovers_a_proven_orphan(self):
        class PullCreationUnavailable(FakeGitHub):
            fail_creation = True

            def request(self, method, path, *, token, body=None):
                if (
                    self.fail_creation
                    and method == "POST"
                    and path == "/repos/owner/repo/pulls"
                ):
                    return 503, {}
                return super().request(method, path, token=token, body=body)

        fake = PullCreationUnavailable()
        with self.assertRaises(GitHubLearningProposalTransientError):
            self.publish(fake, (proposal("First"),))
        self.assertEqual(fake.prs, {})
        fake.fail_creation = False
        result = self.publish(fake, (proposal("First"),))
        self.assertEqual(result.status, "created")
        self.assertEqual(len(fake.prs), 1)

    def test_ref_update_before_metadata_patch_is_recovered(self):
        class PatchUnavailable(FakeGitHub):
            fail_patch = False

            def request(self, method, path, *, token, body=None):
                if (
                    self.fail_patch
                    and method == "PATCH"
                    and path == "/repos/owner/repo/pulls/2"
                ):
                    return 503, {}
                return super().request(method, path, token=token, body=body)

        fake = PatchUnavailable()
        self.publish(fake, (proposal("First"),))
        fake.fail_patch = True
        with self.assertRaises(GitHubLearningProposalTransientError):
            self.publish(fake, (proposal("First"), proposal("Second")))
        fake.fail_patch = False
        result = self.publish(fake, (proposal("First"), proposal("Second")))
        self.assertEqual(result.status, "updated")
        self.assertEqual(len(fake.prs), 1)
        self.assertIn("Proposal count: 2", fake.prs[2]["body"])

    def test_source_preflight_states_and_repository_lookup_fail_closed(self):
        class RepositoryMissing(FakeGitHub):
            def request(self, method, path, *, token, body=None):
                if method == "GET" and path == "/repos/owner/repo":
                    return 404, {}
                return super().request(method, path, token=token, body=body)

        with self.assertRaises(GitHubLearningProposalError):
            self.publish(RepositoryMissing(), (proposal(),))

        for state, draft, expected in (
            ("closed", False, "skipped_pr_state"),
            ("open", True, "skipped_pr_state"),
        ):
            fake = FakeGitHub()
            source = fake._source()
            source["state"] = state
            source["draft"] = draft
            fake._source = lambda source=source: source
            result = self.publish(fake, (proposal(),))
            self.assertEqual(result.status, expected)

        fake = FakeGitHub()
        source = fake._source()
        source["head"] = "invalid"
        fake._source = lambda source=source: source
        with self.assertRaises(GitHubLearningProposalError):
            self.publish(fake, (proposal(),))

        fake = FakeGitHub()
        source = fake._source()
        source.pop("draft")
        fake._source = lambda source=source: source
        self.assertEqual(self.publish(fake, (proposal(),)).status, "skipped_pr_state")

    def test_valid_legacy_v1_draft_is_upgraded_in_place(self):
        fake = FakeGitHub()
        first = proposal("First")
        self.publish(fake, (first,))
        stable = "review-sensei/learnings/pr-1"
        legacy = f"review-sensei/learnings/pr-1-{proposal_digest(first)[:16]}"
        fake.refs[legacy] = fake.refs.pop(stable)
        detail = fake.prs[2]
        detail["head"]["ref"] = legacy
        detail["title"] = "Propose ReviewSensei learnings"
        detail["body"] = "\n".join(
            [
                "Proposed ReviewSensei learning files:",
                f"- `{learning_path(first)}`",
                "",
                f"<!-- reviewsensei:learning:v1 pr=1 batch={proposal_digest(first)} -->",
            ]
        )
        result = self.publish(fake, (first, proposal("Second")))
        self.assertEqual(result.status, "updated")
        self.assertIn("learning:v2", fake.prs[2]["body"])

    def test_low_level_contract_helpers_are_strict_and_bounded(self):
        with self.assertRaises(GitHubLearningProposalError):
            _normalized_proposals("bad")
        with self.assertRaises(GitHubLearningProposalError):
            _normalized_proposals([object()])
        self.assertTrue(_is_sha("a" * 40))
        self.assertFalse(_is_sha("g" * 40))
        self.assertTrue(_is_sha256("a" * 64))
        self.assertFalse(_is_sha256("a" * 63))
        marker = _marker_line(
            repository_id=1,
            pull_request=1,
            batch="a" * 64,
            head_sha="b" * 40,
        )
        self.assertEqual(_parse_marker_line(marker).version, 2)
        self.assertEqual(
            _parse_marker_line(
                "<!-- reviewsensei:learning:v1 pr=1 batch=" + "a" * 64 + " -->"
            ).version,
            1,
        )
        self.assertIsNone(_parse_marker_line("\n\t"))
        self.assertIsNone(_parse_provenance("not provenance"))
        self.assertIsNotNone(_parse_github_timestamp("2026-09-02T00:00:00Z"))
        self.assertIsNotNone(_parse_github_timestamp("2026-09-02T00:00:00-04:00"))
        self.assertIsNone(_parse_github_timestamp("2026-02-30T00:00:00Z"))
        self.assertIsNone(_parse_github_timestamp(None))
        self.assertFalse(_has_marker(None))
        self.assertTrue(_has_marker("note reviewsensei:learning:v2"))
        self.assertTrue(_branch_for_pr("review-sensei/learnings/pr-1", 1))
        self.assertFalse(_branch_for_pr("feature", 1))
        self.assertEqual(len(_sanitize_display_text("x" * 20, limit=4)), 4)
        self.assertTrue(_truncate_bytes("é" * 200, 10).endswith("…"))
        for invalid in ({}, {"encoding": "utf-8", "content": ""}):
            with (
                self.subTest(invalid=invalid),
                self.assertRaises(GitHubLearningProposalError),
            ):
                _decode_base64(invalid)
        with self.assertRaises(GitHubLearningProposalError):
            _raise_response(400, "rejected")
        publisher = LearningPRPublisher(http=FakeGitHub())
        with self.assertRaises(GitHubLearningProposalError):
            publisher._tree_files(
                [{"path": "bad", "mode": "100644", "type": "blob", "sha": "bad"}]
            )

    def test_additional_boundaries_and_ownership_checks_are_fail_closed(self):
        with patch(
            "review_sensei.hosting.github.learning_pr.proposal_digest",
            return_value="a" * 64,
        ):
            with self.assertRaises(GitHubLearningProposalError):
                _normalized_proposals((proposal("one"), proposal("two")))
        self.assertEqual(_truncate_bytes("x", 0), "")
        self.assertEqual(_truncate_bytes("é", 1), "")
        self.assertEqual(_sanitize_display_text("x", limit=0), "")
        self.assertIsNone(_parse_marker_line(1))
        self.assertFalse(_branch_for_pr(None, 1))

        fake = FakeGitHub()
        publisher = LearningPRPublisher(http=fake)
        with self.assertRaises(GitHubLearningProposalError):
            publisher.propose_batch(
                token="token",
                repository="owner/repo",
                repository_id=10**20,
                pull_request=1,
                head_sha=fake.source_sha,
                base_branch="main",
                base_sha=fake.base_sha,
                proposals=(proposal(),),
            )
        with self.assertRaises(GitHubLearningProposalError):
            publisher.propose_batch(
                token="token",
                repository="owner/repo",
                repository_id=1,
                pull_request=10**20,
                head_sha=fake.source_sha,
                base_branch="main",
                base_sha=fake.base_sha,
                proposals=(proposal(),),
            )

        self.publish(fake, (proposal(),))
        detail = fake.prs[2]
        with self.assertRaises(GitHubLearningProposalError):
            publisher._find_open_by_marker(
                token="token",
                repository="owner/repo",
                repository_id=999,
                branch=detail["head"]["ref"],
                base_branch="main",
                title=detail["title"],
                body=detail["body"],
            )
        with self.assertRaises(GitHubLearningProposalError):
            publisher._patch_learning_pull_request(
                token="token",
                repository="owner/repo",
                repository_id=999,
                pull_request_number=2,
                branch=detail["head"]["ref"],
                old_head=detail["head"]["sha"],
                new_head=detail["head"]["sha"],
                base_branch="main",
                old_title=detail["title"],
                old_body=detail["body"],
                title=detail["title"],
                body=detail["body"],
            )

        class MismatchedCommit(FakeGitHub):
            def request(self, method, path, *, token, body=None):
                if (
                    method == "GET"
                    and path == f"/repos/owner/repo/git/commits/{self.base_sha}"
                ):
                    return 200, {"sha": "c" * 40, "tree": {"sha": "f" * 40}}
                return super().request(method, path, token=token, body=body)

        with self.assertRaises(GitHubLearningProposalError):
            publisher = LearningPRPublisher(http=MismatchedCommit())
            publisher._commit_tree_sha(
                token="token", repository="owner/repo", commit_sha="a" * 40
            )

    def test_tree_and_commit_contract_errors_fail_closed(self):
        publisher = LearningPRPublisher(http=FakeGitHub())
        with self.assertRaises(GitHubLearningProposalError):
            publisher._tree_files([{}])
        with self.assertRaises(GitHubLearningProposalError):
            publisher._tree_files(
                [
                    {
                        "path": ".github/review-sensei/learnings/nested/bad.json",
                        "mode": "040000",
                        "type": "tree",
                    }
                ]
            )
        with self.assertRaises(GitHubLearningProposalError):
            publisher._create_learning_commit(
                token="token",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                base_sha="a" * 40,
                head_sha="b" * 40,
                batch="c" * 64,
                title="title",
                body="body",
                proposals=(proposal(),),
                parents=(),
            )

        fake = FakeGitHub()
        fake.refs["review-sensei/learnings/pr-1"] = "not-a-sha"
        with self.assertRaises(GitHubLearningProposalError):
            LearningPRPublisher(http=fake)._read_ref(
                token="token",
                repository="owner/repo",
                branch="review-sensei/learnings/pr-1",
            )

        class InvalidBaseTree(FakeGitHub):
            def request(self, method, path, *, token, body=None):
                if (
                    method == "GET"
                    and path == f"/repos/owner/repo/git/commits/{self.base_sha}"
                ):
                    return 200, {"tree": {"sha": "invalid"}}
                return super().request(method, path, token=token, body=body)

        invalid_tree = InvalidBaseTree()
        with self.assertRaises(GitHubLearningProposalError):
            LearningPRPublisher(http=invalid_tree)._create_learning_commit(
                token="token",
                repository="owner/repo",
                repository_id=1,
                pull_request=1,
                base_sha=invalid_tree.base_sha,
                head_sha=invalid_tree.source_sha,
                batch="c" * 64,
                title="title",
                body="body",
                proposals=(proposal(),),
                parents=(invalid_tree.base_sha,),
            )


if __name__ == "__main__":
    unittest.main()
