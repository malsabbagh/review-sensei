"""Provider-neutral AI code review primitives for ReviewSensei."""

from .concurrency import ConcurrencyGroup, ReviewConcurrencyPlan
from .context import (
    RepositoryContextStore,
    ReviewContextSelection,
    build_review_context_selection,
)
from .conversation import ConversationService
from .learnings import LearningStore, load_repository_learnings
from .diagnostics import build_plan, run_doctor
from .models import (
    ConversationContext,
    ConversationFinding,
    ConversationMessage,
    ConversationReply,
    LearningEntry,
    LearningProposal,
    ProviderRequest,
    ProviderResponse,
    ReviewComment,
    ReviewDocument,
    ReviewLensContext,
    ReviewRequest,
    ReviewResult,
)
from .patches import PatchSuggestion, create_patch_suggestion
from .service import ReviewService
from .stages import (
    ContextDocumentSource,
    ReviewCategory,
    ReviewCategoryCatalog,
    Stage,
    load_review_categories_from_dir,
    load_stages_from_dir,
)
from .validation import DEFAULT_REVIEW_LIMITS, ReviewLimits
from .workflow import ReviewExecutionPlan, plan_review_execution

__all__ = [
    "ConcurrencyGroup",
    "ContextDocumentSource",
    "ConversationContext",
    "ConversationFinding",
    "ConversationMessage",
    "ConversationReply",
    "ConversationService",
    "LearningEntry",
    "LearningProposal",
    "LearningStore",
    "load_repository_learnings",
    "ProviderRequest",
    "ProviderResponse",
    "RepositoryContextStore",
    "ReviewContextSelection",
    "ReviewComment",
    "ReviewDocument",
    "ReviewLensContext",
    "ReviewLimits",
    "ReviewCategory",
    "ReviewCategoryCatalog",
    "ReviewConcurrencyPlan",
    "ReviewRequest",
    "ReviewResult",
    "ReviewService",
    "ReviewExecutionPlan",
    "DEFAULT_REVIEW_LIMITS",
    "Stage",
    "build_review_context_selection",
    "build_plan",
    "run_doctor",
    "PatchSuggestion",
    "create_patch_suggestion",
    "load_review_categories_from_dir",
    "load_stages_from_dir",
    "plan_review_execution",
]
