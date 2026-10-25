"""Compose the explicit media command from independently retained hosted state.

This boundary owns no allowance. Metadata, retained bytes, confirmation and
publication all spend the caller's original budget and physical transport.
"""

from __future__ import annotations

from copy import copy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping
from urllib.parse import quote

from review_sensei.bounded_evidence import EvidenceReadBudget
from review_sensei.context import finding_lifecycle_for_comment
from review_sensei.errors import ReviewInputError
from review_sensei.human_assessment import finding_instance_fingerprint
from review_sensei.human_file_review import is_blanket_media_approval, positive
from review_sensei.mixed_coverage import CurrentFindingFacts
from review_sensei.models import ReviewResult, ReviewTransaction
from review_sensei.session import SessionIdentity

from .approval import ReviewApprovalEligibility
from .checks import REVIEW_POLICY_MODES
from .http import GitHubHttp
from .human_file_review import HumanFileReviewPolicy, HumanFileReviewPublisher
from .media_budget import media_http
from .publication import (
    APPROVAL_ELIGIBILITY_MARKER_PREFIX,
    FINDING_INDEX_MARKER_PREFIX,
    FindingIndex,
    ReviewApprovalFinalizer,
    review_result_digest,
)
from .publication_recovery import _publication_transport, publication_http
from .session_ledger import GitHubIssueCommentSessionLedger

if TYPE_CHECKING:
    from .application import GitHubApplication, GitHubWriteOptions


@dataclass(frozen=True)
class HostedMediaContext:
    publisher: HumanFileReviewPublisher
    result: ReviewResult
    request_comment_id: int


class _MediaTransport(GitHubHttp):
    """Route reads to existing metadata access, without owning accounting.

    The session capability already permits PR review/comment writes. Immutable
    trees use the independently supplied Contents-read metadata credential.
    Only the verified Bot identity observed during canonical retention loading
    is retained; responses and mutable authorization facts are never cached.
    """

    def __init__(self, http: GitHubHttp, *, read_token: str, app_slug: str) -> None:
        super().__init__(api_url=http.api_url, timeout=getattr(http, "timeout", 30))
        self.transport = http
        self.read_token = read_token
        self.app_path = "/users/" + quote(app_slug, safe="")
        self.observed_app_identity: Mapping[str, Any] | None = None

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        body: dict[str, object] | None = None,
        timeout_seconds: float | None = None,
    ) -> tuple[int, Any]:
        read = method == "GET" or (method == "POST" and path == "/graphql")
        response = self.transport.request(
            method,
            path,
            token=self.read_token if read else token,
            body=body,
            timeout_seconds=timeout_seconds,
        )
        if method == "GET" and path == self.app_path:
            status, value = response
            self.observed_app_identity = (
                dict(value) if status == 200 and isinstance(value, Mapping) else None
            )
        return response


