from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Sequence

from .concurrency import ReviewConcurrencyPlan
from .diff import analyze_diff
from .errors import ReviewFormatError, ReviewInputError, ReviewSenseiError
from .models import (
    LearningProposal,
    ProviderRequest,
    ReviewComment,
    ReviewRequest,
    ReviewResult,
)
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


class ReviewService:
    """Run and validate a review independently of GitHub and model vendors."""

    def __init__(
        self,
        provider: ReviewProvider,
        *,
        enforce_locations: bool = True,
        stages: Sequence[Stage] | None = None,
    ) -> None:
        self.provider = provider
        self.enforce_locations = enforce_locations
        self.stages = tuple(stages) if stages is not None else DEFAULT_STAGES
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
            prompt = self._format_prompt(
                stage,
                request,
                active_categories=active_categories,
            )
            try:
                provider_request = ProviderRequest(
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
            try:
                response = self.provider.complete(provider_request)
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
                raise ReviewFormatError("provider response did not contain review text")
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
            last_provider = response.provider
            if response.model:
                last_model = response.model

            payload = self._decode_json(response.text)

            if "summary" in stage.outputs:
                summary = payload.get("summary")
                if not isinstance(summary, str) or not summary.strip():
                    msg = "provider response must contain a non-empty summary"
                    if len(self.stages) > 1 or stage != DEFAULT_STAGES[0]:
                        msg = f"[{stage.name}] {msg}"
                    raise ReviewFormatError(msg)
                if accumulated_summary:
                    accumulated_summary += "\n\n" + summary.strip()
                else:
                    accumulated_summary = summary.strip()

            if "comments" in stage.outputs:
                comments = payload.get("comments", [])
                if not isinstance(comments, list):
                    msg = "provider response comments must be an array"
                    if len(self.stages) > 1 or stage != DEFAULT_STAGES[0]:
                        msg = f"[{stage.name}] {msg}"
                    raise ReviewFormatError(msg)
                for index, value in enumerate(comments):
                    accumulated_comments.append(
                        self._validate_comment(
                            value,
                            index=index,
                            changed_lines=changed_lines,
                            stage=stage,
                            allowed_category_ids={
                                category.id for category in active_categories
                            },
                        )
                    )

            if "learning_proposals" in stage.outputs:
                learning_values = payload.get("learning_proposals", [])
                if not isinstance(learning_values, list):
                    msg = "provider response learning_proposals must be an array"
                    if len(self.stages) > 1 or stage != DEFAULT_STAGES[0]:
                        msg = f"[{stage.name}] {msg}"
                    raise ReviewFormatError(msg)
                if request.propose_learnings:
                    for index, value in enumerate(learning_values):
                        try:
                            accumulated_proposals.append(
                                LearningProposal.from_dict(value)
                            )
                        except (KeyError, ReviewInputError, TypeError) as exc:
                            msg = f"learning proposal {index} has an invalid shape"
                            if len(self.stages) > 1 or stage != DEFAULT_STAGES[0]:
                                msg = f"[{stage.name}] {msg}"
                            raise ReviewFormatError(msg) from exc

            # Validate the aggregate after every stage.  This both enforces the
            # publisher contract for direct construction and prevents another
            # provider call once a bounded output is already full.  Reassigning
            # comments from the checkpoint carries stable first-wins
            # deduplication into the next stage.
            try:
                checkpoint = ReviewResult(
                    summary=accumulated_summary or "Review complete.",
                    comments=tuple(accumulated_comments),
                    provider=last_provider,
                    model=last_model or request.model,
                    learning_proposals=tuple(accumulated_proposals),
                    limits=request.limits,
                )
            except ReviewInputError as exc:
                raise ReviewFormatError(
                    "provider output exceeds the configured result limits"
                ) from exc
            accumulated_comments = list(checkpoint.comments)

        final_summary = accumulated_summary or "Review complete."
        try:
            return ReviewResult(
                summary=final_summary,
                comments=tuple(accumulated_comments),
                provider=last_provider,
                model=last_model or request.model,
                learning_proposals=tuple(accumulated_proposals),
                limits=request.limits,
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
        review_context = json.dumps(
            [
                context.to_prompt_dict()
                for context in request.lens_contexts
                if context.category_id in active_ids
            ],
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
        changed_lines: dict[str, frozenset[int]],
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

        if self.enforce_locations and comment.line not in changed_lines.get(
            comment.path, frozenset()
        ):
            raise ReviewFormatError(
                f"comment {index} targets a line that is not added or modified in the diff"
            )
        return comment
