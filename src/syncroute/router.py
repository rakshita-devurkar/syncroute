"""The single routing pipeline shared by the UI, the CLI and evaluation.

Order is fixed and each stage records why it fired:

1. sanitize   - redact secret shapes before anything leaves the process
2. derive     - compute counters, elapsed time and retry delays in code
3. escalate   - mandatory policy escalations (before any model call)
4. rules      - high-precision deterministic rules; conflicts escalate
5. classify   - Jev, once, for whatever is left
6. gate       - confidence and top-two margin thresholds
7. re-check   - policy runs again over the proposed route
8. explain    - deterministic template text, labelled as routing policy

``proposed_route`` and ``final_route`` are always kept apart, and an
ENGINEER_REVIEW outcome always records whether it was chosen or fallen back to.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .jev_client import (
    JevAuthError,
    JevError,
    JevProvider,
    JevUnavailable,
    build_state,
)
from .models import (
    DecisionSource,
    ModelAnswer,
    PolicyFacts,
    Route,
    RoutingDecision,
    SyncFailureEvent,
)
from .policy import (
    DEFAULT_SCHEDULING_HORIZON_SECONDS,
    check_mandatory_escalation,
    check_post_route_policy,
    derive_policy_facts,
)
from .rules import evaluate_rules
from .sanitize import sanitize_event

PIPELINE_VERSION = "pipeline-2026.09.1"

EXPLANATION_PREFIX = "Routing policy explanation (generated from the decision record, not model reasoning):"


@dataclass(frozen=True)
class RouterConfig:
    """Demonstration defaults, not calibrated guarantees.

    The thresholds below were chosen as a starting point and may only be tuned
    on development cases. Retuning them invalidates a frozen held-out claim.
    """

    confidence_threshold: float = 0.80
    margin_threshold: float = 0.15
    scheduling_horizon_seconds: float = DEFAULT_SCHEDULING_HORIZON_SECONDS
    system: str = "hybrid"  # "hybrid" | "rules_only"

    @property
    def is_rules_only(self) -> bool:
        return self.system == "rules_only"


def new_run_id(prefix: str = "run") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _explain(
    *,
    final_route: Route,
    source: DecisionSource,
    rule_id: Optional[str],
    policy_id: Optional[str],
    reason: str,
    answer: Optional[ModelAnswer],
    config: RouterConfig,
) -> str:
    """Deterministic template text. Never presented as the model's reasoning."""
    parts = [f"{EXPLANATION_PREFIX}"]
    if source is DecisionSource.POLICY_ESCALATION:
        parts.append(f"Policy {policy_id} escalated this before classification. {reason}")
    elif source is DecisionSource.DETERMINISTIC_RULE:
        parts.append(f"Deterministic rule {rule_id} selected {final_route.value}. {reason}")
    elif source in (DecisionSource.MODEL, DecisionSource.MODEL_FIXTURE):
        label = "A fixture response" if source is DecisionSource.MODEL_FIXTURE else "Jev"
        assert answer is not None
        parts.append(
            f"No deterministic rule applied, so the event was classified. {label} selected "
            f"{answer.choice.value} with probability {answer.selected_probability:.2f}, "
            f"top-two margin {answer.top_two_margin:.2f} and reported confidence "
            f"{answer.confidence:.2f}. Both gates passed "
            f"(confidence >= {config.confidence_threshold:.2f}, "
            f"margin >= {config.margin_threshold:.2f})."
        )
    elif source is DecisionSource.UNCERTAINTY_FALLBACK:
        assert answer is not None
        parts.append(
            f"The classifier proposed {answer.choice.value}, but the uncertainty gate failed "
            f"(probability {answer.selected_probability:.2f}, margin {answer.top_two_margin:.2f}, "
            f"confidence {answer.confidence:.2f} against thresholds "
            f"{config.confidence_threshold:.2f}/{config.margin_threshold:.2f}). "
            "The event was sent for engineering review instead."
        )
    elif source is DecisionSource.API_ERROR_FALLBACK:
        parts.append(f"Classification failed and no result was substituted. {reason}")
    elif source is DecisionSource.CLASSIFICATION_UNAVAILABLE:
        parts.append(f"No classification was available in this mode. {reason}")
    elif source is DecisionSource.BASELINE_UNMATCHED:
        parts.append(
            "Rules-only baseline: no deterministic rule matched, so the event goes to "
            "engineering review by definition."
        )
    elif source is DecisionSource.POST_ROUTE_POLICY:
        parts.append(f"Policy {policy_id} overrode the proposed route. {reason}")
    if final_route is Route.REVIEW_RESYNC:
        parts.append("A resync assessment requires human approval and never starts automatically.")
    return " ".join(parts)


