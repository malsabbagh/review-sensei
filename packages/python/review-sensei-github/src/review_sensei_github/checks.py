"""One stable ReviewSensei check-run gate bound to the App and reviewed head.

YAML cannot make a check required, so this module publishes the product's
authoritative check and reports its identity for administrator setup. The gate
conclusion never encodes incomplete enforcement as ``neutral`` or ``skipped``:
GitHub accepts those conclusions for required checks, so they would silently
disable the gate. ``advisory`` is the one mode that publishes ``neutral``,
because that mode promises no ReviewSensei-imposed merge gate. In
``auto-approve`` and ``blocking`` modes, a completed review publishes
``failure`` when required fixes remain, ``action_required`` when no required
fixes remain and a required human decision is pending, and ``success`` when
all obligations are satisfied.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from review_sensei.configuration import REVIEW_POLICIES
from review_sensei.errors import ReviewInputError
from review_sensei.models import ReviewResult
from review_sensei.review_verdict import ReviewVerdict
from review_sensei.validation import validate_bounded_text

from .approval import has_blocking_findings, has_human_adjudication_findings
from .check_diagnostics import (
    CheckFailure,
    CheckOperationError,
    CheckOperationTransientError,
    check_failure,
)
from .errors import (
    GitHubHTTPError,
    GitHubHTTPTransientError,
    GitHubPublicationError,
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
    failure: CheckFailure | None = None


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
    required_decisions: bool = False,
    mixed_coverage_satisfied: bool = False,
    coverage_uncertainty_accepted: bool = False,
    review_verdict: ReviewVerdict | None = None,
) -> CheckOutcome:
    """Map one review outcome onto the documented gate conclusions."""

    if policy not in REVIEW_POLICY_MODES:
        raise ReviewInputError("review policy mode is invalid")
    if review_status not in {"complete", "partial", "incomplete", "summary-only"}:
        raise ReviewInputError("review status is invalid")
    if review_verdict is not None and not isinstance(review_verdict, ReviewVerdict):
        raise ReviewInputError("check outcome review verdict is invalid")
    unselected_fixes = bool(
        review_verdict and review_verdict.unselected_blocking_defect
    )
    unselected_decisions = bool(
        review_verdict and review_verdict.unselected_human_decision
    )
    bounded_required = required_fixes is True or unselected_fixes
    bounded_decisions = required_decisions is True or unselected_decisions
    required_guidance = (
        (
            "Published findings are listed in the ReviewSensei review comments. "
            if required_fixes is True or required_decisions is True
            else ""
        )
        + "A qualifying review must establish that these requirements are satisfied."
        if unselected_fixes or unselected_decisions
        else "They are listed in the ReviewSensei review comments."
    )
    bounded_mixed = mixed_coverage_satisfied is True
    bounded_coverage_accepted = coverage_uncertainty_accepted is True
    if policy == "advisory":
        detail = (
            "Required fixes were reported but enforcement is disabled by "
            "github.reviews: advisory."
            if bounded_required
            else (
                "Findings requiring a human decision were reported, but "
                "enforcement is disabled by github.reviews: advisory."
                if bounded_decisions
                else "ReviewSensei does not impose a merge gate in advisory mode."
            )
        )
        return CheckOutcome(
            conclusion="neutral",
            title="Advisory review — not enforced",
            summary=detail,
        )
    effective_complete = (
        review_status == "complete"
        or (review_status == "partial" and bounded_mixed)
        or (review_status == "partial" and bounded_coverage_accepted)
    )
    if not effective_complete:
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
        if bounded_decisions:
            return CheckOutcome(
                conclusion="failure",
                title="Required fixes and human decisions remain",
                summary=(
                    "The completed review reported required fixes and findings "
                    "that require a human decision for this head. " + required_guidance
                ),
            )
        return CheckOutcome(
            conclusion="failure",
            title="Required fixes remain",
            summary=(
                "The completed review reported required fixes for this head. "
                + required_guidance
            ),
        )
    if bounded_decisions:
        return CheckOutcome(
            conclusion="action_required",
            title="Human decision required",
            summary=(
                "The completed review reported findings that require a human "
                "decision before approval."
                + (" " + required_guidance if unselected_decisions else "")
            ),
        )
    if review_status == "partial" and bounded_mixed:
        return CheckOutcome(
            conclusion="success",
            title="No required fixes",
            summary=(
                "AI-reviewed text and human-confirmed non-text coverage "
                "satisfied the review gate for this head (AI review status "
                "remains partial)."
            ),
        )
    if review_status == "partial" and bounded_coverage_accepted:
        return CheckOutcome(
            conclusion="success",
            title="Coverage uncertainty accepted by maintainer",
            summary=(
                "Incomplete AI coverage was explicitly accepted by a maintainer "
                "for this head (AI review status remains partial)."
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


def _coverage_recovery_guidance(result: ReviewResult) -> str:
    coverage = result.coverage
    if coverage is None:
        return ""
    uncovered = [entry for entry in coverage.files if entry.outcome != "reviewed"]
    if not uncovered and coverage.enumeration_complete:
        return ""
    if uncovered:
        from review_sensei.human_file_review import is_human_confirmable_file

        entries: list[str] = []
        for entry in uncovered[:10]:
            reason = entry.reason or entry.outcome
            action = (
                "confirm human file review"
                if is_human_confirmable_file(entry)
                else "rescan to complete coverage"
            )
            entries.append(f"{entry.path} ({reason}: {action})")
        listed = "; ".join(entries)
        if len(uncovered) > 10:
            listed += f"; +{len(uncovered) - 10} more"
        if not coverage.enumeration_complete:
            listed += "; diff enumeration incomplete (rescan to complete coverage)"
        return f" Uncovered files: {listed}."
    return " Diff enumeration was incomplete. Recovery: rescan to complete coverage."


def check_outcome_for_result(
    result: ReviewResult,
    *,
    policy: str,
    required_fixes: bool | None = None,
    required_decisions: bool | None = None,
    mixed_coverage_satisfied: bool = False,
) -> CheckOutcome:
    """Map one validated review result onto the gate conclusions."""

    if not isinstance(result, ReviewResult):
        raise ReviewInputError("check outcome requires a validated review result")
    outcome = review_check_outcome(
        policy=policy,
        review_status=result.review_status,
        required_fixes=(
            has_blocking_findings(result) if required_fixes is None else required_fixes
        ),
        required_decisions=(
            has_human_adjudication_findings(result)
            if required_decisions is None
            else required_decisions
        ),
        mixed_coverage_satisfied=mixed_coverage_satisfied,
        review_verdict=result.review_verdict,
    )
    if result.persistence_status == "capacity-exceeded":
        detail = "Analysis completed, but complete evidence could not fit the durable baseline. Publication retains the full validated finding inventory."
        if policy == "advisory":
            detail += " This advisory policy does not request automatic approval or impose a merge gate."
        else:
            detail += " Approval is withheld."
        if any(comment.needs_human for comment in result.comments):
            detail += " Human assessment obligations remain open independently."
        return CheckOutcome(
            conclusion=outcome.conclusion,
            title="Analysis complete — baseline capacity exceeded"
            + (" (approval withheld)" if policy != "advisory" else ""),
            summary=detail,
        )
    if (
        result.review_status != "complete"
        and not (result.review_status == "partial" and mixed_coverage_satisfied is True)
        and result.coverage is not None
    ):
        guidance = _coverage_recovery_guidance(result)
        if guidance:
            if len(outcome.summary) + len(guidance) <= MAX_CHECK_SUMMARY_CHARS:
                summary = outcome.summary + guidance
            else:
                head_limit = max(0, MAX_CHECK_SUMMARY_CHARS - len(guidance))
                summary = (outcome.summary[:head_limit] + guidance)[
                    :MAX_CHECK_SUMMARY_CHARS
                ]
            return CheckOutcome(
                conclusion=outcome.conclusion,
                title=outcome.title,
                summary=summary,
            )
    return outcome


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
        from .controlled_restart import effect_guard

        if effect_guard(self.http) is not None and outcome.conclusion in {
            "success",
            "neutral",
        }:
            # Fresh analysis cannot certify obligations in an opaque archive.
            # Keep the independent completed result, but never let abandonment
            # of historical execution turn into a passing required check.
            outcome = CheckOutcome(
                "action_required",
                "Historical review remains unsettled",
                "The fresh review completed, but historical findings, decisions "
                "and delayed effects remain unsettled. Controlled restart does "
                "not certify those obligations or authorize approval.",
            )
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

    def _repository_path(self, repository: str, suffix: str) -> str:
        repo_path = getattr(self.http, "repository_path", None)
        if callable(repo_path):
            return str(repo_path(repository, suffix))
        return f"/repos/{repository}{suffix}"

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
            self._repository_path(repository, f"/commits/{head_sha}/check-runs")
            + f"?check_name={REVIEW_CHECK_NAME}&per_page={MAX_CHECK_PAGE_SIZE}"
        )
        try:
            status, payload = self.http.request("GET", path, token=token)
        except GitHubHTTPTransientError:
            raise CheckOperationTransientError(
                check_failure("lookup", transient=True)
            ) from None
        except GitHubHTTPError:
            raise CheckOperationError(check_failure("lookup")) from None
        if type(status) is not int or status < 200 or status >= 300:
            raise CheckOperationError(check_failure("lookup", status=status))
        if not isinstance(payload, Mapping):
            raise CheckOperationError(check_failure("lookup", status=status))
        runs = payload.get("check_runs")
        if not isinstance(runs, list):
            raise CheckOperationError(check_failure("lookup", status=status))
        expected = app_slug.casefold()
        # Review/comment users expose the App's REST Bot login, whereas a
        # native check's App object exposes its unsuffixed slug. Both identify
        # the same producer; never strip a suffix from an arbitrary returned
        # App or accept a prefix/sibling match.
        expected_slugs = {expected}
        if expected.endswith("[bot]") and expected[:-5]:
            expected_slugs.add(expected[:-5])
        for run in runs:
            if not isinstance(run, Mapping):
                continue
            if run.get("name") != REVIEW_CHECK_NAME:
                continue
            if "head_sha" in run and run["head_sha"] != head_sha:
                continue
            app = run.get("app")
            slug = app.get("slug") if isinstance(app, Mapping) else None
            if not isinstance(slug, str) or slug.casefold() not in expected_slugs:
                continue
            check_run_id = run.get("id")
            if isinstance(check_run_id, int) and not isinstance(check_run_id, bool):
                from .controlled_restart import owns_target

                if owns_target(
                    self.http,
                    self._repository_path(repository, f"/check-runs/{check_run_id}"),
                ):
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
            "POST", self._repository_path(repository, "/check-runs"), token, body
        )
        if status_code == 403:
            return CheckPublicationResult(
                status="check_permission_denied",
                diagnostic="check_access_denied",
                failure=check_failure("create", status=status_code),
            )
        if status_code == 201 and isinstance(payload, Mapping):
            check_run_id = payload.get("id")
            if isinstance(check_run_id, int) and not isinstance(check_run_id, bool):
                return CheckPublicationResult(
                    status="created", check_run_id=check_run_id
                )
        raise CheckOperationError(check_failure("create", status=status_code))

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
            self._repository_path(repository, f"/check-runs/{check_run_id}"),
            token,
            body,
        )
        if status_code == 403:
            return CheckPublicationResult(
                status="check_permission_denied",
                diagnostic="check_access_denied",
                failure=check_failure("update", status=status_code),
            )
        if status_code == 200 and isinstance(payload, Mapping):
            updated_id = payload.get("id")
            if isinstance(updated_id, int) and not isinstance(updated_id, bool):
                return CheckPublicationResult(status="updated", check_run_id=updated_id)
        raise CheckOperationError(check_failure("update", status=status_code))

    def _request(
        self,
        method: str,
        path: str,
        token: str,
        body: dict[str, object],
    ) -> tuple[int, Mapping[str, object] | None]:
        try:
            status, payload = self.http.request(method, path, token=token, body=body)
        except GitHubHTTPTransientError:
            raise CheckOperationTransientError(
                check_failure(
                    "create" if method == "POST" else "update", transient=True
                )
            ) from None
        except GitHubHTTPError:
            raise CheckOperationError(
                check_failure("create" if method == "POST" else "update")
            ) from None
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
