"""Explicit trusted-host publication; the ordinary REST writer never opts in.

Host callbacks own original operation authorization, serialized generation
mutation and durable reconciliation. A plan is independent expected context,
not a grant. Unknown activation must retain the original attempt as unknown.
"""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Mapping, Protocol

from review_sensei.bounded_evidence import (
    EvidenceReadBudget,
    canonical_bytes,
    partition_evidence,
    validate_binding,
    validate_manifest,
)
from review_sensei.convergence import ReviewConvergencePolicy
from review_sensei.errors import ReviewInputError
from review_sensei.human_assessment import (
    HumanInventoryResolution,
    finding_instance_fingerprint,
)
from review_sensei.human_inventory import inventory_result_document
from review_sensei.models import ReviewResult, ReviewTransaction

from .approval import ReviewApprovalEligibility, approval_eligibility_from_result
from .eligibility_parts import (
    PartitionedEligibilityAllocation,
    PartitionedEligibilityContext,
    _preflight,
    visible_result_document,
)
from .errors import GitHubPublicationError
from .prose_publication import CommentReviewProseStore, VisibleProseReceipt
from .publication_parts import prepare_finding_prose_parts


@dataclass(frozen=True)
class PartitionedPublicationPlan:
    """Authenticated host/result expectations, never learned from new manifests."""

    binding: Mapping[str, object]
    result_digest: str
    activation_generation: int
    visible_instances: tuple[str, ...]
    allocation: PartitionedEligibilityAllocation
    budget: EvidenceReadBudget

    def __post_init__(self) -> None:
        binding = validate_binding(dict(self.binding))
        if (
            binding["purpose"] != "human-inventory"
            or not str(binding["producer"]).startswith("github-bot:")
            or type(self.activation_generation) is not int
            or not 0 <= self.activation_generation <= 2_147_483_647
            or not isinstance(self.result_digest, str)
            or len(self.result_digest) != 64
            or any(char not in "0123456789abcdef" for char in self.result_digest)
            or not isinstance(self.visible_instances, tuple)
            or len(self.visible_instances) > 250
            or len(set(self.visible_instances)) != len(self.visible_instances)
            or any(
                not isinstance(item, str)
                or len(item) != 64
                or any(char not in "0123456789abcdef" for char in item)
                for item in self.visible_instances
            )
            or not isinstance(self.allocation, PartitionedEligibilityAllocation)
            or not isinstance(self.budget, EvidenceReadBudget)
        ):
            raise ReviewInputError("partition publication host plan is invalid")
        object.__setattr__(self, "binding", MappingProxyType(binding))


@dataclass(frozen=True)
class PartitionedRootActivation:
    plan: PartitionedPublicationPlan
    body: str
    document_bytes: bytes


class PartitionedPublicationHost(Protocol):
    plan: PartitionedPublicationPlan

    def fence(self) -> None:
        """Authenticate original source/operation and current serialized generation."""
        ...

    def stage(
        self, document: object, *, binding: Mapping[str, object], item_count: int
    ) -> dict[str, object]:
        """Stage/read back through A, charging the plan's original budget once."""
        ...

    def read(
        self,
        manifest: dict[str, object],
        *,
        expected_binding: Mapping[str, object],
        budget: EvidenceReadBudget,
    ) -> object: ...

    def activate(
        self, request: PartitionedRootActivation
    ) -> PartitionedEligibilityContext:
        """Conditionally activate/read back, then return actual owned-root context.

        Bind every expected field to independent retained host/result inputs.
        Only the actual authenticated numeric POST/readback ID supplies root ID.
        Preserve unknown writes/charges; never mint another attempt on retry.
        """
        ...


@dataclass(frozen=True)
class PartitionedPublicationReceipt:
    eligibility: ReviewApprovalEligibility
    context: PartitionedEligibilityContext
    body: str
    diagnostic: str | None


