"""One stable ReviewSensei check-run gate bound to the App and reviewed head.

YAML cannot make a check required, so this module publishes the product's
authoritative check and reports its identity for administrator setup. The gate
conclusion never encodes incomplete enforcement as ``neutral`` or ``skipped``:
GitHub accepts those conclusions for required checks, so they would silently
disable the gate. ``advisory`` is the one mode that publishes ``neutral``,
because that mode promises no ReviewSensei-imposed merge gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from ...configuration import REVIEW_POLICIES
from ...errors import ReviewInputError
from ...models import ReviewResult
from ...validation import validate_bounded_text
from .approval import has_blocking_findings
from .errors import (
    GitHubHTTPError,
    GitHubHTTPTransientError,
    GitHubPublicationError,
    GitHubPublicationTransientError,
)
from .http import GitHubHttp

REVIEW_CHECK_NAME = "ReviewSensei"
REVIEW_CHECK_EXTERNAL_ID = "reviewsensei-review-v1"
# The gate modes are the canonical ``github.reviews`` vocabulary, so a policy
# the gate cannot map is impossible rather than merely unlikely.
REVIEW_POLICY_MODES = frozenset(REVIEW_POLICIES)
COMPLETED_CONCLUSIONS = frozenset(
    {"success", "failure", "neutral", "cancelled", "timed_out", "action_required"}
)
MAX_CHECK_TITLE_CHARS = 255
MAX_CHECK_SUMMARY_CHARS = 4096
MAX_CHECK_PAGE_SIZE = 100


@dataclass(frozen=True)
class CheckOutcome:
    """One bounded check conclusion with its reader-facing output."""

    conclusion: str
    title: str
    summary: str


@dataclass(frozen=True)
class CheckPublicationResult:
    status: str
    check_run_id: int | None = None
    diagnostic: str | None = None


def required_check_identity(*, app_slug: str) -> dict[str, str]:
    """Return the expected required-check identity for administrator setup."""

    if not isinstance(app_slug, str) or not app_slug.strip():
        raise ReviewInputError("check producer slug is required")
    return {"name": REVIEW_CHECK_NAME, "producer": app_slug}


def review_check_outcome(
    *,
    policy: str,
    review_status: str,
    required_fixes: bool,
) -> CheckOutcome:
    """Map one review outcome onto the documented gate conclusions."""

    if policy not in REVIEW_POLICY_MODES:
        raise ReviewInputError("review policy mode is invalid")
    if review_status not in {"complete", "partial", "incomplete", "summary-only"}:
        raise ReviewInputError("review status is invalid")
    bounded_required = required_fixes is True
    if policy == "advisory":
        detail = (
            "Required fixes were reported but enforcement is disabled by "
            "github.reviews: advisory."
            if bounded_required
            else "ReviewSensei does not impose a merge gate in advisory mode."
        )
        return CheckOutcome(
            conclusion="neutral",
            title="Advisory review — not enforced",
            summary=detail,
        )
    if review_status != "complete":
        return CheckOutcome(
            conclusion="action_required",
            title="Review incomplete",
            summary=(
                "This review did not establish complete evidence for the "
                "reviewed head, so the check stays non-passing until an "
                "eligible review completes."
            ),
        )
    if bounded_required:
        return CheckOutcome(
            conclusion="failure",
            title="Required fixes remain",
            summary=(
                "The completed review reported required fixes for this head. "
                "They are listed in the ReviewSensei review and its inline "
                "threads."
            ),
        )
    return CheckOutcome(
        conclusion="success",
        title="No required fixes",
        summary="The completed review reported no required fixes for this head.",
    )


def review_check_outcome_for_publication_failure(
    *, policy: str, transient: bool
) -> CheckOutcome:
    """Map a failed publication attempt onto a non-passing check conclusion."""

    if policy not in REVIEW_POLICY_MODES:
        raise ReviewInputError("review policy mode is invalid")
    if policy == "advisory":
        return CheckOutcome(
            conclusion="neutral",
            title="Advisory review — publication failed",
            summary=(
                "ReviewSensei could not publish its review for this head. "
                "Advisory mode does not impose a merge gate."
            ),
        )
    return CheckOutcome(
        conclusion="action_required",
        title="Review publication failed",
        summary=(
            "ReviewSensei could not publish its review for this head"
            + (" (temporary failure; rerun the review)." if transient else ".")
        ),
    )


def check_outcome_for_result(
    result: ReviewResult, *, policy: str, required_fixes: bool | None = None
) -> CheckOutcome:
    """Map one validated review result onto the gate conclusions."""

    if not isinstance(result, ReviewResult):
        raise ReviewInputError("check outcome requires a validated review result")
    return review_check_outcome(
        policy=policy,
        review_status=result.review_status,
        required_fixes=(
            has_blocking_findings(result) if required_fixes is None else required_fixes
        ),
    )


class ReviewCheckPublisher:
    """Create or update the one stable ReviewSensei check for an exact head.

    A run is reused only when its installed App slug matches the expected
    producer, so this publisher never completes, overwrites, or inherits a
    same-named check owned by another App. Every write is bound to the reviewed
    head: the started check carries ``in_progress`` so an earlier success on
    the same head cannot stand in for a pending current review.

    The pending window is exactly the publication window. A run that dies
    inside it leaves ``in_progress`` behind, and the next review of the same
    head rewrites that same run, so an interrupted run cannot leave a passing
    conclusion standing in for a review that never completed. A caller that
    knows its run ended without a conclusion can publish that state directly
    with :meth:`cancel` instead of waiting for the healing rerun.
    """

    def __init__(self, *, http: GitHubHttp) -> None:
        self.http = http

    def start(
        self,
        *,
        token: str,
        repository: str,
        head_sha: str,
        app_slug: str,
    ) -> CheckPublicationResult:
        """Publish the pending check state for one new eligible review."""

        return self._write(
            token=token,
            repository=repository,
            head_sha=head_sha,
            app_slug=app_slug,
            status="in_progress",
            title="ReviewSensei review in progress",
            summary=(
                "A new ReviewSensei review is running for this head; any "
                "earlier conclusion on this head does not stand for it."
            ),
            conclusion=None,
        )

    def complete(
        self,
        *,
        token: str,
        repository: str,
        head_sha: str,
        app_slug: str,
        outcome: CheckOutcome,
    ) -> CheckPublicationResult:
        """Publish the final gate conclusion for one reviewed head."""

        if outcome.conclusion not in COMPLETED_CONCLUSIONS:
            raise GitHubPublicationError("check conclusion is invalid")
        validate_bounded_text(
            outcome.title,
            MAX_CHECK_TITLE_CHARS,
            label="check title",
            allow_empty=False,
        )
        validate_bounded_text(
            outcome.summary,
            MAX_CHECK_SUMMARY_CHARS,
            label="check summary",
            allow_empty=False,
        )
        return self._write(
            token=token,
            repository=repository,
            head_sha=head_sha,
            app_slug=app_slug,
            status="completed",
            title=outcome.title,
            summary=outcome.summary,
            conclusion=outcome.conclusion,
        )

    def cancel(
        self,
        *,
        token: str,
        repository: str,
        head_sha: str,
        app_slug: str,
    ) -> CheckPublicationResult:
        """Publish the non-passing cancellation conclusion for one run.

        ``cancelled`` is GitHub's own conclusion for a run that stopped before
        reaching a result, and it is deliberately not policy-dependent: a
        required check must never pass because a run was interrupted. Advisory
        mode is unaffected because it tells administrators not to require this
        check at all.
        """

        return self._write(
            token=token,
            repository=repository,
            head_sha=head_sha,
            app_slug=app_slug,
            status="completed",
            title="Review cancelled",
            summary=(
                "This review run ended without publishing a conclusion for "
                "this head. The check stays non-passing until an eligible "
                "review of this head completes."
            ),
            conclusion="cancelled",
        )

    def _write(
        self,
        *,
        token: str,
        repository: str,
        head_sha: str,
        app_slug: str,
        status: str,
        title: str,
        summary: str,
        conclusion: str | None,
    ) -> CheckPublicationResult:
        if status not in {"in_progress", "completed"}:
            raise GitHubPublicationError("check status is invalid")
        if (status == "completed") != (conclusion is not None):
            raise GitHubPublicationError("check conclusion is invalid")
        if not isinstance(app_slug, str) or not app_slug.strip():
            raise GitHubPublicationError("check producer slug is invalid")
        existing = self._find_own_run(
            token=token,
            repository=repository,
            head_sha=head_sha,
            app_slug=app_slug,
        )
        if existing is not None:
            return self._update(
                token=token,
                repository=repository,
                check_run_id=existing,
                status=status,
                title=title,
                summary=summary,
                conclusion=conclusion,
            )
        return self._create(
            token=token,
            repository=repository,
            head_sha=head_sha,
            status=status,
            title=title,
            summary=summary,
            conclusion=conclusion,
        )

    def _find_own_run(
        self,
        *,
        token: str,
        repository: str,
        head_sha: str,
        app_slug: str,
    ) -> int | None:
        """Return the App-owned check-run id for this head, if one exists."""

        path = (
            self.http.repository_path(repository, f"/commits/{head_sha}/check-runs")
            + f"?check_name={REVIEW_CHECK_NAME}&per_page={MAX_CHECK_PAGE_SIZE}"
        )
        try:
            status, payload = self.http.request("GET", path, token=token)
        except GitHubHTTPTransientError as exc:
            raise GitHubPublicationTransientError(
                "check lookup failed temporarily"
            ) from exc
        except GitHubHTTPError as exc:
            raise GitHubPublicationError("check lookup failed") from exc
        if status < 200 or status >= 300:
            raise GitHubPublicationError("check lookup failed")
        if not isinstance(payload, Mapping):
            raise GitHubPublicationError("check lookup response was invalid")
        runs = payload.get("check_runs")
        if not isinstance(runs, list):
            raise GitHubPublicationError("check lookup response was invalid")
        expected = app_slug.casefold()
        for run in runs:
            if not isinstance(run, Mapping):
                continue
            if run.get("name") != REVIEW_CHECK_NAME:
                continue
            app = run.get("app")
            slug = app.get("slug") if isinstance(app, Mapping) else None
            if not isinstance(slug, str) or slug.casefold() != expected:
                continue
            check_run_id = run.get("id")
            if isinstance(check_run_id, int) and not isinstance(check_run_id, bool):
                return check_run_id
        return None

    def _create(
        self,
        *,
        token: str,
        repository: str,
        head_sha: str,
        status: str,
        title: str,
        summary: str,
        conclusion: str | None,
    ) -> CheckPublicationResult:
        body: dict[str, object] = {
            "name": REVIEW_CHECK_NAME,
            "head_sha": head_sha,
            "status": status,
            "external_id": REVIEW_CHECK_EXTERNAL_ID,
            "output": {"title": title, "summary": summary},
        }
        if conclusion is not None:
            body["conclusion"] = conclusion
        status_code, payload = self._request(
            "POST", self.http.repository_path(repository, "/check-runs"), token, body
        )
        if status_code == 403:
            return CheckPublicationResult(
                status="check_permission_denied", diagnostic="check_permission"
            )
        if status_code == 404:
            raise GitHubPublicationError("check target was not found")
        if status_code == 422:
            raise GitHubPublicationError("check publication was rejected")
        if status_code == 201 and isinstance(payload, Mapping):
            check_run_id = payload.get("id")
            if isinstance(check_run_id, int) and not isinstance(check_run_id, bool):
                return CheckPublicationResult(
                    status="created", check_run_id=check_run_id
                )
        raise GitHubPublicationError("check publication was rejected")

    def _update(
        self,
        *,
        token: str,
        repository: str,
        check_run_id: int,
        status: str,
        title: str,
        summary: str,
        conclusion: str | None,
    ) -> CheckPublicationResult:
        body: dict[str, object] = {
            "status": status,
            "output": {"title": title, "summary": summary},
        }
        if conclusion is not None:
            body["conclusion"] = conclusion
        status_code, payload = self._request(
            "PATCH",
            self.http.repository_path(repository, f"/check-runs/{check_run_id}"),
            token,
            body,
        )
        if status_code == 403:
            return CheckPublicationResult(
                status="check_permission_denied", diagnostic="check_permission"
            )
        if status_code == 404:
            raise GitHubPublicationError("check target was not found")
        if status_code == 422:
            raise GitHubPublicationError("check update was rejected")
        if status_code == 200 and isinstance(payload, Mapping):
            updated_id = payload.get("id")
            if isinstance(updated_id, int) and not isinstance(updated_id, bool):
                return CheckPublicationResult(status="updated", check_run_id=updated_id)
        raise GitHubPublicationError("check update was rejected")

    def _request(
        self,
        method: str,
        path: str,
        token: str,
        body: dict[str, object],
    ) -> tuple[int, Mapping[str, object] | None]:
        try:
            status, payload = self.http.request(method, path, token=token, body=body)
        except GitHubHTTPTransientError as exc:
            raise GitHubPublicationTransientError(
                "check publication failed temporarily"
            ) from exc
        except GitHubHTTPError as exc:
            raise GitHubPublicationError("check publication failed") from exc
        if isinstance(payload, Mapping):
            return status, payload
        return status, None


__all__ = [
    "COMPLETED_CONCLUSIONS",
    "CheckOutcome",
    "CheckPublicationResult",
    "REVIEW_CHECK_NAME",
    "REVIEW_POLICY_MODES",
    "ReviewCheckPublisher",
    "check_outcome_for_result",
    "required_check_identity",
    "review_check_outcome",
    "review_check_outcome_for_publication_failure",
]
