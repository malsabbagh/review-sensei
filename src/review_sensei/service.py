from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .concurrency import ReviewConcurrencyPlan
from .diff import analyze_diff
from .errors import (
    ProviderError,
    ReviewFormatError,
    ReviewInputError,
    ReviewSenseiError,
)
from .models import (
    LearningProposal,
    ProviderRequest,
    ProviderResponse,
    ReviewComment,
    ReviewRequest,
    ReviewResult,
)
from .outcomes import ResourceBudget
from .providers.base import ReviewProvider
from .stages import (
    ReviewCategory,
    Stage,
    load_review_categories_from_dir,
    load_stages_from_dir,
)
from .validation import validate_bounded_text

DEFAULT_CATEGORY_CATALOG = load_review_categories_from_dir(
    Path(__file__).parent / "default_categories"
)
DEFAULT_STAGES = tuple(
    load_stages_from_dir(
        Path(__file__).parent / "default_stages",
        category_catalog=DEFAULT_CATEGORY_CATALOG,
    )
)
_PROMPT_PLACEHOLDER = re.compile(r"\{([a-z][a-z0-9_]*)\}")
_MAX_PROVIDER_OUTPUT_ATTEMPTS = 2
_LOGGER = logging.getLogger(__name__)
_PROVIDER_OUTPUT_CORRECTION = """

<review-output-correction>
Your previous response failed validation. Return a fresh response matching the
requested JSON structure. Every inline comment must use the exact
repository-relative path and new-file line number of an added or modified line
in the diff. Do not target context or deleted lines. If a finding cannot be
attached to a changed line, keep it in the summary and omit that inline comment.
For each actionable inline finding, classify the independent dimensions: whether
it is merge-blocking (`blocking`: true or false), severity (`critical`, `high`,
`medium`, or `low`), fix effort (`trivial`, `small`, `moderate`, `large`, or
`unknown`), and the configured category id as the lens source. A non-blocking
finding is a follow-up and does not independently prevent approval. Do not
combine these dimensions into one priority value.
</review-output-correction>
""".strip()


@dataclass(frozen=True)
class _ValidatedStageOutput:
    summary: str | None
    comments: tuple[ReviewComment, ...]
    proposals: tuple[LearningProposal, ...]
    omitted_inline_comments: int = 0


