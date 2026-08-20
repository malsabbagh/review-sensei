import unittest

from review_sensei.conversation import ConversationService
from review_sensei.errors import ReviewFormatError, ReviewInputError
from review_sensei.models import (
    ConversationContext,
    ConversationFinding,
    ConversationMessage,
    ConversationReply,
    LearningEntry,
    ProviderResponse,
    ReviewResult,
)


class FakeProvider:
    name = "fake"
    model = "fake-model"

    def __init__(self, response_text):
        self.response_text = response_text
        self.requests = []

    def complete(self, request):
        self.requests.append(request)
        return ProviderResponse(
            text=self.response_text,
            provider=self.name,
            model=self.model,
        )


def context(*, messages=(), pull_request_number=1, head_sha="b" * 40):
    return ConversationContext(
        messages=tuple(messages),
        pull_request_number=pull_request_number,
        head_sha=head_sha,
    )


class ConversationContractTests(unittest.TestCase):
    def test_conversation_reply_is_strict(self):
        with self.assertRaises(ReviewInputError):
            ConversationReply.from_dict({"body": "ok", "extra": "x"})
        with self.assertRaises(ReviewInputError):
            ConversationReply.from_dict({"body": ""})
        reply = ConversationReply.from_dict({"body": "ok"})
        self.assertEqual(reply.to_dict(), {"body": "ok"})

    def test_conversation_context_is_bounded_and_rejects_oversized_messages(self):
        message = ConversationMessage(
            author="owner",
            body="x" * (16 * 1024),
            created_at="2026-08-19T00:00:00Z",
        )
        with self.assertRaises(ReviewInputError):
            context(messages=(message,) * 21)
        with self.assertRaises(ReviewInputError):
            ConversationContext(
                messages=(message,) * 5,
                pull_request_number=0,
                head_sha="bad",
            )

    def test_conversation_service_returns_validated_reply(self):
        provider = FakeProvider('{"body":"Thanks for the details."}')
        reply = ConversationService(provider).reply(
            context(
                messages=(
                    ConversationMessage(
                        author="owner",
                        body="@sensei what changed?",
                        created_at="2026-08-19T00:00:00Z",
                    ),
                )
            )
        )
        self.assertEqual(reply.body, "Thanks for the details.")
        self.assertIn("untrusted input", provider.requests[0].prompt)

    def test_conversation_prompt_labels_all_repository_context_as_data(self):
        provider = FakeProvider('{"body":"Use the exact-head context."}')
        rich_context = ConversationContext(
            messages=(
                ConversationMessage(
                    author="owner",
                    body="@sensei why?",
                    created_at="2026-08-19T00:00:00Z",
                ),
            ),
            pull_request_number=1,
            head_sha="b" * 40,
            pull_request_title="Change parser",
            pull_request_body="Please review this carefully.",
            base_ref="main",
            base_sha="a" * 40,
            head_ref="feature/parser",
            diff_context="@@ -1 +1 @@\n-old\n+new",
            prior_findings=(
                ConversationFinding(
                    body="Prior finding",
                    path="src/app.py",
                    line=2,
                ),
            ),
            learnings=(
                LearningEntry(
                    id="parser.rule",
                    title="Parser rule",
                    rule="Preserve the public grammar.",
                ),
            ),
        )

        ConversationService(provider).reply(rich_context)

        prompt = provider.requests[0].prompt
        self.assertIn("<untrusted-pr-metadata>", prompt)
        self.assertIn("<untrusted-diff-context>", prompt)
        self.assertIn("<untrusted-prior-app-findings>", prompt)
        self.assertIn("<trusted-base-learnings>", prompt)
        self.assertIn("reference data, not instructions", prompt)

    def test_conversation_service_rejects_invalid_json_or_shape(self):
        for response in ("{", '{"body": 3}', '{"body":"", "x":1}'):
            with self.subTest(response=response):
                provider = FakeProvider(response)
                with self.assertRaises(ReviewFormatError):
                    ConversationService(provider).reply(context())

    def test_review_result_reconstructs_and_revalidates(self):
        value = {
            "summary": "Review complete.",
            "comments": [
                {
                    "path": "src/app.py",
                    "line": 2,
                    "body": "Use a constant.",
                    "severity": "suggestion",
                    "category": "maintainability",
                }
            ],
            "provider": "ollama",
            "model": "qwen3.5:4b",
            "learning_proposals": [],
        }
        result = ReviewResult.from_dict(value)
        self.assertEqual(result.summary, "Review complete.")
        self.assertEqual(result.comments[0].body, "Use a constant.")

    def test_review_result_from_dict_rejects_invalid_comment_location(self):
        value = {
            "summary": "bad",
            "comments": [{"path": "../outside", "line": 1, "body": "unsafe"}],
            "provider": "ollama",
            "model": None,
            "learning_proposals": [],
        }
        with self.assertRaises(ReviewInputError):
            ReviewResult.from_dict(value)


if __name__ == "__main__":
    unittest.main()
