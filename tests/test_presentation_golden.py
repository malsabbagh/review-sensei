"""Golden published review bodies for the slice-D presentation layer.

The fixture at ``tests/fixtures/presentation/review-bodies.json`` holds one
scenario per presentation state the epic requires plus the exact published
summary and inline comments each one produces. These are golden bodies: a
rendering change must be deliberate, and regeneration is
``REVIEWSENSEI_UPDATE_PRESENTATION_GOLDEN=1`` with this module selected.
"""

from __future__ import annotations

import json
import os
import re
import unittest
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from review_sensei.convergence import ReviewConvergencePolicy, derive_blocker_candidate
from review_sensei.hosting.github import ReviewPublisher
from review_sensei.models import ReviewResult

try:
    from fake_github_http import json_response, make_http, placement_responses
except ModuleNotFoundError:
    from tests.fake_github_http import json_response, make_http, placement_responses

HEAD = "b" * 40
BASE_SHA = "a" * 40
UPDATE_ENV = "REVIEWSENSEI_UPDATE_PRESENTATION_GOLDEN"
MARKER = "<!-- reviewsensei:review:v1"
FIXTURE_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "presentation" / "review-bodies.json"
)

# Local copies of the small fixtures this suite needs, deliberately not
# imported from another test module: an edit to a shared fixture must not
# silently change these golden bodies.
DIFF = """diff --git a/src/app.py b/src/app.py
--- a/src/app.py
+++ b/src/app.py
@@ -1 +1,2 @@
 keep
+change
"""


def pr_payload(*, head_sha):
    return {
        "state": "open",
        "draft": False,
        "user": {"login": "alice", "type": "User"},
        "head": {
            "sha": head_sha,
            "repo": {"full_name": "owner/repo", "fork": False},
        },
        "base": {
            "ref": "main",
            "sha": BASE_SHA,
            "repo": {"id": 1, "full_name": "owner/repo", "fork": False},
        },
        "merged": False,
    }


