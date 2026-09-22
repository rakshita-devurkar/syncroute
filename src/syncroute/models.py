"""Validated domain models for SyncRoute.

Design rules enforced here:

* ``Route`` is the single shared enum. Rules, the model criteria, the UI,
  persistence and evaluation all refer to it; nothing may invent a route.
* ``SyncFailureEvent`` is the *model-visible* record. Evaluation labels live in
  ``EventMetadata`` and are deliberately kept in a separate object so that a
  label cannot leak into an API payload.
* Unknown is a distinct state from false. Every tri-state field defaults to
  ``unknown`` rather than to a negative assertion.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
)

MAX_ERROR_MESSAGE_CHARS = 8000
MAX_PREVIOUS_ATTEMPTS = 20

ShortStr = Annotated[str, StringConstraints(min_length=1, max_length=200, strip_whitespace=True)]


class Route(str, enum.Enum):
    """The seven recovery workflows. This set is closed."""

    RETRY_LATER = "RETRY_LATER"
    REAUTHENTICATE = "REAUTHENTICATE"
    FIX_PERMISSIONS = "FIX_PERMISSIONS"
    REVIEW_CONFIGURATION = "REVIEW_CONFIGURATION"
    CHECK_CONNECTIVITY = "CHECK_CONNECTIVITY"
    REVIEW_RESYNC = "REVIEW_RESYNC"
    ENGINEER_REVIEW = "ENGINEER_REVIEW"


ALL_ROUTES: tuple[Route, ...] = tuple(Route)

#: Route semantics handed to the model as Choice criteria. These describe the
#: *next action*, not a claimed root cause.
ROUTE_CRITERIA: dict[str, str] = {
    Route.RETRY_LATER.value: (
        "The evidence indicates a transient service-side failure or a rate limit, "
        "and the supplied retry budget still permits another attempt."
    ),
    Route.REAUTHENTICATE.value: (
        "The evidence indicates the stored credential is invalid, expired, or revoked, "
        "so the connection must be reconnected or its access refreshed."
    ),
    Route.FIX_PERMISSIONS.value: (
        "The evidence indicates the identity authenticated successfully but lacks a "
        "required grant, scope, or resource permission for the attempted operation."
    ),
    Route.REVIEW_CONFIGURATION.value: (
        "The evidence indicates the selected resource or a configured setting is "
        "incorrect or obsolete, such as a configured object that no longer exists "
        "while the account itself responds normally."
    ),
    Route.CHECK_CONNECTIVITY.value: (
        "The evidence indicates a name-resolution, network reachability, connection "
        "establishment, or TLS problem that prevented a session from being established."
    ),
    Route.REVIEW_RESYNC.value: (
        "The evidence indicates the replication cursor, retained logs, or resume "
        "position can no longer support continuing from the last position."
    ),
    Route.ENGINEER_REVIEW.value: (
        "The failure is unknown, the error language is ambiguous or self-contradictory, or "
        "several causes would imply incompatible next actions and the evidence does not "
        "resolve between them. Also choose this for a failure none of the other six "
        "workflows addresses. Do not choose this merely because a structured evidence field "
        "is unknown, and do not choose it when the error language plainly indicates one of "
        "the other workflows."
    ),
}


class AuthState(str, enum.Enum):
    UNKNOWN = "unknown"
    VALID = "valid"
    EXPIRED = "expired"
    REVOKED = "revoked"


class ResourceValidationState(str, enum.Enum):
    UNKNOWN = "unknown"
    EXISTS = "exists"
    MISSING = "missing"


class NetworkProbeState(str, enum.Enum):
    UNKNOWN = "unknown"
    REACHABLE = "reachable"
    UNREACHABLE = "unreachable"


class ReplicationPositionState(str, enum.Enum):
    UNKNOWN = "unknown"
    AVAILABLE = "available"
    EXPIRED = "expired"


class ConnectorType(str, enum.Enum):
    SAAS_API = "saas_api"
    DATABASE = "database"
    FILE_SOURCE = "file_source"


class AttemptOutcome(str, enum.Enum):
    """What happened to a previously attempted recovery action."""

    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"
    PENDING = "pending"


class DecisionSource(str, enum.Enum):
    """Which stage of the pipeline produced the final route."""

    POLICY_ESCALATION = "POLICY_ESCALATION"
    DETERMINISTIC_RULE = "DETERMINISTIC_RULE"
    MODEL = "MODEL"
    MODEL_FIXTURE = "MODEL_FIXTURE"
    UNCERTAINTY_FALLBACK = "UNCERTAINTY_FALLBACK"
    API_ERROR_FALLBACK = "API_ERROR_FALLBACK"
    BASELINE_UNMATCHED = "BASELINE_UNMATCHED"
    POST_ROUTE_POLICY = "POST_ROUTE_POLICY"
    CLASSIFICATION_UNAVAILABLE = "CLASSIFICATION_UNAVAILABLE"


#: Sources whose ENGINEER_REVIEW outcome is a fallback rather than an explicit
#: selection of the investigation workflow.
FALLBACK_SOURCES = frozenset(
    {
        DecisionSource.UNCERTAINTY_FALLBACK,
        DecisionSource.API_ERROR_FALLBACK,
        DecisionSource.BASELINE_UNMATCHED,
        DecisionSource.CLASSIFICATION_UNAVAILABLE,
    }
)


class RecoveryAttempt(BaseModel):
    """A previously attempted recovery action on the same incident."""

    model_config = ConfigDict(extra="forbid")

    action: Route
    timestamp: datetime
    outcome: AttemptOutcome
    same_failure_persisted: bool = False

    @field_validator("timestamp")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return _as_utc(v)


def _as_utc(value: datetime) -> datetime:
    """Coerce to timezone-aware UTC; naive input is assumed to be UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class SyncFailureEvent(BaseModel):
    """A single connector failure.

    This is the only object permitted to reach a model payload (after
    sanitization). It intentionally has no field for an expected route.
    """

    model_config = ConfigDict(extra="forbid", use_enum_values=False)

    # Identity
    event_id: ShortStr
    incident_id: ShortStr
    connector_id: ShortStr
    provider: ShortStr
    connector_type: ConnectorType

    # What failed
    occurred_at: datetime
    failure_stage: ShortStr
    operation: ShortStr
    error_message: Annotated[str, StringConstraints(min_length=1, max_length=MAX_ERROR_MESSAGE_CHARS)]
    http_status: Optional[int] = Field(default=None, ge=100, le=599)
    provider_error_code: Optional[ShortStr] = None
    retry_after: Optional[str] = Field(
        default=None,
        max_length=200,
        description="Raw provider Retry-After value: delta-seconds or an HTTP-date.",
    )

    # Attempt accounting
    attempt_number: int = Field(default=1, ge=0, le=10_000)
    retry_budget_remaining: int = Field(default=0, ge=0, le=10_000)
    consecutive_failure_count: int = Field(default=1, ge=0, le=10_000)
    first_failure_at: Optional[datetime] = None

    # Last known good
    last_success_at: Optional[datetime] = None
    last_successful_stage: Optional[ShortStr] = None

    # Structured evidence. `unknown` is never equivalent to a negative fact.
    auth_state: AuthState = AuthState.UNKNOWN
    credential_refresh_attempted: bool = False
    credential_refresh_succeeded: Optional[bool] = None
    reauthentication_completed_since_failure: bool = False
    resource_validation_state: ResourceValidationState = ResourceValidationState.UNKNOWN
    network_probe_state: NetworkProbeState = NetworkProbeState.UNKNOWN
    replication_position_state: ReplicationPositionState = ReplicationPositionState.UNKNOWN

    previous_recovery_attempts: list[RecoveryAttempt] = Field(
        default_factory=list, max_length=MAX_PREVIOUS_ATTEMPTS
    )

    @field_validator("occurred_at", "first_failure_at", "last_success_at")
    @classmethod
    def _utc_fields(cls, v: Optional[datetime]) -> Optional[datetime]:
        return None if v is None else _as_utc(v)

    @field_validator("error_message")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("error_message must not be blank")
        return v


