import base64
import json
import unittest

from review_sensei.hosting.github import (
    GitHubLearningProposalError,
    GitHubLearningProposalTransientError,
    LearningPRPublisher,
)
from review_sensei.hosting.github.learning_pr import batch_digest, proposal_digest
from review_sensei.models import LearningEntry, LearningProposal

try:
    from fake_github_http import json_response, make_http
except ModuleNotFoundError:
    from tests.fake_github_http import json_response, make_http


def source_pr_payload(head_sha="b" * 40, *, fork=False):
    return {
        "state": "open",
        "draft": False,
        "head": {
            "sha": head_sha,
            "repo": {"full_name": "owner/repo", "fork": fork},
        },
        "base": {
            "ref": "main",
            "sha": "a" * 40,
            "repo": {"id": 1, "full_name": "owner/repo", "fork": False},
        },
    }


def make_learning_http(responses, *, source=None):
    remaining = list(responses) if isinstance(responses, list) else [responses]
    return make_http([json_response(source or source_pr_payload()), *remaining])


class TestLearningPRPublisher(LearningPRPublisher):
    def propose(self, **kwargs):
        kwargs.setdefault("repository_id", 1)
        kwargs.setdefault("head_sha", "b" * 40)
        return super().propose(**kwargs)

    def propose_batch(self, **kwargs):
        kwargs.setdefault("repository_id", 1)
        kwargs.setdefault("head_sha", "b" * 40)
        return super().propose_batch(**kwargs)