def graphql_review_threads_response(*, nodes=()):
    return json_response(
        {
            "data": {
                "repository": {
                    "pullRequest": {
                        "reviewThreads": {
                            "nodes": list(nodes),
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            }
        }
    )


@dataclass(frozen=True)
class PublishedScenario:
    events: tuple[str, ...]
    inline: tuple[Mapping[str, object], ...]
    body: str
    calls: list[object]

    @property
    def summary(self) -> str:
        return split_published_body(self.body)[0]


def _policy(scenario: Mapping[str, object]) -> ReviewConvergencePolicy:
    return ReviewConvergencePolicy(mode=str(scenario.get("policy", "merge-focused")))


def _blocker_candidates(scenario, comments):
    candidates = []
    for spec in scenario.get("blocker_candidates", ()):
        comment = comments[int(spec["comment"])]
        candidates.append(
            derive_blocker_candidate(
                comment,
                on_changed_path=bool(spec.get("on_changed_path", True)),
                evidence_locations_validated=bool(
                    spec.get("evidence_locations_validated", True)
                ),
                has_failure_condition=bool(spec.get("has_failure_condition", True)),
                has_actionable_remedy=bool(spec.get("has_actionable_remedy", True)),
                has_specific_violation=bool(spec.get("has_specific_violation", True)),
            )
        )
    return tuple(candidates)


def _result(scenario: Mapping[str, object]) -> ReviewResult:
    result = ReviewResult.from_dict(scenario["review"])
    needs_human = tuple(scenario.get("runtime_needs_human", ()))
    if not needs_human:
        return result
    comments = tuple(
        replace(comment, needs_human=True) if index in needs_human else comment
        for index, comment in enumerate(result.comments)
    )
    return replace(result, comments=comments)


def _host_facts_needed(result: ReviewResult, policy) -> bool:
    """Mirror the publication predicate: only legacy mode can need the read."""

    if not policy.inline_advisory_threads:
        return False
    return any(
        not comment.blocks_approval
        and not comment.needs_human
        and isinstance(comment.line, int)
        and comment.side in ("LEFT", "RIGHT")
        for comment in result.comments
    )


def publish_scenario(scenario, *, thread_nodes=()) -> PublishedScenario:
    """Publish one fixture scenario and capture the publication requests."""

    policy = _policy(scenario)
    result = _result(scenario)
    candidates = _blocker_candidates(scenario, result.comments)
    facts_required = str(scenario.get("conversation_resolution", "")) == "required"
    responses: list[object] = [
        json_response(pr_payload(head_sha=HEAD)),
        json_response([]),
        json_response(pr_payload(head_sha=HEAD)),
    ]
    if _host_facts_needed(result, policy):
        responses.extend(placement_responses(required=facts_required))
    if scenario["expected_inline"]:
        # Duplicate suppression reads the existing threads only when this run
        # is about to post at least one inline finding.
        responses.append(graphql_review_threads_response(nodes=thread_nodes))
    responses.append(json_response({"id": 5}, 200))
    # The shared finalizer re-reads the pull request after the findings review.
    # A run whose finding review already submitted REQUEST_CHANGES stops here
    # and simply leaves the remaining scripted responses unused.
    responses.extend(
        [
            json_response(pr_payload(head_sha=HEAD)),
            graphql_review_threads_response(),
            json_response(pr_payload(head_sha=HEAD)),
            json_response([]),
            json_response({"id": 6}, 200),
        ]
    )
    http, calls = make_http(responses)
    outcome = ReviewPublisher(http=http).publish(
        token="token",
        repository="owner/repo",
        repository_id=1,
        pull_request=2,
        head_sha=HEAD,
        base_branch="main",
        base_sha=BASE_SHA,
        result=result,
        diff=DIFF,
        app_slug="reviewsensei[bot]",
        auto_approve=bool(scenario.get("auto_approve", False)),
        convergence_policy=policy,
        allow_retired_legacy_policy=True,
        blocker_candidates=candidates or None,
    )
    if outcome.status != "published":
        raise AssertionError(f"scenario {scenario['name']} did not publish")
    reviews = _posted_reviews(calls)
    findings = next(review for review in reviews if "comments" in review)
    return PublishedScenario(
        events=tuple(str(review["event"]) for review in reviews),
        inline=tuple(findings["comments"]),
        body=str(findings["body"]),
        calls=calls,
    )


def _posted_reviews(calls) -> list[Mapping[str, object]]:
    reviews: list[Mapping[str, object]] = []
    for call in calls:
        if len(call) < 3 or call[2] in (None, b""):
            continue
        try:
            payload = json.loads(call[2].decode("utf-8"))
        except (AttributeError, TypeError, ValueError, UnicodeDecodeError):
            continue
        if isinstance(payload, dict) and "event" in payload and "body" in payload:
            reviews.append(payload)
    return reviews


def split_published_body(published: str) -> tuple[str, str]:
    """Split the published body into its summary and its reconciliation marker."""

    marker_index = published.find(f"\n\n{MARKER}")
    if marker_index < 0:
        raise AssertionError("published review body has no review marker")
    return published[:marker_index], published[marker_index + 2 :]


def _scenarios() -> list[Mapping[str, object]]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["scenarios"]


def _published_visible_lines(outcome: PublishedScenario) -> list[str]:
    """Return every line a reader sees, across the summary and inline comments.

    Code-fence contents and reconciliation markers are excluded: GitHub scrolls
    code blocks horizontally and the HTML markers never render, so neither can
    overflow a narrow layout or form a link.
    """

    lines: list[str] = list(outcome.summary.splitlines())
    for comment in outcome.inline:
        lines.extend(str(comment["body"]).splitlines())
    visible: list[str] = []
    in_fence = False
    for line in lines:
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or line.strip().startswith("<!--"):
            continue
        visible.append(line)
    return visible


# An unescaped markdown link: a backslash-escaped bracket is literal text, so
# the negative lookbehind is what distinguishes a rendered link from sanitized
# inert content.
_MARKDOWN_LINK = re.compile(r"(?<!\\)\[([^\]\n]*)\]\(([^)\n]*)\)")


class ReviewBodyGoldenTests(unittest.TestCase):
    maxDiff = None

    def test_fixture_covers_every_required_presentation_state(self):
        names = {str(scenario["name"]) for scenario in _scenarios()}
        for required in (
            "clean-approval",
            "required-inline",
            "optional-body",
            "advisory-defect",
            "uncertainty",
            "cross-file-unanchored",
            "partial-coverage",
            "failed-review",
            "resource-handoff",
            "many-optional",
            "formatting-edges",
            "conversation-resolution-required",
            "retry-dedupe",
        ):
            self.assertIn(required, names)

    def test_published_review_bodies_match_the_golden_fixture(self):
        scenarios = _scenarios()
        rendered: list[tuple[Mapping[str, object], PublishedScenario]] = []
        for scenario in scenarios:
            with self.subTest(scenario=scenario["name"]):
                outcome = publish_scenario(scenario)
                rendered.append((scenario, outcome))
                self.assertEqual(
                    outcome.events, tuple(scenario.get("expected_events", ("COMMENT",)))
                )
                self.assertEqual(len(outcome.inline), scenario["expected_inline"])
                summary, marker = split_published_body(outcome.body)
                self.assertTrue(marker.startswith(f"{MARKER} "))
                self.assertIn("head=" + HEAD, marker)
                for forbidden in scenario.get("forbidden", ()):
                    self.assertNotIn(forbidden, outcome.body)
                    for comment in outcome.inline:
                        self.assertNotIn(forbidden, comment["body"])
        if os.environ.get(UPDATE_ENV) == "1":
            # A regeneration must never silently drop a scenario that failed to
            # render; the fixture is only rewritten from a complete pass.
            self.assertEqual(len(rendered), len(scenarios))
            self._write_fixture(scenarios, rendered)
            self.skipTest(f"golden fixture rewritten at {FIXTURE_PATH}")
        for scenario, outcome in rendered:
            with self.subTest(scenario=scenario["name"]):
                self.assertIn("summary", scenario, "fixture has no golden summary")
                self.assertIn(
                    "inline", scenario, "fixture has no golden inline comments"
                )
                self.assertEqual(
                    outcome.summary,
                    scenario["summary"],
                    f"scenario {scenario['name']} no longer matches its golden body",
                )
                self.assertEqual(
                    [dict(comment) for comment in outcome.inline],
                    scenario["inline"],
                    f"scenario {scenario['name']} no longer matches its golden inline "
                    "comments",
                )

    def _write_fixture(self, scenarios, rendered):
        updated = []
        for scenario, outcome in rendered:
            entry = dict(scenario)
            entry["summary"] = outcome.summary
            entry["inline"] = [dict(comment) for comment in outcome.inline]
            updated.append(entry)
        FIXTURE_PATH.write_text(
            json.dumps({"scenarios": updated}, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def test_a_retry_does_not_duplicate_the_inline_thread_or_the_summary(self):
        scenario = next(
            item for item in _scenarios() if item.get("name") == "retry-dedupe"
        )
        first = publish_scenario(scenario)
        self.assertEqual(len(first.inline), 1)
        posted = first.inline[0]
        node = {
            "isResolved": False,
            "comments": {
                "nodes": [
                    {
                        "body": posted["body"],
                        "path": posted["path"],
                        "line": posted["line"],
                        "author": {"login": "reviewsensei[bot]"},
                    }
                ]
            },
        }
        retry = publish_scenario(scenario, thread_nodes=(node,))
        self.assertEqual(retry.events, first.events)
        self.assertEqual(retry.inline, ())
        self.assertEqual(retry.body, first.body)


class GoldenBodyInvariantTests(unittest.TestCase):
    """Properties every golden body must hold regardless of its scenario."""

    def test_no_scenario_publishes_a_forbidden_artifact(self):
        forbidden = (
            "## Advisory observations",
            "## Review observations",
            "## Findings without a publishable inline location",
            "Review classification:",
            "Proposed:",
            "Severity:",
            "Fix effort:",
            "Confidence:",
            "## ReviewSensei — Approved",
        )
        for scenario in _scenarios():
            with self.subTest(scenario=scenario["name"]):
                outcome = publish_scenario(scenario)
                for token in forbidden:
                    self.assertNotIn(token, outcome.body)

    # A narrow layout only breaks when a single visible token has no whitespace
    # to wrap at; prose length itself wraps. Forty columns leaves the longest
    # real link target in the fixture (34) room to render.
    NARROW_VISIBLE_TOKEN_LIMIT = 40

    def test_no_visible_token_breaks_narrow_layouts(self):
        """Long bodies stay readable: every visible token fits a narrow column.

        This is the automated half of the Formatting row's rendered
        narrow-layout inspection; the rendered half is the golden fixture
        inspected at a phone-width column.
        """

        for scenario in _scenarios():
            with self.subTest(scenario=scenario["name"]):
                outcome = publish_scenario(scenario)
                for line in _published_visible_lines(outcome):
                    for token in line.split():
                        visible = token.strip("`*[]()_.,:;\"'")
                        self.assertLessEqual(
                            len(visible), self.NARROW_VISIBLE_TOKEN_LIMIT, line
                        )

    def test_every_rendered_link_keeps_a_safe_scheme(self):
        """A hostile target stays inert text; only http(s) targets render."""

        for scenario in _scenarios():
            with self.subTest(scenario=scenario["name"]):
                outcome = publish_scenario(scenario)
                for line in _published_visible_lines(outcome):
                    for label, target in _MARKDOWN_LINK.findall(line):
                        self.assertTrue(
                            target.startswith(("http://", "https://")),
                            f"{label!r} renders as a link to {target!r}",
                        )

    def test_every_code_fence_is_balanced(self):
        for scenario in _scenarios():
            with self.subTest(scenario=scenario["name"]):
                outcome = publish_scenario(scenario)
                fences = [
                    line
                    for line in outcome.body.splitlines()
                    if line.strip().startswith("```")
                ]
                self.assertEqual(len(fences) % 2, 0, fences)

    def test_identifiers_do_not_drift_between_reruns(self):
        first = {
            str(scenario["name"]): publish_scenario(scenario).body
            for scenario in _scenarios()
        }
        second = {
            str(scenario["name"]): publish_scenario(scenario).body
            for scenario in _scenarios()
        }
        self.assertEqual(first, second)

    def test_required_findings_never_disappear_from_the_summary(self):
        for scenario in _scenarios():
            identifiers = scenario.get("expected_required_ids", ())
            if not identifiers:
                continue
            with self.subTest(scenario=scenario["name"]):
                outcome = publish_scenario(scenario)
                for identifier in identifiers:
                    self.assertIn(f"**{identifier}:**", outcome.summary)


if __name__ == "__main__":
    unittest.main()