class EventMetadata(BaseModel):
    """Evaluation-only metadata.

    Deliberately a separate model. Nothing in the routing pipeline reads
    ``expected_route``, and :mod:`syncroute.sanitize` never sees this object.
    """

    model_config = ConfigDict(extra="forbid")

    event_id: ShortStr
    expected_route: Route
    family_id: ShortStr
    split: Literal["development", "heldout", "demo"]
    label_rationale: str = Field(max_length=2000)
    synthetic: Literal[True] = True
    human_reviewed: bool = False
    difficulty: Literal["easy", "medium", "hard"] = "medium"


class LabeledEvent(BaseModel):
    """Dataset row: the event and its labels, kept structurally apart."""

    model_config = ConfigDict(extra="forbid")

    event: SyncFailureEvent
    meta: EventMetadata


class PolicyFacts(BaseModel):
    """Numeric and temporal facts derived in code, never asked of the model."""

    model_config = ConfigDict(extra="forbid")

    retry_budget_exhausted: bool
    retry_attempts_made: int
    same_failure_persisted_after_recovery: bool
    persisted_recovery_actions: list[Route] = Field(default_factory=list)
    recovery_loop_detected: bool
    distinct_recovery_actions_tried: int
    minutes_since_first_failure: Optional[float] = None
    minutes_since_last_success: Optional[float] = None
    retry_after_seconds: Optional[float] = None
    retry_after_parse_failed: bool = False


