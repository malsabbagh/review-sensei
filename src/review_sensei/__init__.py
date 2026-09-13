"""Provider-neutral AI code review primitives for ReviewSensei."""

from .concurrency import ConcurrencyGroup, ReviewConcurrencyPlan
from .context import (
    ContextSnapshot,
    FindingLifecycle,
    RepositoryContextStore,
    ReviewContextCache,
    ReviewContextCacheKey,
    ReviewContextSelection,
    SourceContextExcerpt,
    SourceContextSelection,
    SymbolAwareContextSelector,
    build_review_context_selection,
    reconcile_finding_lifecycle,
    stable_finding_fingerprint,
)
from .conversation import ConversationService
from .learnings import (
    LearningDiagnostic,
    LearningFeedback,
    LearningStore,
    load_repository_learnings,
)
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
from .evaluation import PromotionRecord, validate_promotion_record
from .outcomes import RecoveryArtifact, ResourceBudget, RunOutcome
from .release_manifest import Artifact, CompatibilityManifest, validate_compatibility_manifest
from .verifier import CandidateFinding, EvidenceReference, VerificationResult, verify_candidate, verify_candidates

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
    "LearningDiagnostic",
    "LearningFeedback",
    "load_repository_learnings",
    "ProviderRequest",
    "ProviderResponse",
    "RepositoryContextStore",
    "ContextSnapshot",
    "SourceContextExcerpt",
    "SourceContextSelection",
    "SymbolAwareContextSelector",
    "ReviewContextCache",
    "ReviewContextCacheKey",
    "FindingLifecycle",
    "stable_finding_fingerprint",
    "reconcile_finding_lifecycle",
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
    "PromotionRecord",
    "validate_promotion_record",
    "RecoveryArtifact",
    "ResourceBudget",
    "RunOutcome",
    "Artifact",
    "CompatibilityManifest",
    "validate_compatibility_manifest",
    "CandidateFinding",
    "EvidenceReference",
    "VerificationResult",
    "verify_candidate",
    "verify_candidates",
]
