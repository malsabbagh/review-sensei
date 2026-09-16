from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .concurrency import ReviewConcurrencyPlan
from .context import (
    FindingLifecycle,
    IncrementalReviewPlan,
    ReviewContextCache,
    ReviewContextCacheKey,
    build_review_context_cache_key,
    cache_key_is_compatible,
    finding_lifecycle_for_comment,
    reconcile_finding_set,
)
from .diff import analyze_diff
from .errors import (
    ProviderError,
    ReviewFormatError,
    ReviewInputError,
    ReviewSenseiError,
)
from .models import (
    FindingLifecycleRecord,
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
_COVERAGE_APPENDIX = """
<review-coverage>
Coverage mode: {mode}.
Reviewed paths: {reviewed}
Related dependency context retained for cross-file and test regressions: {related}
Do not treat absence of a prior finding in this pass as proof that it is fixed.
</review-coverage>
""".strip()
MAX_COVERAGE_PATHS = 64


@dataclass(frozen=True)
class _CoverageDecision:
    mode: str
    skip_provider: bool
    reviewed_paths: tuple[str, ...]
    reviewed_paths_bound: tuple[str, ...] | None
    related_paths: tuple[str, ...]
    previous_findings: tuple[FindingLifecycle, ...]
    generation: int
    evidence_confirmed: tuple[str, ...]
    current_key: ReviewContextCacheKey | None


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
        cache: ReviewContextCache | None = None,
    ) -> None:
        self.provider = provider
        self.enforce_locations = enforce_locations
        self.stages = tuple(stages) if stages is not None else DEFAULT_STAGES
        self.stage_providers = dict(stage_providers or {})
        self.budget = budget if budget is not None else ResourceBudget.create()
        self._provider_calls = 0
        self.cache = cache
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
        """Call a provider and count every invocation toward the budget.

        Each provider call consumes one ``max_provider_calls`` slot whether or
        not the adapter returns a response, so transient transport failures
        cannot bypass the deterministic call ceiling. Structural output retries
        within a stage remain bounded separately by ``max_retry_attempts`` via
        ``_stage_attempt_limit``.
        """
        if self._provider_calls >= self.budget.max_provider_calls:
            raise ProviderError(
                "resource budget exhausted "
                f"({self._provider_calls}/{self.budget.max_provider_calls} "
                "provider calls)"
            )
        self._provider_calls += 1
        return provider.complete(provider_request)

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

    def review(
        self,
        request: ReviewRequest,
        *,
        incremental: IncrementalReviewPlan | None = None,
        profile: str = "default",
    ) -> ReviewResult:
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
        coverage = self._coverage_decision(
            request, incremental=incremental, profile=profile
        )

        if coverage.skip_provider:
            # No provider call is made, so none is charged to the budget: the
            # budget counts real provider invocations, and a skipped incremental
            # pass must not consume a call a later pass may need.  The summary
            # below is engine-authored rather than provider output, and the pass
            # carries no findings, so approval still depends entirely on the
            # finalizer's unresolved-blocking-root check.
            return self._finalize_result(
                request,
                summary="Incremental review: no changed paths since the last accepted review.",
                comments=(),
                proposals=(),
                provider=last_provider,
                model=last_model,
                review_status="complete",
                coverage=coverage,
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
                coverage_mode=coverage.mode,
                reviewed_paths=coverage.reviewed_paths,
                related_paths=coverage.related_paths,
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
            return self._finalize_result(
                request,
                summary=final_summary,
                comments=tuple(accumulated_comments),
                proposals=tuple(accumulated_proposals),
                provider=last_provider,
                model=last_model or request.model,
                review_status=review_status,
                source_context_coverage=self._source_context_coverage(request),
                coverage=coverage,
            )
        except ReviewInputError as exc:
            raise ReviewFormatError(
                "provider output exceeds the configured result limits"
            ) from exc

    def _coverage_decision(
        self,
        request: ReviewRequest,
        *,
        incremental: IncrementalReviewPlan | None,
        profile: str,
    ) -> _CoverageDecision:
        """Resolve the coverage mode and the prior findings that stay in scope.

        Prior findings are only reconciled when this run can verify that the
        caller's prior cache key is compatible with the current snapshot and
        configuration.  A ``fallback-full`` caused by incomplete related context
        or a missing changed-path list keeps those verified priors, so a full
        bounded re-review reports them as ``uncertain`` rather than dropping
        them; omission still never counts as proof of a fix.  A ``fallback-full``
        caused by an incompatible or unverifiable key discards them instead,
        because their identities cannot be tied to this snapshot.
        """

        current_key = build_review_context_cache_key(
            request,
            provider_name=self.provider.name,
            stages=self.stages,
            profile=profile,
        )
        mode = "full"
        skip_provider = False
        reviewed_paths: tuple[str, ...] | None = None
        related_paths: tuple[str, ...] = ()
        previous_findings: tuple[FindingLifecycle, ...] = ()
        generation = 0
        evidence_confirmed: tuple[str, ...] = ()
        # Stale same-PR entries are evicted on every run that can name a cache
        # identity, not only on incremental runs, so a full pass cannot leave
        # entries bound to a superseded base, model, or configuration behind for
        # a later incremental pass to consider compatible.
        if self.cache is not None and current_key is not None:
            self.cache.invalidate_incompatible(current_key)
        if incremental is not None and current_key is not None:
            generation = incremental.generation
            previous_findings = incremental.previous_findings
            evidence_confirmed = incremental.evidence_confirmed_concerns
            related_paths = incremental.related_paths
            if not cache_key_is_compatible(current_key, incremental.previous_key):
                mode = "fallback-full"
                previous_findings = ()
            elif incremental.reviewed_paths is None:
                # Incremental coverage is defined by the caller's changed-path
                # list.  Without one there is no reviewed scope to reason about,
                # so this is a full bounded review rather than an incremental
                # pass that silently treats every prior path as reviewed.
                mode = "fallback-full"
            elif not incremental.context_complete:
                mode = "fallback-full"
            else:
                mode = "incremental"
                reviewed_paths = incremental.reviewed_paths
                skip_provider = incremental.reviewed_paths == ()
        elif incremental is not None:
            # Without repository/PR/base/head identity the prior key cannot be
            # checked for compatibility, so prior findings are not trusted.
            mode = "fallback-full"
            generation = incremental.generation
            evidence_confirmed = incremental.evidence_confirmed_concerns
            related_paths = incremental.related_paths
        return _CoverageDecision(
            mode=mode,
            skip_provider=skip_provider,
            reviewed_paths=reviewed_paths or (),
            reviewed_paths_bound=reviewed_paths,
            related_paths=related_paths,
            previous_findings=previous_findings,
            generation=generation,
            evidence_confirmed=evidence_confirmed,
            current_key=current_key,
        )

    def _coverage_appendix(
        self,
        *,
        coverage_mode: str,
        reviewed_paths: tuple[str, ...],
        related_paths: tuple[str, ...],
    ) -> str:
        if coverage_mode == "full" and not related_paths:
            return ""
        reviewed = ", ".join(reviewed_paths[:MAX_COVERAGE_PATHS]) or "(current diff)"
        related = ", ".join(related_paths[:MAX_COVERAGE_PATHS]) or "(none)"
        return "\n\n" + _COVERAGE_APPENDIX.format(
            mode=coverage_mode, reviewed=reviewed, related=related
        )

    def _finalize_result(
        self,
        request: ReviewRequest,
        *,
        summary: str,
        comments: tuple[ReviewComment, ...],
        proposals: tuple[LearningProposal, ...],
        provider: str,
        model: str | None,
        review_status: str,
        coverage: _CoverageDecision,
        source_context_coverage: object | None = None,
    ) -> ReviewResult:
        current = tuple(
            finding_lifecycle_for_comment(comment, generation=coverage.generation)
            for comment in comments
        )
        review_complete = review_status == "complete"
        lifecycles = reconcile_finding_set(
            coverage.previous_findings,
            current,
            review_complete=review_complete,
            reviewed_paths=coverage.reviewed_paths_bound,
            evidence_confirmed_concerns=coverage.evidence_confirmed,
            generation=coverage.generation,
        )
        result = ReviewResult(
            summary=summary,
            comments=comments,
            provider=provider,
            model=model,
            learning_proposals=proposals,
            limits=request.limits,
            review_status=review_status,
            source_context_coverage=source_context_coverage,
            coverage_mode=coverage.mode,
            finding_lifecycles=tuple(
                FindingLifecycleRecord(item.fingerprint, item.state)
                for item in lifecycles
            ),
        )
        # ``_coverage_decision`` already evicted same-PR entries incompatible
        # with this key, so the write cannot land behind a stale entry that a
        # later incremental pass would still read as compatible.  Only a
        # validated complete pass may become the authoritative record: a partial
        # or incomplete aggregate must not let a later incremental run treat its
        # lifecycle set as verified or decide there is nothing left to review.
        if (
            self.cache is not None
            and coverage.current_key is not None
            and review_complete
        ):
            self.cache.put_if_newer(
                coverage.current_key,
                (coverage.generation, coverage.mode, len(lifecycles)),
                generation=coverage.generation,
            )
        return result

    def _format_prompt(
        self,
        stage: Stage,
        request: ReviewRequest,
        *,
        active_categories: tuple[ReviewCategory, ...],
        coverage_mode: str = "full",
        reviewed_paths: tuple[str, ...] = (),
        related_paths: tuple[str, ...] = (),
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
        ) + self._coverage_appendix(
            coverage_mode=coverage_mode,
            reviewed_paths=reviewed_paths,
            related_paths=related_paths,
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
                symbol=value.get("symbol"),
                defect_kind=value.get("defect_kind"),
                evidence_id=value.get("evidence_id"),
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