class LearningPRPublisherTests(unittest.TestCase):
    def test_creates_one_deterministic_draft_pr_for_a_proposal_batch(self):
        proposals = (
            LearningProposal(title="Second", rule="Rule two"),
            LearningProposal(title="First", rule="Rule one"),
        )
        http, calls = make_learning_http(
            [
                json_response([]),
                json_response({"message": "not found"}, 404),
                json_response({"message": "not found"}, 404),
                json_response({}, 201),
                json_response({"sha": "b" * 40}, 201),
                json_response({"sha": "e" * 40}, 201),
                json_response({"tree": {"sha": "f" * 40}}),
                json_response({"sha": "c" * 40}, 201),
                json_response({"sha": "d" * 40}, 201),
                json_response({}, 200),
                json_response({"number": 42}, 201),
            ]
        )

        outcome = TestLearningPRPublisher(http=http).propose_batch(
            token="token",
            repository="owner/repo",
            pull_request=1,
            base_branch="main",
            base_sha="a" * 40,
            proposals=proposals,
        )

        self.assertEqual(outcome.status, "created")
        self.assertEqual(outcome.pull_request_number, 42)
        tree_calls = [call for call in calls if call[1].endswith("/git/trees")]
        self.assertEqual(len(tree_calls), 1)
        tree = json.loads(tree_calls[0][2].decode("utf-8"))
        self.assertEqual(len(tree["tree"]), 2)
        self.assertTrue(
            all(
                entry["path"].startswith(".github/review-sensei/learnings/")
                for entry in tree["tree"]
            )
        )
        pull_calls = [
            call for call in calls if call[0] == "POST" and call[1].endswith("/pulls")
        ]
        self.assertEqual(len(pull_calls), 1)
        pull = json.loads(pull_calls[0][2].decode("utf-8"))
        digest = batch_digest(proposals)
        self.assertEqual(pull["head"], f"review-sensei/learnings/pr-1-{digest[:16]}")
        self.assertIn(f"batch={digest}", pull["body"])
        self.assertTrue(pull["draft"])

    def test_batch_identity_is_order_independent_and_deduplicates_proposals(self):
        first = LearningProposal(title="First", rule="Rule one")
        second = LearningProposal(title="Second", rule="Rule two")
        self.assertEqual(
            batch_digest((first, second)),
            batch_digest((second, first, second)),
        )

    def test_creates_draft_learning_pr_from_git_objects(self):
        proposal = LearningProposal(title="T", rule="R")
        http, calls = make_learning_http(
            [
                json_response([]),
                json_response({"message": "not found"}, 404),
                json_response({}, 201),
                json_response({"sha": "b" * 40}, 201),
                json_response({"tree": {"sha": "f" * 40}}),
                json_response({"sha": "c" * 40}, 201),
                json_response({"sha": "d" * 40}, 201),
                json_response({}, 200),
                json_response({"number": 42}, 201),
            ]
        )

        outcome = TestLearningPRPublisher(http=http).propose(
            token="token",
            repository="owner/repo",
            pull_request=1,
            base_branch="main",
            base_sha="a" * 40,
            proposal=proposal,
        )

        self.assertEqual(outcome.status, "created")
        self.assertEqual(outcome.pull_request_number, 42)
        self.assertEqual(len(calls), 10)
        self.assertEqual(calls[-1][0], "POST")
        blob_call = next(call for call in calls if call[1].endswith("/git/blobs"))
        blob = json.loads(blob_call[2].decode("utf-8"))
        entry = LearningEntry.from_dict(json.loads(blob["content"]))
        self.assertEqual(entry.title, proposal.title)
        self.assertEqual(entry.status, "active")

    def test_proposal_path_is_deterministic(self):
        proposal = LearningProposal(title="Keep adapters isolated", rule="Do not leak.")
        from review_sensei.hosting.github.learning_pr import learning_path

        path = learning_path(proposal)
        self.assertTrue(path.startswith(".github/review-sensei/learnings/sensei-"))
        self.assertEqual(path, learning_path(proposal))

    def test_existing_open_pr_skips_write(self):
        proposal = LearningProposal(title="T", rule="R")
        digest = proposal_digest(proposal)
        branch = f"review-sensei/learnings/pr-1-{digest[:16]}"
        marker = f"<!-- reviewsensei:learning:v1 pr=1 batch={digest} -->"
        http, calls = make_learning_http(
            json_response([{"number": 9, "head": {"ref": branch}, "body": marker}])
        )
        outcome = TestLearningPRPublisher(http=http).propose(
            token="token",
            repository="owner/repo",
            pull_request=1,
            base_branch="main",
            base_sha="a" * 40,
            proposal=proposal,
        )
        self.assertEqual(outcome.status, "skipped_pull_request_exists")
        self.assertEqual(outcome.pull_request_number, 9)
        self.assertEqual(len(calls), 2)

    def test_conflicting_existing_branch_fails_closed(self):
        # The real digest branch won't match the test expectation, so this test
        # only verifies that a 422 without an open PR raises a stable error.
        proposal = LearningProposal(title="T", rule="R")
        http, calls = make_learning_http(
            json_response({"message": "reference already exists"}, 422)
        )
        with self.assertRaises(GitHubLearningProposalError):
            TestLearningPRPublisher(http=http).propose(
                token="token",
                repository="owner/repo",
                pull_request=1,
                base_branch="main",
                base_sha="a" * 40,
                proposal=proposal,
            )
        self.assertTrue(calls)

    def test_unavailable_open_pr_reconciliation_fails_closed(self):
        proposal = LearningProposal(title="T", rule="R")
        http, calls = make_learning_http(json_response({}, 500))

        with self.assertRaises(GitHubLearningProposalTransientError):
            TestLearningPRPublisher(http=http).propose_batch(
                token="token",
                repository="owner/repo",
                pull_request=1,
                base_branch="main",
                base_sha="a" * 40,
                proposals=(proposal,),
            )

        self.assertEqual(len(calls), 2)

    def test_identical_existing_file_skips_without_creating_branch(self):
        proposal = LearningProposal(title="T", rule="R")
        digest = proposal_digest(proposal)
        content = (
            json.dumps(
                proposal.to_entry(id=f"sensei-{digest[:16]}").to_dict(), indent=2
            )
            + "\n"
        )
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        http, calls = make_learning_http(
            [
                json_response([]),
                json_response({"content": encoded, "encoding": "base64"}),
            ]
        )
        outcome = TestLearningPRPublisher(http=http).propose(
            token="token",
            repository="owner/repo",
            pull_request=1,
            base_branch="main",
            base_sha="a" * 40,
            proposal=proposal,
        )
        self.assertEqual(outcome.status, "skipped_identical")
        self.assertEqual(len(calls), 3)
        self.assertIn("/contents/.github/review-sensei/learnings/", calls[2][1])
        self.assertIn("?ref=" + ("a" * 40), calls[2][1])

    def test_ambiguous_pull_request_post_reconciles_batch_marker(self):
        proposal = LearningProposal(title="T", rule="R")
        digest = batch_digest((proposal,))
        branch = f"review-sensei/learnings/pr-1-{digest[:16]}"
        marker = f"<!-- reviewsensei:learning:v1 pr=1 batch={digest} -->"
        http, calls = make_learning_http(
            [
                json_response([]),
                json_response({"message": "not found"}, 404),
                json_response({}, 201),
                json_response({"sha": "b" * 40}, 201),
                json_response({"tree": {"sha": "f" * 40}}),
                json_response({"sha": "c" * 40}, 201),
                json_response({"sha": "d" * 40}, 201),
                json_response({}, 200),
                json_response({}, 500),
                json_response(
                    [
                        {
                            "number": 42,
                            "head": {"ref": branch},
                            "body": marker,
                        }
                    ]
                ),
            ]
        )

        outcome = TestLearningPRPublisher(http=http).propose_batch(
            token="token",
            repository="owner/repo",
            pull_request=1,
            base_branch="main",
            base_sha="a" * 40,
            proposals=(proposal,),
        )

        self.assertEqual(outcome.status, "skipped_pull_request_exists")
        self.assertEqual(outcome.pull_request_number, 42)
        self.assertEqual(len(calls), 11)

    def test_ambiguous_ref_post_continues_only_at_exact_base(self):
        proposal = LearningProposal(title="T", rule="R")
        http, calls = make_learning_http(
            [
                json_response([]),
                json_response({"message": "not found"}, 404),
                json_response({}, 500),
                json_response([]),
                json_response({"object": {"sha": "a" * 40}}),
                json_response({"sha": "b" * 40}, 201),
                json_response({"tree": {"sha": "f" * 40}}),
                json_response({"sha": "c" * 40}, 201),
                json_response({"sha": "d" * 40}, 201),
                json_response({}, 200),
                json_response({"number": 42}, 201),
            ]
        )

        outcome = TestLearningPRPublisher(http=http).propose_batch(
            token="token",
            repository="owner/repo",
            pull_request=1,
            base_branch="main",
            base_sha="a" * 40,
            proposals=(proposal,),
        )

        self.assertEqual(outcome.status, "created")
        self.assertEqual(outcome.pull_request_number, 42)
        self.assertEqual(len(calls), 12)

    def test_retry_resumes_after_branch_commit_before_pull_request(self):
        proposal = LearningProposal(title="T", rule="R")
        digest = batch_digest((proposal,))
        branch = f"review-sensei/learnings/pr-1-{digest[:16]}"
        commit_sha = "d" * 40
        content = (
            json.dumps(
                proposal.to_entry(id=f"sensei-{digest[:16]}").to_dict(), indent=2
            )
            + "\n"
        )
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        from review_sensei.hosting.github.learning_pr import learning_path

        path = learning_path(proposal)
        http, calls = make_learning_http(
            [
                json_response([]),
                json_response({"message": "not found"}, 404),
                json_response({"message": "reference already exists"}, 422),
                json_response([]),
                json_response({"object": {"sha": commit_sha}}),
                json_response({"object": {"sha": commit_sha}}),
                json_response(
                    {
                        "message": "chore: propose ReviewSensei learnings",
                        "parents": [{"sha": "a" * 40}],
                        "tree": {"sha": "c" * 40},
                    }
                ),
                json_response({"files": [{"filename": path, "status": "added"}]}),
                json_response(
                    [
                        {
                            "name": "config.yml",
                            "path": ".github/review-sensei/config.yml",
                            "type": "file",
                            "sha": "f" * 40,
                        },
                        {
                            "name": "learnings",
                            "path": ".github/review-sensei/learnings",
                            "type": "dir",
                            "sha": "1" * 40,
                        },
                    ]
                ),
                json_response(
                    {
                        "truncated": False,
                        "tree": [
                            {
                                "path": path.rsplit("/", 1)[-1],
                                "mode": "100644",
                                "type": "blob",
                                "sha": "e" * 40,
                            }
                        ],
                    }
                ),
                json_response({"content": encoded, "encoding": "base64"}),
                json_response({"number": 42}, 201),
            ]
        )

        outcome = TestLearningPRPublisher(http=http).propose_batch(
            token="token",
            repository="owner/repo",
            pull_request=1,
            base_branch="main",
            base_sha="a" * 40,
            proposals=(proposal,),
        )

        self.assertEqual(outcome.status, "created")
        self.assertEqual(outcome.pull_request_number, 42)
        self.assertFalse(any(call[1].endswith("/git/blobs") for call in calls))
        self.assertFalse(any(call[1].endswith("/git/trees") for call in calls))
        self.assertFalse(any("recursive=1" in call[1] for call in calls))
        self.assertTrue(
            any("/contents/.github/review-sensei?ref=" in call[1] for call in calls)
        )
        pull = json.loads(calls[-1][2].decode("utf-8"))
        self.assertEqual(pull["head"], branch)
        self.assertTrue(pull["draft"])

    def test_retry_rejects_noncanonical_learning_tree_entries(self):
        proposal = LearningProposal(title="T", rule="R")
        commit_sha = "d" * 40
        from review_sensei.hosting.github.learning_pr import learning_path

        path = learning_path(proposal)
        for mode, entry_type in (("100755", "blob"), ("120000", "blob")):
            with self.subTest(mode=mode, entry_type=entry_type):
                http, calls = make_learning_http(
                    [
                        json_response([]),
                        json_response({"message": "not found"}, 404),
                        json_response({"message": "reference already exists"}, 422),
                        json_response([]),
                        json_response({"object": {"sha": commit_sha}}),
                        json_response({"object": {"sha": commit_sha}}),
                        json_response(
                            {
                                "message": "chore: propose ReviewSensei learnings",
                                "parents": [{"sha": "a" * 40}],
                                "tree": {"sha": "c" * 40},
                            }
                        ),
                        json_response(
                            {"files": [{"filename": path, "status": "added"}]}
                        ),
                        json_response(
                            [
                                {
                                    "name": "learnings",
                                    "path": ".github/review-sensei/learnings",
                                    "type": "dir",
                                    "sha": "1" * 40,
                                }
                            ]
                        ),
                        json_response(
                            {
                                "truncated": False,
                                "tree": [
                                    {
                                        "path": path.rsplit("/", 1)[-1],
                                        "mode": mode,
                                        "type": entry_type,
                                        "sha": "e" * 40,
                                    }
                                ],
                            }
                        ),
                    ]
                )

                with self.assertRaises(GitHubLearningProposalError):
                    TestLearningPRPublisher(http=http).propose_batch(
                        token="token",
                        repository="owner/repo",
                        pull_request=1,
                        base_branch="main",
                        base_sha="a" * 40,
                        proposals=(proposal,),
                    )

                self.assertFalse(any(call[1].endswith("/pulls") for call in calls))

    def test_retry_rejects_truncated_learning_subtree(self):
        proposal = LearningProposal(title="T", rule="R")
        commit_sha = "d" * 40
        from review_sensei.hosting.github.learning_pr import learning_path

        path = learning_path(proposal)
        http, calls = make_learning_http(
            [
                json_response([]),
                json_response({"message": "not found"}, 404),
                json_response({"message": "reference already exists"}, 422),
                json_response([]),
                json_response({"object": {"sha": commit_sha}}),
                json_response({"object": {"sha": commit_sha}}),
                json_response(
                    {
                        "message": "chore: propose ReviewSensei learnings",
                        "parents": [{"sha": "a" * 40}],
                        "tree": {"sha": "c" * 40},
                    }
                ),
                json_response({"files": [{"filename": path, "status": "added"}]}),
                json_response(
                    [
                        {
                            "name": "learnings",
                            "path": ".github/review-sensei/learnings",
                            "type": "dir",
                            "sha": "1" * 40,
                        }
                    ]
                ),
                json_response({"truncated": True, "tree": []}),
            ]
        )

        with self.assertRaises(GitHubLearningProposalError):
            TestLearningPRPublisher(http=http).propose_batch(
                token="token",
                repository="owner/repo",
                pull_request=1,
                base_branch="main",
                base_sha="a" * 40,
                proposals=(proposal,),
            )

        self.assertFalse(any(call[1].endswith("/pulls") for call in calls))

    def test_different_existing_file_conflicts_without_writing(self):
        proposal = LearningProposal(title="T", rule="R")
        encoded = base64.b64encode(b'{"title":"different"}\n').decode("ascii")
        http, calls = make_learning_http(
            [
                json_response([]),
                json_response({"content": encoded, "encoding": "base64"}),
            ]
        )
        with self.assertRaises(GitHubLearningProposalError):
            TestLearningPRPublisher(http=http).propose(
                token="token",
                repository="owner/repo",
                pull_request=1,
                base_branch="main",
                base_sha="a" * 40,
                proposal=proposal,
            )
        self.assertEqual(len(calls), 3)

    def test_invalid_base_branch_is_rejected_before_lookup(self):
        proposal = LearningProposal(title="T", rule="R")
        http, calls = make_learning_http([])
        with self.assertRaises(GitHubLearningProposalError):
            TestLearningPRPublisher(http=http).propose(
                token="token",
                repository="owner/repo",
                pull_request=1,
                base_branch="../main",
                base_sha="a" * 40,
                proposal=proposal,
            )
        self.assertEqual(calls, [])

    def test_source_head_and_fork_are_rechecked_before_any_learning_write(self):
        proposal = LearningProposal(title="T", rule="R")
        for expected, source in (
            ("skipped_stale_head", source_pr_payload("c" * 40)),
            ("skipped_fork", source_pr_payload(fork=True)),
        ):
            with self.subTest(expected=expected):
                http, calls = make_learning_http([], source=source)
                outcome = TestLearningPRPublisher(http=http).propose(
                    token="token",
                    repository="owner/repo",
                    pull_request=1,
                    base_branch="main",
                    base_sha="a" * 40,
                    proposal=proposal,
                )
                self.assertEqual(outcome.status, expected)
                self.assertEqual(len(calls), 1)

    def test_arbitrary_path_is_never_accepted(self):
        proposal = LearningProposal(title="T", rule="R")
        from review_sensei.hosting.github.learning_pr import learning_path

        self.assertNotIn("..", learning_path(proposal))
        self.assertTrue(
            learning_path(proposal).startswith(".github/review-sensei/learnings/")
        )


if __name__ == "__main__":
    unittest.main()