class ReviewService:
    """Run and validate a review independently of GitHub and model vendors.

    Instances are reusable, but ``review()`` is not thread-safe: concurrent
    calls would race on the per-run ``_provider_calls`` budget counter.
    """

    def __init__(
        self,
        provider: ReviewProvider,
        *,
        enforce_locations: bool = True,
        stages: Sequence[Stage] | None = None,
        stage_providers: Mapping[str, ReviewProvider] | None = None,
        budget: ResourceBudget | None = None,
    ) -> None:
        self.provider = provider
        self.enforce_locations = enforce_locations
        self.stages = tuple(stages) if stages is not None else DEFAULT_STAGES
        self.stage_providers = dict(stage_providers or {})
        self.budget = budget if budget is not None else ResourceBudget.create()
        self._provider_calls = 0
        if not self.stages:
            raise ReviewInputError("review must contain at least one configured stage")
        if any(not isinstance(stage, Stage) for stage in self.stages):
            raise ReviewInputError("review stages must contain only Stage values")
        stage_names = [stage.name.strip() for stage in self.stages]
        if len(stage_names) != len(set(stage_names)):
            raise ReviewInputError("review stage names must be unique")
        category_definitions: dict[str, ReviewCategory] = {}
        for stage in self.stages:
            for category in stage.categories:
                existing = category_definitions.setdefault(category.id, category)
                if existing != category:
                    raise ReviewInputError(
                        "review category ids must have consistent definitions across stages"
                    )
        self.review_categories = tuple(category_definitions.values())
        if any(
            not isinstance(name, str) or not name.strip()
            for name in self.stage_providers
        ):
            raise ReviewInputError("stage provider names must be non-empty strings")
        unknown_stage_providers = set(self.stage_providers) - set(stage_names)
        if unknown_stage_providers:
            raise ReviewInputError("stage providers must match configured stage names")
        if not isinstance(self.budget, ResourceBudget):
            raise ReviewInputError("review budget must be a ResourceBudget value")

    def _provider_for_stage(self, stage: Stage) -> ReviewProvider:
        return self.stage_providers.get(stage.name, self.provider)

    def _stage_attempt_limit(self) -> int:
        return max(
            1,
            min(_MAX_PROVIDER_OUTPUT_ATTEMPTS, self.budget.max_retry_attempts + 1),
        )

    def _complete(
        self, provider: ReviewProvider, provider_request: ProviderRequest
    ) -> ProviderResponse:
        """Call a provider and count only successful completions toward the budget.

        Failed attempts, including transient transport errors, do not consume
        ``max_provider_calls``; only responses returned from ``complete`` do.
        """
        if self._provider_calls >= self.budget.max_provider_calls:
            raise ProviderError("resource budget exhausted")
        response = provider.complete(provider_request)
        self._provider_calls += 1
        return response

    @staticmethod
    def _source_context_coverage(request: ReviewRequest) -> object | None:
        source = request.source_context
        if source is None:
            return None
        from .context import SourceContextSelection

        if not isinstance(source, SourceContextSelection):
            return None
        return source.coverage(untrusted_head_sha=request.untrusted_head_sha)

    def concurrency_plan(self, request: ReviewRequest) -> ReviewConcurrencyPlan:
        """Return the host-enforced concurrency policy for ``request``."""

        return ReviewConcurrencyPlan.for_request(
            request, provider_name=self.provider.name
        )

    def review(self, request: ReviewRequest) -> ReviewResult:
        # Multi-stage execution
        accumulated_summary: str = ""
        accumulated_comments: list[ReviewComment] = []
        accumulated_proposals: list[LearningProposal] = []
        last_provider: str = self.provider.name
        last_model: str | None = request.model
        skipped_stages = 0
        omitted_inline_comments = 0
        executed_comment_stage = False
        self._provider_calls = 0

        # Preflight the complete diff exactly once, before the first provider
        # construction/call.  The parser owns byte, line, file, hunk, marker,
        # and canonical path limits.
        try:
            analysis = analyze_diff(request.diff, limits=request.limits)
        except ReviewInputError as exc:
            raise ReviewInputError("diff failed bounded preflight") from exc
        changed_lines = analysis.changed_lines
        configured_category_ids = {category.id for category in self.review_categories}
        if request.active_category_ids is not None and not set(
            request.active_category_ids
        ).issubset(configured_category_ids):
            raise ReviewInputError(
                "active review category ids must be declared by a configured stage"
            )

        for stage in self.stages:
            active_categories = (
                tuple(
                    category
                    for category in stage.categories
                    if category.id in request.active_category_ids
                )
                if request.active_category_ids is not None
                else stage.categories
            )
            # A stage whose configured lenses do not apply to the changed paths
            # has no useful provider work.  Stages without categories are
            # intentionally independent and continue to run (for example an
            # overall summary stage).
            if stage.categories and not active_categories:
                skipped_stages += 1
                continue
            executed_comment_stage = (
                executed_comment_stage or "comments" in stage.outputs
            )
            prompt = self._format_prompt(
                stage,
                request,
                active_categories=active_categories,
            )
            provider_request = self._provider_request(prompt, request=request)
            stage_summary: str | None = None
            stage_comments: tuple[ReviewComment, ...] = ()
            stage_proposals: tuple[LearningProposal, ...] = ()
            response_provider = last_provider
            response_model = last_model
            stage_provider = self._provider_for_stage(stage)
            max_attempts = self._stage_attempt_limit()
            for attempt in range(max_attempts):
                try:
                    response = self._complete(stage_provider, provider_request)
                except ReviewFormatError:
                    raise
                except ReviewInputError as exc:
                    raise ReviewFormatError("provider response failed") from exc
                except ReviewSenseiError:
                    raise
                except Exception as exc:
                    # Provider adapters should sanitize their own failures.  Keep
                    # this fallback static so a third-party adapter cannot echo a
                    # prompt, response body, or credential through the core.
                    raise ReviewFormatError("provider request failed") from exc
                if not hasattr(response, "text") or not isinstance(response.text, str):
                    raise ReviewFormatError(
                        "provider response did not contain review text"
                    )
                try:
                    validate_bounded_text(
                        response.text,
                        request.limits.max_provider_response_bytes,
                        label="provider response",
                        allow_empty=False,
                    )
                except ReviewInputError as exc:
                    raise ReviewFormatError(
                        "provider response exceeds the configured limit"
                    ) from exc
                try:
                    stage_output = self._validated_stage_output(
                        response.text,
                        stage=stage,
                        changed_lines=changed_lines,
                        active_categories=active_categories,
                        propose_learnings=request.propose_learnings,
                    )
                    stage_summary = stage_output.summary
                    stage_comments = stage_output.comments
                    stage_proposals = stage_output.proposals
                    omitted_inline_comments += stage_output.omitted_inline_comments
                except ReviewFormatError:
                    if attempt + 1 >= max_attempts:
                        raise
                    provider_request = self._provider_request(
                        f"{prompt}\n\n{_PROVIDER_OUTPUT_CORRECTION}",
                        request=request,
                    )
                    continue
                response_provider = response.provider
                if response.model:
                    response_model = response.model
                break

            candidate_summary = accumulated_summary
            if stage_summary is not None:
                candidate_summary = (
                    f"{candidate_summary}\n\n{stage_summary}"
                    if candidate_summary
                    else stage_summary
                )
            candidate_comments = [*accumulated_comments, *stage_comments]
            candidate_proposals = [*accumulated_proposals, *stage_proposals]

            # Validate the aggregate after every stage.  This both enforces the
            # publisher contract for direct construction and prevents another
            # provider call once a bounded output is already full.  Reassigning
            # comments from the checkpoint carries stable first-wins
            # deduplication into the next stage.
            try:
                checkpoint_status = (
                    "partial"
                    if skipped_stages or omitted_inline_comments
                    else "summary-only"
                    if not executed_comment_stage
                    else "complete"
                )
                checkpoint = ReviewResult(
                    summary=candidate_summary or "Review complete.",
                    comments=tuple(candidate_comments),
                    provider=response_provider,
                    model=response_model or request.model,
                    learning_proposals=tuple(candidate_proposals),
                    limits=request.limits,
                    review_status=checkpoint_status,
                    source_context_coverage=self._source_context_coverage(request),
                )
            except ReviewInputError as exc:
                raise ReviewFormatError(
                    "provider output exceeds the configured result limits"
                ) from exc
            accumulated_summary = candidate_summary
            accumulated_comments = list(checkpoint.comments)
            accumulated_proposals = candidate_proposals
            last_provider = response_provider
            last_model = response_model

        final_summary = accumulated_summary or "Review complete."
        review_status = (
            "partial"
            if skipped_stages or omitted_inline_comments
            else "summary-only"
            if not executed_comment_stage
            else "complete"
        )
        try:
            return ReviewResult(
                summary=final_summary,
                comments=tuple(accumulated_comments),
                provider=last_provider,
                model=last_model or request.model,
                learning_proposals=tuple(accumulated_proposals),
                limits=request.limits,
                review_status=review_status,
                source_context_coverage=self._source_context_coverage(request),
            )
        except ReviewInputError as exc:
            raise ReviewFormatError(
                "provider output exceeds the configured result limits"
            ) from exc

    def _format_prompt(
        self,
        stage: Stage,
        request: ReviewRequest,
        *,
        active_categories: tuple[ReviewCategory, ...],
    ) -> str:
        active_ids = {category.id for category in active_categories}
        lens_learning_ids = {
            learning.id
            for context in request.lens_contexts
            if context.category_id in active_ids
            for learning in context.learnings
        }
        general_learnings = tuple(
            learning
            for learning in request.learnings
            if learning.id not in lens_learning_ids
        )
        learning_text = (
            json.dumps(
                [learning.to_prompt_dict() for learning in general_learnings],
                ensure_ascii=False,
                indent=2,
            )
            if general_learnings
            else "No approved repository learnings were provided."
        )
        proposal_instruction = (
            "Propose only durable repository facts that should influence future reviews; "
            "return an empty learning_proposals array when there is nothing durable to record."
            if request.propose_learnings
            else "Do not propose repository learnings; return an empty learning_proposals array."
        )
        context: list[str] = []
        if request.repository:
            context.append(f"Repository: {request.repository}")
        if request.pull_request_number is not None:
            context.append(f"Pull request: #{request.pull_request_number}")
        if request.title:
            context.append(f"Pull request title: {request.title}")
        context_text = "\n".join(context) or "No repository metadata was provided."
        extra = (
            request.instructions.strip()
            if request.instructions
            else "No additional instructions were provided."
        )
        review_categories = json.dumps(
            [category.to_prompt_dict() for category in active_categories],
            ensure_ascii=False,
            indent=2,
        )
        review_context_payload: list[object] = [
            context.to_prompt_dict()
            for context in request.lens_contexts
            if context.category_id in active_ids
        ]
        if request.source_context is not None:
            from .context import SourceContextSelection

            if isinstance(request.source_context, SourceContextSelection):
                review_context_payload.append(request.source_context.to_prompt_dict())
        review_context = json.dumps(
            review_context_payload,
            ensure_ascii=False,
            indent=2,
        )
        replacements = {
            "diff": request.diff,
            "repository": request.repository or "",
            "pull_request": str(request.pull_request_number or ""),
            "title": request.title or "",
            "instructions": extra,
            "learnings": learning_text,
            "context_text": context_text,
            "proposal_instruction": proposal_instruction,
            "review_categories": review_categories,
            "review_context": review_context,
        }
        return _PROMPT_PLACEHOLDER.sub(
            lambda match: replacements[match.group(1)],
            stage.prompt_template,
        )

    @staticmethod
    def _provider_request(prompt: str, *, request: ReviewRequest) -> ProviderRequest:
        try:
            return ProviderRequest(
                prompt=prompt,
                model=request.model,
                json_mode=True,
                max_prompt_bytes=request.limits.max_prompt_bytes,
                max_response_bytes=request.limits.max_provider_response_bytes,
                limits=request.limits,
            )
        except ReviewInputError as exc:
            raise ReviewInputError(
                "review prompt exceeds the configured limit"
            ) from exc

    def _validated_stage_output(
        self,
        text: str,
        *,
        stage: Stage,
        changed_lines: dict[str, frozenset[int]],
        active_categories: tuple[ReviewCategory, ...],
        propose_learnings: bool,
    ) -> _ValidatedStageOutput:
        payload = self._decode_json(text)
        stage_summary: str | None = None
        stage_comments: list[ReviewComment] = []
        stage_proposals: list[LearningProposal] = []
        omitted_inline_comments = 0

        if "summary" in stage.outputs:
            summary = payload.get("summary")
            if not isinstance(summary, str) or not summary.strip():
                msg = "provider response must contain a non-empty summary"
                if len(self.stages) > 1 or stage != DEFAULT_STAGES[0]:
                    msg = f"[{stage.name}] {msg}"
                raise ReviewFormatError(msg)
            stage_summary = summary.strip()

        if "comments" in stage.outputs:
            comments = payload.get("comments", [])
            if not isinstance(comments, list):
                msg = "provider response comments must be an array"
                if len(self.stages) > 1 or stage != DEFAULT_STAGES[0]:
                    msg = f"[{stage.name}] {msg}"
                raise ReviewFormatError(msg)
            for index, value in enumerate(comments):
                comment = self._validate_comment(
                    value,
                    index=index,
                    stage=stage,
                    allowed_category_ids={
                        category.id for category in active_categories
                    },
                )
                if self.enforce_locations and comment.line not in changed_lines.get(
                    comment.path, frozenset()
                ):
                    # The provider has already supplied a valid, bounded comment
                    # shape, but its target is not publishable as a GitHub inline
                    # annotation. Do not make one bad coordinate discard the
                    # independently valid summary or comments. Keep the log
                    # intentionally free of provider-controlled path and body text.
                    _LOGGER.warning(
                        "review-sensei: omitted inline comment %d because its target "
                        "is not an added or modified diff line",
                        index,
                    )
                    omitted_inline_comments += 1
                    continue
                stage_comments.append(comment)

        if "learning_proposals" in stage.outputs:
            learning_values = payload.get("learning_proposals", [])
            if not isinstance(learning_values, list):
                msg = "provider response learning_proposals must be an array"
                if len(self.stages) > 1 or stage != DEFAULT_STAGES[0]:
                    msg = f"[{stage.name}] {msg}"
                raise ReviewFormatError(msg)
            if propose_learnings:
                for index, value in enumerate(learning_values):
                    try:
                        stage_proposals.append(LearningProposal.from_dict(value))
                    except (KeyError, ReviewInputError, TypeError) as exc:
                        msg = f"learning proposal {index} has an invalid shape"
                        if len(self.stages) > 1 or stage != DEFAULT_STAGES[0]:
                            msg = f"[{stage.name}] {msg}"
                        raise ReviewFormatError(msg) from exc

        return _ValidatedStageOutput(
            stage_summary,
            tuple(stage_comments),
            tuple(stage_proposals),
            omitted_inline_comments,
        )

    @staticmethod
    def _decode_json(text: str) -> dict[str, Any]:
        content = text.strip()
        if content.startswith("```"):
            lines = content.splitlines()
            lines = lines[1:] if lines and lines[0].startswith("```") else lines
            lines = lines[:-1] if lines and lines[-1].strip() == "```" else lines
            content = "\n".join(lines).strip()

        try:
            value = json.loads(content)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ReviewFormatError("provider response was not valid JSON") from exc
        if not isinstance(value, dict):
            raise ReviewFormatError("provider response must be a JSON object")
        return value

    def _validate_comment(
        self,
        value: object,
        *,
        index: int,
        stage: Stage,
        allowed_category_ids: set[str],
    ) -> ReviewComment:
        if not isinstance(value, dict):
            raise ReviewFormatError(f"comment {index} must be a JSON object")
        try:
            comment = ReviewComment(
                path=value["path"],
                line=value["line"],
                body=value["body"],
                severity=value.get("severity"),
                category=value.get("category"),
                fix_effort=value.get("fix_effort"),
                blocking=value.get("blocking"),
            )
        except (KeyError, ReviewInputError, TypeError) as exc:
            raise ReviewFormatError(f"comment {index} has an invalid shape") from exc

        if (
            comment.category is not None
            and stage.categories
            and comment.category not in allowed_category_ids
        ):
            raise ReviewFormatError(
                f"[{stage.name}] comment {index} uses a category that is not declared by the stage"
            )

        return comment