class ModelAnswer(BaseModel):
    """A validated Jev choice answer."""

    model_config = ConfigDict(extra="forbid")

    choice: Route
    probabilities: dict[str, float]
    #: Distribution-derived statistic reported by the API. Stored separately
    #: from ``selected_probability``; it is not a probability of correctness.
    confidence: float = Field(ge=0.0, le=1.0)
    selected_probability: float = Field(ge=0.0, le=1.0)
    top_two_margin: float = Field(ge=0.0, le=1.0)
    model_version: Optional[str] = None
    input_tokens: Optional[int] = Field(default=None, ge=0)
    output_tokens: Optional[int] = Field(default=None, ge=0)
    is_fixture: bool = False
    latency_ms: Optional[float] = Field(default=None, ge=0.0)
    retry_count: int = Field(default=0, ge=0)
    raw_response: Optional[dict[str, Any]] = None


class RoutingDecision(BaseModel):
    """The immutable record of one routing decision within one run."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    event_id: str
    incident_id: str
    decided_at: datetime

    proposed_route: Optional[Route] = None
    final_route: Route
    source: DecisionSource

    rule_id: Optional[str] = None
    policy_id: Optional[str] = None
    fallback_reason: Optional[str] = None
    explanation: str = ""

    answer: Optional[ModelAnswer] = None
    policy_facts: Optional[PolicyFacts] = None
    redactions: list[str] = Field(default_factory=list)

    model_called: bool = False
    total_latency_ms: float = 0.0
    mode: Literal["fixture", "live", "rules_only"] = "fixture"
    system: Literal["hybrid", "rules_only"] = "hybrid"

    @property
    def is_fallback_review(self) -> bool:
        """True when ENGINEER_REVIEW came from a fallback, not a selection."""
        return self.final_route is Route.ENGINEER_REVIEW and self.source in FALLBACK_SOURCES

    @field_validator("decided_at")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return _as_utc(v)


class WorkflowStatus(str, enum.Enum):
    ROUTED = "ROUTED"
    WAITING = "WAITING"
    NEEDS_USER_ACTION = "NEEDS_USER_ACTION"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    ESCALATED = "ESCALATED"
    SIMULATED_RECOVERED = "SIMULATED_RECOVERED"


class WorkflowStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    detail: str = ""
    completed: bool = False


class WorkflowPlan(BaseModel):
    """A simulated recovery workflow. Nothing here touches a real system."""

    model_config = ConfigDict(extra="forbid")

    route: Route
    status: WorkflowStatus
    summary: str
    steps: list[WorkflowStep] = Field(default_factory=list)
    required_actor: str
    requires_approval: bool = False
    approved: bool = False
    next_attempt_at: Optional[datetime] = None
    simulated: Literal[True] = True
    notes: list[str] = Field(default_factory=list)