def route_event(
    event: SyncFailureEvent,
    provider: Optional[JevProvider] = None,
    config: Optional[RouterConfig] = None,
    *,
    run_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> RoutingDecision:
    """Route one event. Never raises for a classification failure."""
    config = config or RouterConfig()
    run_id = run_id or new_run_id()
    now = now or datetime.now(timezone.utc)
    started = time.perf_counter()

    # 1. Sanitize before anything else touches the event.
    clean_event, redactions = sanitize_event(event)

    # 2. Facts derived in code, never asked of the model.
    facts: PolicyFacts = derive_policy_facts(clean_event, now=now)

    mode = "rules_only" if config.is_rules_only else getattr(provider, "mode", "fixture")

    def finish(
        *,
        final_route: Route,
        source: DecisionSource,
        proposed_route: Optional[Route] = None,
        rule_id: Optional[str] = None,
        policy_id: Optional[str] = None,
        reason: str = "",
        answer: Optional[ModelAnswer] = None,
        model_called: bool = False,
        fallback_reason: Optional[str] = None,
    ) -> RoutingDecision:
        return RoutingDecision(
            run_id=run_id,
            event_id=clean_event.event_id,
            incident_id=clean_event.incident_id,
            decided_at=now,
            proposed_route=proposed_route,
            final_route=final_route,
            source=source,
            rule_id=rule_id,
            policy_id=policy_id,
            fallback_reason=fallback_reason,
            explanation=_explain(
                final_route=final_route,
                source=source,
                rule_id=rule_id,
                policy_id=policy_id,
                reason=reason,
                answer=answer,
                config=config,
            ),
            answer=answer,
            policy_facts=facts,
            redactions=redactions,
            model_called=model_called,
            total_latency_ms=(time.perf_counter() - started) * 1000.0,
            mode=mode,
            system="rules_only" if config.is_rules_only else "hybrid",
        )

    # 3. Mandatory escalation, before rules and before any model call.
    escalation = check_mandatory_escalation(clean_event, facts)
    if escalation is not None:
        return finish(
            final_route=escalation.route,
            source=DecisionSource.POLICY_ESCALATION,
            policy_id=escalation.policy_id,
            reason=escalation.reason,
            fallback_reason=f"policy:{escalation.policy_id}",
        )

    # 4. Deterministic rules.
    rule_result = evaluate_rules(clean_event, facts)
    if rule_result.route is not None:
        if rule_result.conflict:
            return finish(
                final_route=Route.ENGINEER_REVIEW,
                source=DecisionSource.DETERMINISTIC_RULE,
                rule_id=rule_result.rule_id,
                reason=rule_result.reason,
                fallback_reason="rule_conflict",
            )
        override = check_post_route_policy(
            rule_result.route, clean_event, facts, config.scheduling_horizon_seconds
        )
        if override is not None:
            return finish(
                final_route=override.route,
                source=DecisionSource.POST_ROUTE_POLICY,
                proposed_route=rule_result.route,
                rule_id=rule_result.rule_id,
                policy_id=override.policy_id,
                reason=override.reason,
                fallback_reason=f"policy:{override.policy_id}",
            )
        return finish(
            final_route=rule_result.route,
            source=DecisionSource.DETERMINISTIC_RULE,
            proposed_route=rule_result.route,
            rule_id=rule_result.rule_id,
            reason=rule_result.reason,
        )

    # 5. Baseline stops here: everything unmatched is review by definition.
    if config.is_rules_only:
        return finish(
            final_route=Route.ENGINEER_REVIEW,
            source=DecisionSource.BASELINE_UNMATCHED,
            fallback_reason="rules_only_unmatched",
        )

    if provider is None:
        return finish(
            final_route=Route.ENGINEER_REVIEW,
            source=DecisionSource.CLASSIFICATION_UNAVAILABLE,
            reason="No classification provider was configured.",
            fallback_reason="no_provider",
        )

    # 6. Classify. A failure here is labelled, never silently mocked.
    state = build_state(clean_event, facts)
    try:
        answer = provider.classify(state, clean_event.event_id)
    except JevUnavailable as exc:
        return finish(
            final_route=Route.ENGINEER_REVIEW,
            source=DecisionSource.CLASSIFICATION_UNAVAILABLE,
            reason=str(exc),
            fallback_reason="classification_unavailable",
            model_called=False,
        )
    except JevAuthError as exc:
        return finish(
            final_route=Route.ENGINEER_REVIEW,
            source=DecisionSource.API_ERROR_FALLBACK,
            reason=f"Authentication with the classification API failed: {exc}",
            fallback_reason="api_auth_error",
            model_called=True,
        )
    except JevError as exc:
        return finish(
            final_route=Route.ENGINEER_REVIEW,
            source=DecisionSource.API_ERROR_FALLBACK,
            reason=f"{exc.__class__.__name__}: {exc}",
            fallback_reason=f"api_error:{exc.__class__.__name__}",
            model_called=True,
        )

    model_source = DecisionSource.MODEL_FIXTURE if answer.is_fixture else DecisionSource.MODEL

    # 7. Uncertainty gating.
    if answer.confidence < config.confidence_threshold:
        return finish(
            final_route=Route.ENGINEER_REVIEW,
            source=DecisionSource.UNCERTAINTY_FALLBACK,
            proposed_route=answer.choice,
            answer=answer,
            fallback_reason="confidence_below_threshold",
            model_called=True,
        )
    if answer.top_two_margin < config.margin_threshold:
        return finish(
            final_route=Route.ENGINEER_REVIEW,
            source=DecisionSource.UNCERTAINTY_FALLBACK,
            proposed_route=answer.choice,
            answer=answer,
            fallback_reason="margin_below_threshold",
            model_called=True,
        )

    # 8. Policy re-check: a classification cannot lift a retry limit.
    override = check_post_route_policy(
        answer.choice, clean_event, facts, config.scheduling_horizon_seconds
    )
    if override is not None:
        return finish(
            final_route=override.route,
            source=DecisionSource.POST_ROUTE_POLICY,
            proposed_route=answer.choice,
            policy_id=override.policy_id,
            reason=override.reason,
            answer=answer,
            fallback_reason=f"policy:{override.policy_id}",
            model_called=True,
        )

    return finish(
        final_route=answer.choice,
        source=model_source,
        proposed_route=answer.choice,
        answer=answer,
        model_called=True,
    )