def prepare_hosted_media_context(
    *,
    application: GitHubApplication,
    options: GitHubWriteOptions,
    http: GitHubHttp,
    budget: EvidenceReadBudget,
    session_token: str,
    read_token: str,
    repository: str,
    repository_id: int,
    pull_request: int,
    head_sha: str,
    source_comment_id: int,
    actor_login: str,
    body: str,
    app_slug: str,
    reviews_policy: str,
) -> HostedMediaContext:
    """Authenticate the current result and source before creating a request.

    ``actor_login`` and ``body`` must be the command reconstructed from the
    broker attestation, not workflow event routing hints. The live source is
    independently checked against both before any visible media write.
    """

    if (
        not isinstance(budget, EvidenceReadBudget)
        or not isinstance(http, GitHubHttp)
        or reviews_policy not in REVIEW_POLICY_MODES
        or not options.github_writes
        or not options.github_session_ledger
        or not isinstance(session_token, str)
        or not session_token.strip()
        or not isinstance(read_token, str)
        or not read_token.strip()
        or not positive(source_comment_id)
        or not isinstance(actor_login, str)
        or not actor_login
        or not is_blanket_media_approval(body)
    ):
        raise ReviewInputError("hosted media command context is invalid")
    identity = SessionIdentity(repository, pull_request, repository_id=repository_id)
    # Validate any existing accounting wrapper before removing it for the
    # publisher, whose _io guard already charges each physical dispatch.
    publication_http(http, budget)
    routed = _MediaTransport(
        media_http(_publication_transport(http)),
        read_token=read_token,
        app_slug=app_slug,
    )
    charged = publication_http(routed, budget)
    owner = copy(application)
    owner.http = routed
    owner.session_ledger = None
    ledger = owner._session_ledger_for_token(
        read_token,
        options=options,
        app_slug=app_slug,
        prefer_remote=True,
        evidence_budget=budget,
    )
    if not isinstance(ledger, GitHubIssueCommentSessionLedger):
        raise ReviewInputError("hosted media requires the remote session ledger")
    loaded = ledger.load(identity)
    if loaded.status not in {"ok", "migrated"} or loaded.record is None:
        raise ReviewInputError("hosted media current session is unavailable")
    transaction = loaded.record.transaction
    if (
        not isinstance(transaction, ReviewTransaction)
        or transaction.repository != repository
        or transaction.pull_request != pull_request
        or transaction.head_sha != head_sha
        or transaction.result_sha256 is None
        or transaction.phase == "publication_suppressed"
    ):
        raise ReviewInputError("hosted media current transaction is unavailable")
    # The ledger charges its own _request calls. Its canonical retained loader
    # instead calls _store directly, so give only that copied loader a charged
    # transport and the already loaded transaction (no second ledger load).
    retained_loader = copy(ledger)
    retained_loader.http = charged
    try:
        result = retained_loader.load_retained_checkpoint_result(
            identity, transaction=transaction, head_sha=head_sha
        )
    except ValueError as exc:
        raise ReviewInputError(
            "hosted media retained review evidence is invalid"
        ) from exc
    if not isinstance(result, ReviewResult) or result.transaction != transaction:
        raise ReviewInputError("hosted media retained review result is unavailable")
    app = routed.observed_app_identity
    app_id = app.get("id") if isinstance(app, Mapping) else None
    if (
        not isinstance(app, Mapping)
        or not positive(app_id)
        or app.get("type") != "Bot"
        or not isinstance(app.get("login"), str)
        or app["login"].casefold() != app_slug.casefold()
        or ledger._evidence_author_id != app_id
    ):
        raise ReviewInputError("hosted media App numeric ownership conflicts")

    finalizer = copy(application.reviewer.finalizer)
    if not isinstance(finalizer, ReviewApprovalFinalizer):
        raise ReviewInputError("hosted media approval authority is unavailable")
    finalizer.http = charged
    finalizer.evidence_budget = budget
    finalizer.human_inventory_read_token = read_token
    finalizer.human_inventory_reader_factory = owner._human_inventory_reader
    # Scoped readers must be recreated for this invocation and allowance.
    finalizer._human_inventory_scope = None
    finalizer._scoped_human_inventory_reader = None
    current_index: FindingIndex | None = None
    callback_index: FindingIndex | None = None
    index_fenced = False

    def preflight() -> Any:
        current = finalizer._preflight(
            token=read_token,
            repository=repository,
            pull_request=pull_request,
            head_sha=head_sha,
            app_slug=app_slug,
        )
        if (
            current.result is not None
            or current.repository_id != repository_id
            or current.base_sha != transaction.base_sha
            or current.app_authored
        ):
            raise ReviewInputError("hosted media live approval snapshot is ineligible")
        return current

    def validate_snapshot(payload: Mapping[str, Any]) -> None:
        base, head, author = (
            payload.get("base"),
            payload.get("head"),
            payload.get("user"),
        )
        if (
            payload.get("draft") is not False
            or not isinstance(base, Mapping)
            or not isinstance(head, Mapping)
            or base.get("sha") != transaction.base_sha
            or head.get("sha") != transaction.head_sha
            or not isinstance(author, Mapping)
            or not isinstance(author.get("login"), str)
            or not author["login"]
            or author["login"].casefold() == app_slug.casefold()
        ):
            raise ReviewInputError("hosted media final snapshot is ineligible")
        for value in (base.get("repo"), head.get("repo")):
            if (
                not isinstance(value, Mapping)
                or type(value.get("id")) is not int
                or value["id"] != repository_id
                or value.get("full_name") != repository
                or value.get("fork") is not False
            ):
                raise ReviewInputError(
                    "hosted media final repository snapshot conflicts"
                )

    def eligibility_reader(
        *,
        root: Mapping[str, Any],
        reviews: list[Any],
        result: ReviewResult,
        head_sha: str,
    ) -> ReviewApprovalEligibility:
        nonlocal current_index
        ordered = sorted(
            reviews,
            key=lambda item: (
                item.get("id", -1)
                if isinstance(item, Mapping) and type(item.get("id")) is int
                else -1
            ),
        )
        roots = [
            item
            for item in ordered
            if isinstance(item, Mapping)
            and item.get("commit_id") == head_sha
            and isinstance(item.get("user"), Mapping)
            and item["user"].get("login") == app_slug
            and isinstance(item.get("body"), str)
            and APPROVAL_ELIGIBILITY_MARKER_PREFIX in item["body"]
        ]
        if not roots or roots[-1] != root:
            raise ReviewInputError("hosted media latest review selection conflicts")
        eligibility = finalizer._eligibility_from_reviews(
            ordered,
            head_sha=head_sha,
            app_slug=app_slug,
            require_valid=True,
            token=read_token,
            repository=repository,
            pull_request=pull_request,
        )
        if (
            eligibility is None
            or eligibility.partitioned_root is not None
            or eligibility.head_sha != transaction.head_sha
            or eligibility.result_digest != review_result_digest(result)
            or eligibility.facts.provider != result.provider.strip().lower()
            or (
                eligibility.human_review is not None
                and eligibility.human_review.base_sha != transaction.base_sha
            )
        ):
            raise ReviewInputError("hosted media owned review result conflicts")
        for item in ordered:
            if (
                isinstance(item, Mapping)
                and item.get("commit_id") == head_sha
                and isinstance(item.get("user"), Mapping)
                and item["user"].get("login") == app_slug
                and isinstance(item.get("body"), str)
                and FINDING_INDEX_MARKER_PREFIX in item["body"]
                and (
                    item["user"].get("type") != "Bot"
                    or type(item["user"].get("id")) is not int
                    or item["user"]["id"] != app_id
                )
            ):
                raise ReviewInputError("hosted media finding index ownership conflicts")
        index = finalizer.finding_index_from_reviews(
            ordered,
            repository_id=repository_id,
            pull_request=pull_request,
            head_sha=head_sha,
            app_slug=app_slug,
            eligibility=eligibility,
        )
        if index is not None and (
            index.base_sha != transaction.base_sha
            or index.result_digest != review_result_digest(result)
        ):
            raise ReviewInputError("hosted media current finding index conflicts")
        if index_fenced and index != callback_index:
            raise ReviewInputError("hosted media finding index changed during approval")
        current_index = index
        return eligibility

    def live_approval_facts(
        *, request: Any, result: ReviewResult, eligibility: ReviewApprovalEligibility
    ) -> Mapping[str, object]:
        nonlocal callback_index, index_fenced
        if (
            request.base_sha != transaction.base_sha
            or request.head_sha != transaction.head_sha
            or request.result_digest != review_result_digest(result)
        ):
            raise ReviewInputError("hosted media approval request conflicts")
        current = preflight()
        open_threads, resolved = finalizer._scan_blocking_threads(
            token=read_token,
            repository=repository,
            pull_request=pull_request,
            repository_id=repository_id,
            head_sha=head_sha,
            base_sha=transaction.base_sha,
            app_slug=app_slug,
        )
        index = current_index
        entries = (
            {entry.instance: entry for entry in index.entries}
            if index is not None
            else {}
        )
        inventory = eligibility.human_review
        human_entries = (
            {item.fingerprint: item for item in inventory.findings}
            if inventory is not None
            else {}
        )
        human_resolved = set(inventory.resolved) if inventory is not None else set()
        blocking = eligibility.facts.has_blocking_findings or any(
            entry.blocking and (entry.body_placed or entry.instance not in resolved)
            for entry in entries.values()
        )
        human = eligibility.facts.has_human_adjudication_findings or bool(
            inventory is not None and inventory.pending
        )
        for comment in result.comments:
            instance = finding_instance_fingerprint(comment)
            entry = entries.get(instance)
            exact_entry = (
                entry is not None
                and entry.lifecycle
                == finding_lifecycle_for_comment(comment).fingerprint
            )
            # Canonical result JSON preserves the model proposal, not runtime
            # admission. Only this exact authenticated instance/lifecycle can
            # replace it; missing or mismatched authority keeps the raw fence.
            admitted_blocking = (
                entry.blocking
                if entry is not None and exact_entry
                else comment.blocks_approval
            )
            admitted_human = (
                entry.needs_human
                if entry is not None and exact_entry
                else comment.needs_human
            )
            if admitted_blocking and not (
                entry is not None
                and exact_entry
                and entry.blocking
                and not entry.body_placed
                and instance in resolved
            ):
                blocking = True
            if admitted_human:
                retained_human = human_entries.get(instance)
                if (
                    entry is None
                    or not exact_entry
                    or not entry.needs_human
                    or retained_human is None
                    or retained_human.path != comment.path
                    or retained_human.body != comment.body
                    or instance not in human_resolved
                ):
                    human = True
        # An index-only human obligation still requires its authenticated
        # resolution inventory; a closed inline thread cannot substitute.
        if any(
            entry.needs_human
            and (
                entry.instance not in human_entries
                or entry.instance not in human_resolved
            )
            for entry in entries.values()
        ):
            human = True
        callback_index, index_fenced = index, True
        qualification = (
            eligibility.facts.qualification
            if finalizer._qualification_authorizes(
                eligibility,
                repository=repository,
                pull_request=pull_request,
                base_sha=transaction.base_sha,
            )
            else "unverified"
        )
        return {
            "enabled": reviews_policy == "auto-approve",
            "app_authored": current.app_authored,
            "has_open_review_threads": open_threads,
            "qualification": qualification,
            "current_findings": CurrentFindingFacts(
                result.content_digest(), bool(blocking), bool(human)
            ),
        }

    def before_approval(required_dispatches: int) -> None:
        budget.check()
        if type(required_dispatches) is not int or required_dispatches not in {2, 3}:
            raise ReviewInputError("hosted media approval tail is invalid")
        if required_dispatches > budget.ordinary_remaining():
            raise ReviewInputError(
                "media review receipt is retained; the original request budget "
                "cannot fund approval readback; retry the same supported media command"
            )

    source_path = charged.repository_path(
        repository, f"/issues/comments/{source_comment_id}"
    )
    status, source = charged.request("GET", source_path, token=read_token)
    user = source.get("user") if isinstance(source, Mapping) else None
    if (
        status != 200
        or not isinstance(source, Mapping)
        or type(source.get("id")) is not int
        or source["id"] != source_comment_id
        or source.get("issue_url")
        != charged.api_url
        + charged.repository_path(repository, f"/issues/{pull_request}")
        or source.get("body") != body
        or source.get("author_association") not in {"OWNER", "MEMBER", "COLLABORATOR"}
        or not isinstance(user, Mapping)
        or user.get("type") != "User"
        or not positive(user.get("id"))
        or user.get("login") != actor_login
        or actor_login.casefold() == app_slug.casefold()
    ):
        raise ReviewInputError("hosted media live source does not match the command")
    status, permission = charged.request(
        "GET",
        charged.repository_path(
            repository, f"/collaborators/{quote(actor_login, safe='')}/permission"
        ),
        token=read_token,
    )
    permission_user = (
        permission.get("user") if isinstance(permission, Mapping) else None
    )
    if (
        status != 200
        or not isinstance(permission, Mapping)
        or permission.get("permission") not in {"maintain", "admin"}
        or not isinstance(permission_user, Mapping)
        or type(permission_user.get("id")) is not int
        or permission_user["id"] != user["id"]
        or permission_user.get("login") != actor_login
    ):
        raise ReviewInputError("hosted media approval requires the live maintainer")
    reviews = charged.paginate(
        path=charged.repository_path(repository, f"/pulls/{pull_request}/reviews"),
        token=read_token,
        max_requests=10,
        timeout_seconds=budget._remaining_seconds(),
    )
    candidates = [
        item
        for item in reviews
        if isinstance(item, dict)
        and item.get("commit_id") == head_sha
        and isinstance(item.get("user"), Mapping)
        and item["user"].get("login") == app_slug
        and isinstance(item.get("body"), str)
        and APPROVAL_ELIGIBILITY_MARKER_PREFIX in item["body"]
    ]
    if (
        not candidates
        or any(not positive(item.get("id")) for item in candidates)
        or len({item["id"] for item in candidates}) != len(candidates)
    ):
        raise ReviewInputError("hosted media latest owned review is unavailable")
    root = max(candidates, key=lambda item: item["id"])
    if (
        root["user"].get("type") != "Bot"
        or type(root["user"].get("id")) is not int
        or root["user"]["id"] != app_id
        or root.get("state") != "COMMENTED"
    ):
        raise ReviewInputError("hosted media latest review ownership conflicts")
    eligibility_reader(
        root=root,
        reviews=reviews,
        result=result,
        head_sha=head_sha,
    )
    publisher = HumanFileReviewPublisher(
        http=routed,
        api_url=charged.api_url,
        repository=repository,
        repository_id=repository_id,
        pull_request=pull_request,
        app_login=app_slug,
        app_user_id=app_id,
        token=session_token,
        before_request=budget.consume,
        policy=HumanFileReviewPolicy(allow_confirmations=True),
        approval_facts_provider=live_approval_facts,
        eligibility_reader=eligibility_reader,
        snapshot_validator=validate_snapshot,
        before_approval=before_approval,
    )
    request_comment_id, _request = publisher.request_review(
        review_id=root["id"], result=result
    )
    return HostedMediaContext(publisher, result, request_comment_id)