def _projected_manifest(document, binding, count, *, parts=None):
    # Inventory compressed content is already exact. Receipt numeric IDs are
    # not yet known, so use the uncompressed bound plus one framing chunk.
    raw = canonical_bytes(document)
    deflate_bound = (
        len(raw) + (len(raw) >> 12) + (len(raw) >> 14) + (len(raw) >> 25) + 13
    )
    count_parts = parts or ((deflate_bound + 23_499) // 23_500)
    return {
        "encoding": "partitioned-json-v1",
        "binding": dict(binding),
        "item_count": count,
        "decoded_bytes": len(raw),
        "sha256": "f" * 64,
        "encoded_bytes": count_parts * 32768,
        "parts": [
            {"storage_id": str(i).zfill(128), "bytes": 32768, "sha256": "f" * 64}
            for i in range(count_parts)
        ],
    }


def publish_partitioned(
    publisher,
    *,
    token: str,
    host: PartitionedPublicationHost,
    result: ReviewResult,
    diff: str,
    configuration_context: Mapping[str, object],
    convergence_policy: ReviewConvergencePolicy,
    app_slug: str,
    base_branch: str = "main",
    check_token: str | None = None,
    auto_approve: bool = True,
    reviews_policy: str = "auto-approve",
) -> PartitionedPublicationReceipt:
    # Imports stay at this adapter boundary to avoid a publisher/codec cycle.
    from .checks import check_outcome_for_result
    from .human_assessment import FeedbackBudgetHttp
    from .publication import (
        REVIEW_POLICY_MODES,
        ReviewApprovalFinalizer,
        ReviewPublisher,
        restrict_result_qualification,
        review_result_digest,
    )
    from .qualification import TrustedOpenRouterQualification

    plan = getattr(host, "plan", None)
    if not isinstance(plan, PartitionedPublicationPlan) or any(
        not callable(getattr(host, name, None))
        for name in ("fence", "stage", "read", "activate")
    ):
        raise ReviewInputError(
            "explicit trusted partition publication host is required"
        )
    if (
        not isinstance(convergence_policy, ReviewConvergencePolicy)
        or not isinstance(auto_approve, bool)
        or reviews_policy not in REVIEW_POLICY_MODES
    ):
        raise ReviewInputError("partition publication operator policy is invalid")
    if convergence_policy.mode == "legacy":
        raise ReviewInputError(
            "partition writer legacy policy capability is unsupported"
        )
    effective_policy = (
        reviews_policy
        if convergence_policy.automatic_github_review_events
        else "advisory"
    )
    binding, budget = dict(plan.binding), plan.budget
    if binding["policy_digest"] != convergence_policy.digest() or binding[
        "configuration_digest"
    ] != ReviewTransaction.compute_configuration_digest(configuration_context):
        raise ReviewInputError("partition publication policy/configuration changed")
    prepared = publisher.prepare(
        result=result,
        diff=diff,
        head_sha=binding["head_sha"],
        convergence_policy=convergence_policy,
    )
    result = prepared.result
    if "<!-- reviewsensei:" in result.summary:
        raise ReviewInputError(
            "partition publication summary contains a reserved marker"
        )
    instances = tuple(
        sorted(finding_instance_fingerprint(item) for item in result.comments)
    )
    if plan.result_digest != review_result_digest(result) or set(instances) != set(
        plan.visible_instances
    ):
        raise ReviewInputError("partition publication validated result changed")
    qualification = restrict_result_qualification(result, "not-required")
    authority = publisher.finalizer.qualification_authority
    qualification_binding = (
        authority.issue_binding(
            result=result,
            configuration_context=configuration_context,
            publication_mode=convergence_policy.mode,
            repository=binding["repository"],
            pull_request=binding["pull_request"],
            base_sha=binding["base_sha"],
            head_sha=binding["head_sha"],
        )
        if type(authority) is TrustedOpenRouterQualification
        else None
    )
    if qualification_binding is not None:
        qualification = "qualified"
    eligibility = publisher._bind_qualification(
        approval_eligibility_from_result(
            result,
            head_sha=binding["head_sha"],
            base_sha=binding["base_sha"],
            enabled=auto_approve
            and convergence_policy.automatic_github_review_events
            and effective_policy == "auto-approve",
            app_authored=False,
            qualification=qualification,
            check_published=False,
        ),
        qualification_binding,
    )
    inventory = eligibility.human_review
    if inventory is None or not inventory.pending:
        raise ReviewInputError(
            "partition writer requires pending complete human inventory"
        )
    # This finite profile cannot move blocking findings into untracked prose or
    # infer thread resolution. A separate writer must qualify that capability.
    if any(item.blocks_approval for item in result.comments):
        raise ReviewInputError(
            "partition writer blocking-finding capability is unsupported"
        )
    prose = prepare_finding_prose_parts(result.comments, head_sha=binding["head_sha"])
    producer_id = int(str(binding["producer"]).split(":", 1)[1])
    inventory_document = inventory_result_document(
        inventory, result_digest=plan.result_digest
    )
    projected_receipts = tuple(
        VisibleProseReceipt(
            item.index,
            2**63 - 1 - item.index,
            binding["head_sha"],
            producer_id,
            item.sha256,
            item.byte_length,
            item.instances,
            item.paths,
        )
        for item in prose
    )
    projected_visible = visible_result_document(
        projected_receipts,
        result_digest=plan.result_digest,
        inventory_digest=inventory.inventory_digest,
        expected_instances=plan.visible_instances,
    )
    projected = dict(
        schema_version="4",
        head_sha=binding["head_sha"],
        result_digest=plan.result_digest,
        facts=eligibility.facts.to_dict(),
        immutable_generation=binding["generation"],
        activation_generation=plan.activation_generation,
        human_inventory=_projected_manifest(
            inventory_document,
            binding,
            len(inventory.findings),
            parts=len(
                partition_evidence(
                    inventory_document,
                    binding=binding,
                    item_count=len(inventory.findings),
                )
            ),
        ),
        visible_prose=_projected_manifest(
            projected_visible, dict(binding, purpose="evidence"), len(instances)
        ),
        human_resolution=HumanInventoryResolution.from_inventory(inventory).to_dict(),
    )
    # Reject full future growth before the first read or remote mutation. These
    # maximum-width storage IDs are sizing only, never retained reader context.
    _preflight(projected, context=plan.allocation, inventory=inventory)

    def navigation(values):
        return (
            "Finding explanations: "
            + ", ".join(
                f"[part {item.index + 1}](https://github.com/{binding['repository']}/pull/{binding['pull_request']}#pullrequestreview-{item.review_id})"
                for item in values
            )
            + "\n\n"
        )

    summary = f"{result.summary}\n\n{len(inventory.pending)} findings require human assessment; approval is withheld.\n\n"
    check_warning = (
        "The ReviewSensei check could not be updated; enforcement is unavailable.\n\n"
    )
    if (
        len((summary + navigation(projected_receipts) + check_warning).encode())
        > plan.allocation.framing_bytes
    ):
        raise ReviewInputError(
            "partition publication actual framing exceeds allocation"
        )
    store = CommentReviewProseStore(
        http=publisher.http,
        token=token,
        repository=binding["repository"],
        pull_request=binding["pull_request"],
        producer_id=producer_id,
        app_slug=app_slug,
        budget=budget,
    )

    def live_pr():
        budget.consume()
        preflight = publisher._preflight_pr(
            token=token,
            repository=binding["repository"],
            repository_id=binding["repository_id"],
            pull_request=binding["pull_request"],
            head_sha=binding["head_sha"],
            base_branch=base_branch,
            base_sha=binding["base_sha"],
            app_slug=app_slug,
        )
        budget.check()
        if preflight.result is not None or preflight.app_authored:
            raise ReviewInputError("partition publication current PR changed")

    host.fence()
    live_pr()
    checked_publisher = ReviewPublisher(
        http=FeedbackBudgetHttp(http=publisher.http, before_read=budget.consume),
        qualification_authority=authority,
    )
    check_arguments = dict(
        check_token=check_token,
        repository=binding["repository"],
        head_sha=binding["head_sha"],
        app_slug=app_slug,
    )
    check_diagnostic = checked_publisher._check_state_or_diagnostic(
        **check_arguments, outcome=None
    )
    if check_diagnostic is None:
        check_diagnostic = checked_publisher._check_state_or_diagnostic(
            **check_arguments,
            outcome=check_outcome_for_result(
                result,
                policy=effective_policy,
                required_fixes=False,
                required_decisions=True,
            ),
        )
    eligibility = replace(
        eligibility,
        facts=replace(eligibility.facts, check_published=check_diagnostic is None),
    )
    human_manifest = host.stage(
        inventory_document,
        binding=binding,
        item_count=len(inventory.findings),
    )
    receipts = store.stage(prose, head_sha=binding["head_sha"])
    visible_document = visible_result_document(
        receipts,
        result_digest=plan.result_digest,
        inventory_digest=inventory.inventory_digest,
        expected_instances=plan.visible_instances,
    )
    visible_manifest = host.stage(
        visible_document,
        binding=dict(binding, purpose="evidence"),
        item_count=len(plan.visible_instances),
    )
    for manifest, expected, submitted, count in (
        (human_manifest, binding, inventory_document, len(inventory.findings)),
        (
            visible_manifest,
            dict(binding, purpose="evidence"),
            visible_document,
            len(plan.visible_instances),
        ),
    ):
        checked = validate_manifest(manifest)
        wire = canonical_bytes(submitted)
        if (
            checked["binding"] != expected
            or checked["item_count"] != count
            or checked["decoded_bytes"] != len(wire)
            or checked["sha256"] != hashlib.sha256(wire).hexdigest()
        ):
            raise ReviewInputError(
                "partition publication staged manifest content or binding changed"
            )
    # Static full-growth sizing requires no fabricated numeric root identity.
    document = dict(
        schema_version="4",
        head_sha=binding["head_sha"],
        result_digest=plan.result_digest,
        facts=eligibility.facts.to_dict(),
        immutable_generation=binding["generation"],
        activation_generation=plan.activation_generation,
        human_inventory=human_manifest,
        visible_prose=visible_manifest,
        human_resolution=HumanInventoryResolution.from_inventory(inventory).to_dict(),
    )
    _preflight(document, context=plan.allocation, inventory=inventory)
    prefix = summary + navigation(receipts)
    if check_diagnostic is not None:
        prefix += check_warning
    if len(prefix.encode()) > plan.allocation.framing_bytes:
        raise ReviewInputError(
            "partition publication actual framing exceeds allocation"
        )
    body = (
        prefix
        + "<!-- reviewsensei:eligibility:v1 "
        + base64.urlsafe_b64encode(canonical_bytes(document)).decode()
        + " -->"
    )
    if (
        len(body.encode()) + plan.allocation.future_lifecycle_bytes
        > plan.allocation.max_root_bytes
    ):
        raise ReviewInputError("partition publication complete body exceeds allocation")
    host.fence()
    live_pr()
    context = host.activate(
        PartitionedRootActivation(plan, body, canonical_bytes(document))
    )
    if (
        not isinstance(context, PartitionedEligibilityContext)
        or dict(context.binding) != binding
        or context.result_digest != plan.result_digest
        or context.activation_generation != plan.activation_generation
        or context.visible_instances != plan.visible_instances
        or context.budget is not budget
        or (
            context.max_root_bytes,
            context.framing_bytes,
            context.future_lifecycle_bytes,
        )
        != (
            plan.allocation.max_root_bytes,
            plan.allocation.framing_bytes,
            plan.allocation.future_lifecycle_bytes,
        )
    ):
        raise ReviewInputError("partition publication owned context changed")
    # The publisher independently GETs the actual owned root; POST response or
    # caller metadata alone cannot activate the reader context.
    path = publisher.http.repository_path(
        binding["repository"],
        f"/pulls/{binding['pull_request']}/reviews/{context.owned_root_id}",
    )
    status, root = publisher.http.request(
        "GET", path, token=token, timeout_seconds=budget.consume()
    )
    budget.check()
    user = root.get("user") if isinstance(root, dict) else None
    if (
        status != 200
        or not isinstance(root, dict)
        or type(root.get("id")) is not int
        or root.get("id") != context.owned_root_id
        or root.get("body") != body
        or root.get("commit_id") != binding["head_sha"]
        or root.get("state") != "COMMENTED"
        or root.get("pull_request_url") != store.pull_request_url
        or not isinstance(user, dict)
        or type(user.get("id")) is not int
        or user.get("id") != producer_id
        or user.get("login") != app_slug
        or user.get("type") != "Bot"
    ):
        raise GitHubPublicationError("partition publication owned root readback failed")
    finalizer = ReviewApprovalFinalizer(
        http=publisher.http, evidence_budget=budget, qualification_authority=authority
    )
    latest = finalizer.load_eligibility(
        token=token,
        repository=binding["repository"],
        pull_request=binding["pull_request"],
        head_sha=binding["head_sha"],
        app_slug=app_slug,
        require_valid=True,
        partition_reader=host.read,
        expected_context=context,
        visible_reader=lambda values, *, budget: store.revalidate(values),
    )
    if latest is None or latest.to_dict() != document:
        raise ReviewInputError("partition publication latest root changed")
    live_pr()
    return PartitionedPublicationReceipt(
        latest, context, body, check_diagnostic or "approval_withheld"
    )
