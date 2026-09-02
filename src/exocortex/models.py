"""Domain models for notes, sources, and search results."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, ValidationInfo, field_validator

NoteType = Literal[
    "project",
    "task",
    "decision",
    "pattern",
    "incident",
    "command",
    "repository",
    "system",
    "workflow",
]

EvidenceStatus = Literal[
    "confirmed_success",
    "confirmed_failure",
    "decision",
    "proposal",
    "investigation",
    "unknown",
]

ClaimType = Literal[
    "user_decision",
    "tool_observation",
    "assistant_suggestion",
    "brain_derived",
]
ClaimPolarity = Literal["affirmed", "negated"]
EvidencePrecision = Literal["exact", "source"]
RecommendationState = Literal[
    "active",
    "penalized",
    "quarantined",
    "superseded",
]
RecommendationLevel = Literal["deprioritize", "confirm", "auto_apply"]
FeedbackOutcome = Literal["approved", "rejected", "executed_success", "failed"]
ClaimSupport = Literal["direct", "context_only", "contradictory", "unknown"]
AnswerMode = Literal["conservative", "exploration"]
QueryMode = Literal["confirmation", "procedure", "exploratory", "general"]
VerificationStatus = Literal["verified", "candidate"]
SearchFeedbackRelevance = Literal["relevant", "partially_relevant", "irrelevant"]
SearchFeedbackTag = Literal[
    "scope_mismatch",
    "too_specific",
    "overgeneralized",
    "wrong_provider",
    "wrong_runtime",
    "useful_example",
    "useful_pattern",
]
ExtractionStatus = Literal["extracted", "fallback", "manual", "unknown"]
KnowledgeLevel = Literal["pattern", "adapter", "example", "decision"]
ScopeMatch = Literal["explicit", "inferred", "none", "mismatch"]
ResponseStatus = Literal[
    "ok",
    "abstained",
    "degraded",
    "incomplete",
    "not_found",
    "stored",
]


class SourceReference(BaseModel):
    """Reference to a sanitized source without retaining raw content."""

    id: str
    locator: str
    content_hash: str
    occurred_on: date | None = None
    session_id: str | None = None
    segment_id: str | None = None
    event_start: int | None = None
    event_end: int | None = None


class EvidenceSpan(BaseModel):
    """Sanitized evidence supporting one extracted claim."""

    source_id: str
    session_id: str | None = None
    segment_id: str | None = None
    event_start: int | None = None
    event_end: int | None = None
    fragment: str = ""
    fragment_hash: str = ""
    precision: EvidencePrecision = "source"


class Claim(BaseModel):
    """One normalized assertion with inspectable source evidence."""

    id: str
    text: str
    claim_key: str
    polarity: ClaimPolarity = "affirmed"
    claim_type: ClaimType = "assistant_suggestion"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    evidence: list[EvidenceSpan] = Field(default_factory=list)


class KnowledgeScope(BaseModel):
    """Optional structured scope for a reusable pattern or concrete adapter."""

    organization: str = ""
    provider: str = ""
    runtime: str = ""
    region: str = ""
    auth: str = ""
    environment: str = ""
    project: str = ""
    repository: str = ""
    role: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class NoteMetadata(BaseModel):
    """Validated frontmatter stored in a canonical note."""

    schema_version: int = Field(default=1, ge=1)
    id: UUID = Field(default_factory=uuid4)
    type: NoteType
    title: str
    space_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    ingested_at: datetime | None = None
    source_refs: list[SourceReference] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    usage_count: int = Field(default=0, ge=0)
    success_count: int = Field(default=0, ge=0)
    last_feedback_at: datetime | None = None
    last_feedback_notes: str | None = None
    managed_fields: list[str] = Field(
        default_factory=lambda: ["summary", "facts", "links"]
    )
    links: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    manual_labels: list[str] = Field(default_factory=list)
    evidence_status: EvidenceStatus = "unknown"
    superseded_by: str | None = None
    recommendation_state: RecommendationState = "active"
    prompt_version: str = "v1"
    model_version: str = "unknown"
    extraction_status: ExtractionStatus = "unknown"
    embedding_model: str | None = None
    claims: list[Claim] = Field(default_factory=list)
    actions: list[ActionSignature] = Field(default_factory=list)
    workflow_steps: list[dict[str, Any]] = Field(default_factory=list)
    knowledge_level: KnowledgeLevel = "example"
    pattern_key: str = ""
    scope: KnowledgeScope = Field(default_factory=KnowledgeScope)


class VaultNote(BaseModel):
    """Canonical note with validated metadata and Markdown content."""

    metadata: NoteMetadata
    content: str
    path: str


class SanitizationFinding(BaseModel):
    """A deterministic redaction applied before a model request."""

    kind: str
    count: int = Field(ge=1)


class SanitizedContent(BaseModel):
    """Sanitized payload and non-sensitive audit facts."""

    text: str
    source_hash: str
    findings: list[SanitizationFinding] = Field(default_factory=list)


class ActionSignature(BaseModel):
    """Normalized action detected in an experience or workflow."""

    action_key: str
    canonical_action_key: str = ""
    subjects: list[str] = Field(default_factory=list)
    objects: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    route: str = ""
    outcome: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class OperationalContext(BaseModel):
    """Optional work context used only to rank workflow recommendations."""

    role: str = ""
    domains: list[str] = Field(default_factory=list)
    common_tasks: list[str] = Field(default_factory=list)
    preferred_tools: list[str] = Field(default_factory=list)
    low_priority: list[str] = Field(default_factory=list)
    risk_policy: str = "high_confidence_and_low_risk"


class ExtractedKnowledge(BaseModel):
    """Structured knowledge generated from a sanitized source."""

    title: str
    note_type: NoteType = "task"
    summary: str
    facts: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    labels: list[str] = Field(default_factory=list)
    evidence_status: EvidenceStatus = "unknown"
    claims: list[Claim] = Field(default_factory=list)
    actions: list[ActionSignature] = Field(default_factory=list)
    prompt_version: str = "v1"
    model_version: str = "unknown"
    knowledge_level: KnowledgeLevel = "example"
    pattern_key: str = ""
    scope: KnowledgeScope = Field(default_factory=KnowledgeScope)

    @field_validator("facts", "links", "labels", mode="before")
    @classmethod
    def normalize_text_items(
        cls,
        value: Any,
        info: ValidationInfo,
    ) -> Any:
        """Accept gateway objects while keeping the canonical fields textual."""
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        if not isinstance(value, list):
            return value

        preferred_key = {
            "facts": "fact",
            "links": "label",
            "labels": "label",
        }[info.field_name]
        items: list[str] = []
        for item in value:
            if isinstance(item, str):
                items.append(item)
                continue
            if not isinstance(item, dict):
                continue
            candidate = item.get(preferred_key)
            if not isinstance(candidate, str):
                candidate = item.get("text") or item.get("title") or item.get("url")
            if isinstance(candidate, str):
                items.append(candidate)
        return items


class SearchResult(BaseModel):
    """Grounded result returned by lexical or graph retrieval."""

    note_id: str
    title: str
    note_type: str
    space_id: str
    path: str
    score: float
    excerpt: str
    source_refs: list[SourceReference] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    evidence_status: EvidenceStatus = "unknown"
    recommendation_state: RecommendationState = "active"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    claim_support: ClaimSupport = "unknown"
    verification_status: VerificationStatus = "verified"
    retrieval_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    match_reasons: list[str] = Field(default_factory=list)
    recommendation_level: RecommendationLevel | None = None
    evidence_score: float = Field(default=0.0, ge=0.0, le=1.0)
    quality_score: float = Field(default=0.0, ge=0.0, le=1.0)
    relevance_score: float = Field(default=1.0, ge=0.0, le=1.0)
    actionability_score: float = Field(default=0.0, ge=0.0, le=1.0)
    specificity_score: float = Field(default=0.0, ge=0.0, le=1.0)
    genericness_penalty: float = Field(default=0.0, ge=0.0, le=1.0)
    recommendation_score: float = Field(default=0.0, ge=0.0, le=1.0)
    actions: list[ActionSignature] = Field(default_factory=list)
    action_score: float = Field(default=0.0, ge=0.0, le=1.0)
    lexical_score: float = 0.0
    semantic_score: float = 0.0
    rrf_score: float = 0.0
    labels_score: float = 0.0
    graph_score: float = 0.0
    quality_penalty: float = 1.0
    claims: list[Claim] = Field(default_factory=list)
    knowledge_level: KnowledgeLevel = "example"
    pattern_key: str = ""
    scope: KnowledgeScope = Field(default_factory=KnowledgeScope)
    scope_match: ScopeMatch = "none"
    scope_fit_score: float = Field(default=1.0, ge=0.0, le=1.0)
    abstraction_fit_score: float = Field(default=1.0, ge=0.0, le=1.0)
    generalization_risk: str = "medium"


class SearchFeedback(BaseModel):
    """Sanitized relevance feedback for one search response."""

    query: str
    note_ids: list[str] = Field(min_length=1)
    relevance: SearchFeedbackRelevance
    reason: str = ""
    space_id: str = "work"
    tags: list[SearchFeedbackTag] = Field(default_factory=list)


class WorkflowProposal(BaseModel):
    """Structured workflow proposal returned by the reflection model."""

    title: str
    summary: str
    triggers: list[str] = Field(default_factory=list)
    steps: list[WorkflowStep] = Field(default_factory=list)
    validation: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    evidence_note_ids: list[str] = Field(default_factory=list)
    action: ActionSignature | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @field_validator("steps", mode="before")
    @classmethod
    def normalize_steps(cls, value: Any) -> Any:
        """Accept legacy string steps while requiring evidence in storage."""
        if value is None:
            return []
        if not isinstance(value, list):
            return value
        return [
            item if isinstance(item, dict) else {"text": str(item)} for item in value
        ]

    @field_validator("validation", mode="before")
    @classmethod
    def normalize_validation(cls, value: Any) -> Any:
        """Accept validation objects returned by some reflection models."""
        if value is None:
            return []
        if isinstance(value, dict):
            value = value.get("checks") or value.get("validation") or value.get(
                "items"
            )
            if value is None:
                return []
        if not isinstance(value, list):
            return value
        normalized: list[str] = []
        for item in value:
            if isinstance(item, str):
                normalized.append(item)
                continue
            if not isinstance(item, dict):
                continue
            text = item.get("text") or item.get("validation") or item.get(
                "description"
            )
            if isinstance(text, str):
                normalized.append(text)
        return normalized


class WorkflowStep(BaseModel):
    """One workflow step and the claims that justify it."""

    text: str
    evidence_claim_ids: list[str] = Field(default_factory=list)


class LabelAliasProposal(BaseModel):
    """One reversible taxonomy alias proposed by reflection."""

    alias: str
    canonical: str
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class ReflectionKnowledge(BaseModel):
    """Validated reflection output containing workflows and aliases."""

    workflows: list[WorkflowProposal] = Field(default_factory=list)
    aliases: list[LabelAliasProposal] = Field(default_factory=list)


class ResponseEnvelope(BaseModel):
    """Schema-v2 envelope shared by CLI and MCP responses."""

    schema_version: int = 2
    status: ResponseStatus
    method: str
    data: Any = None
    meta: dict[str, Any] = Field(default_factory=dict)
