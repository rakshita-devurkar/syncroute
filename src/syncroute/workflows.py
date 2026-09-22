"""Simulated recovery workflows.

Nothing in this module performs a real recovery. No account is reconnected, no
grant is modified, no host is contacted, no sync is restarted and no database is
resnapshotted. Each handler returns a :class:`WorkflowPlan` describing what
*would* happen, and state transitions only change simulation state.

Clock and randomness are injected so retry scheduling is testable, and no demo
delay is ever actually slept.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Optional

from .models import (
    PolicyFacts,
    Route,
    RoutingDecision,
    SyncFailureEvent,
    WorkflowPlan,
    WorkflowStatus,
    WorkflowStep,
)
from .policy import DEFAULT_SCHEDULING_HORIZON_SECONDS

BASE_BACKOFF_SECONDS = 30.0
MAX_BACKOFF_SECONDS = 900.0
JITTER_FRACTION = 0.25

SIMULATION_BANNER = "Simulated only. No external system is contacted or modified."


def compute_backoff_seconds(
    attempt_number: int,
    rng: random.Random,
    base: float = BASE_BACKOFF_SECONDS,
    cap: float = MAX_BACKOFF_SECONDS,
) -> float:
    """Capped exponential backoff with jitter. Used when no Retry-After is given."""
    exponent = max(0, attempt_number - 1)
    delay = min(cap, base * (2.0**exponent))
    return delay + rng.uniform(0.0, JITTER_FRACTION * delay)


def _retry_plan(
    event: SyncFailureEvent,
    facts: PolicyFacts,
    now: datetime,
    rng: random.Random,
    horizon: float,
) -> WorkflowPlan:
    notes = [SIMULATION_BANNER]
    if facts.retry_after_seconds is not None:
        delay = facts.retry_after_seconds
        source = f"the provider's Retry-After value ({delay:.0f}s)"
    else:
        delay = compute_backoff_seconds(event.attempt_number, rng)
        source = f"capped exponential backoff with jitter ({delay:.0f}s)"
        if facts.retry_after_parse_failed:
            notes.append(
                "The provider sent a Retry-After value that could not be parsed; "
                "backoff was used instead of guessing at the intended delay."
            )

    # Policy should have caught this already; refuse rather than retry early.
    if delay > horizon:
        return _engineer_review_plan(
            event,
            facts,
            reason=(
                f"The requested delay of {delay:.0f}s exceeds the {horizon:.0f}s scheduling "
                "horizon for this prototype."
            ),
        )

    next_attempt = now + timedelta(seconds=delay)
    return WorkflowPlan(
        route=Route.RETRY_LATER,
        status=WorkflowStatus.WAITING,
        summary=f"Queued a simulated retry using {source}.",
        required_actor="automated scheduler (simulated)",
        requires_approval=False,
        next_attempt_at=next_attempt,
        steps=[
            WorkflowStep(
                label="Compute next attempt time",
                detail=f"Derived from {source}. No sleep occurs; the timestamp is displayed only.",
                completed=True,
            ),
            WorkflowStep(
                label="Queue attempt",
                detail=(
                    f"Attempt {event.attempt_number + 1} would run at "
                    f"{next_attempt.isoformat()} with {event.retry_budget_remaining} "
                    "retry budget remaining."
                ),
            ),
        ],
        notes=notes,
    )


def _reauthenticate_plan(event: SyncFailureEvent, facts: PolicyFacts) -> WorkflowPlan:
    return WorkflowPlan(
        route=Route.REAUTHENTICATE,
        status=WorkflowStatus.NEEDS_USER_ACTION,
        summary=f"Reconnect requested for {event.connector_id} ({event.provider}).",
        required_actor="connection owner",
        steps=[
            WorkflowStep(
                label="Notify connection owner",
                detail=f"A simulated reconnect request for connector {event.connector_id}.",
                completed=True,
            ),
            WorkflowStep(
                label="Owner completes reconnect",
                detail="Mark complete below to advance the simulation. Nothing is reconnected.",
            ),
        ],
        notes=[SIMULATION_BANNER],
    )


def _fix_permissions_plan(event: SyncFailureEvent, facts: PolicyFacts) -> WorkflowPlan:
    return WorkflowPlan(
        route=Route.FIX_PERMISSIONS,
        status=WorkflowStatus.NEEDS_USER_ACTION,
        summary="Permission review task created. No grant is named or changed.",
        required_actor="source system administrator",
        steps=[
            WorkflowStep(
                label="Record the failing operation",
                detail=f"Operation '{event.operation}' at stage '{event.failure_stage}'.",
                completed=True,
            ),
            WorkflowStep(
                label="Administrator reviews the identity's access",
                detail=(
                    "The specific missing grant is not inferred. A reviewer compares the "
                    "authenticated identity's access against the operation above."
                ),
            ),
        ],
        notes=[
            SIMULATION_BANNER,
            "No grant is invented: this prototype never claims which permission is missing.",
        ],
    )


def _review_configuration_plan(event: SyncFailureEvent, facts: PolicyFacts) -> WorkflowPlan:
    return WorkflowPlan(
        route=Route.REVIEW_CONFIGURATION,
        status=WorkflowStatus.NEEDS_USER_ACTION,
        summary="Configuration review task created for the implicated fields.",
        required_actor="connector owner",
        steps=[
            WorkflowStep(
                label="Show implicated configuration",
                detail=(
                    f"connector_id={event.connector_id}, operation={event.operation}, "
                    f"failure_stage={event.failure_stage}, "
                    f"resource_validation_state={event.resource_validation_state.value}"
                ),
                completed=True,
            ),
            WorkflowStep(
                label="Owner confirms or corrects the selection",
                detail="Mark complete to record that the configuration was reviewed.",
            ),
        ],
        notes=[SIMULATION_BANNER],
    )


def _check_connectivity_plan(event: SyncFailureEvent, facts: PolicyFacts) -> WorkflowPlan:
    return WorkflowPlan(
        route=Route.CHECK_CONNECTIVITY,
        status=WorkflowStatus.ROUTED,
        summary="Diagnostic checklist prepared. All results below are mock values.",
        required_actor="platform engineer",
        steps=[
            WorkflowStep(
                label="MOCK: name resolution",
                detail="Simulated result only. No DNS query is performed.",
            ),
            WorkflowStep(
                label="MOCK: TCP reachability",
                detail="Simulated result only. No host is contacted.",
            ),
            WorkflowStep(
                label="MOCK: TLS handshake",
                detail="Simulated result only. No session is established.",
            ),
            WorkflowStep(
                label="Recorded probe state",
                detail=f"network_probe_state={event.network_probe_state.value} (from the event).",
                completed=True,
            ),
        ],
        notes=[
            SIMULATION_BANNER,
            "These diagnostics are placeholders; this prototype runs no network checks.",
        ],
    )


def _review_resync_plan(event: SyncFailureEvent, facts: PolicyFacts) -> WorkflowPlan:
    return WorkflowPlan(
        route=Route.REVIEW_RESYNC,
        status=WorkflowStatus.PENDING_APPROVAL,
        summary="Resync assessment created. It cannot proceed without explicit approval.",
        required_actor="data platform owner",
        requires_approval=True,
        steps=[
            WorkflowStep(
                label="Record replication position state",
                detail=f"replication_position={event.replication_position_state.value}.",
                completed=True,
            ),
            WorkflowStep(
                label="Human approval required",
                detail=(
                    "A resync would re-read history from the source. Depending on the "
                    "connector this can replay rows downstream, consume source capacity and "
                    "run for an extended period. A resync is never started automatically."
                ),
            ),
        ],
        notes=[
            SIMULATION_BANNER,
            "Approval here only changes simulation state; no resync is ever started.",
        ],
    )


def _engineer_review_plan(
    event: SyncFailureEvent, facts: PolicyFacts, reason: str = ""
) -> WorkflowPlan:
    steps = [
        WorkflowStep(
            label="Preserve sanitized context",
            detail=(
                f"Event {event.event_id} on incident {event.incident_id} queued with its "
                "redacted error text and structured evidence."
            ),
            completed=True,
        ),
        WorkflowStep(
            label="Record prior recovery attempts",
            detail=(
                ", ".join(
                    f"{a.action.value} ({a.outcome.value}, "
                    f"persisted={a.same_failure_persisted})"
                    for a in event.previous_recovery_attempts
                )
                or "No prior recovery attempts recorded."
            ),
            completed=True,
        ),
        WorkflowStep(label="Engineer investigates", detail="Awaiting a human in the queue."),
    ]
    notes = [SIMULATION_BANNER]
    if reason:
        notes.append(reason)
    return WorkflowPlan(
        route=Route.ENGINEER_REVIEW,
        status=WorkflowStatus.ESCALATED,
        summary="Queued for engineering review with sanitized context preserved.",
        required_actor="on-call data engineer",
        steps=steps,
        notes=notes,
    )


def plan_workflow(
    decision: RoutingDecision,
    event: SyncFailureEvent,
    *,
    now: Optional[datetime] = None,
    rng: Optional[random.Random] = None,
    scheduling_horizon_seconds: float = DEFAULT_SCHEDULING_HORIZON_SECONDS,
) -> WorkflowPlan:
    """Build the simulated plan for a decision's final route."""
    now = now or datetime.now(timezone.utc)
    rng = rng or random.Random()
    facts = decision.policy_facts
    if facts is None:  # pragma: no cover - decisions always carry facts
        from .policy import derive_policy_facts

        facts = derive_policy_facts(event, now=now)

    route = decision.final_route
    if route is Route.RETRY_LATER:
        return _retry_plan(event, facts, now, rng, scheduling_horizon_seconds)
    if route is Route.REAUTHENTICATE:
        return _reauthenticate_plan(event, facts)
    if route is Route.FIX_PERMISSIONS:
        return _fix_permissions_plan(event, facts)
    if route is Route.REVIEW_CONFIGURATION:
        return _review_configuration_plan(event, facts)
    if route is Route.CHECK_CONNECTIVITY:
        return _check_connectivity_plan(event, facts)
    if route is Route.REVIEW_RESYNC:
        return _review_resync_plan(event, facts)
    return _engineer_review_plan(event, facts, reason=decision.fallback_reason or "")


class WorkflowTransitionError(RuntimeError):
    """An attempted simulation transition that policy does not allow."""


def approve(plan: WorkflowPlan) -> WorkflowPlan:
    """Grant the human approval a plan requires. Simulation state only."""
    if not plan.requires_approval:
        raise WorkflowTransitionError(f"{plan.route.value} does not require approval.")
    steps = [s.model_copy(update={"completed": True}) for s in plan.steps]
    return plan.model_copy(
        update={
            "approved": True,
            "status": WorkflowStatus.WAITING,
            "steps": steps,
            "summary": "Approved by a human. A real system would now schedule the assessment.",
        }
    )


def complete_task(plan: WorkflowPlan) -> WorkflowPlan:
    """Mark the outstanding human task complete.

    A plan that requires approval cannot be advanced this way; approval is the
    only door, and it must be taken explicitly.
    """
    if plan.requires_approval and not plan.approved:
        raise WorkflowTransitionError(
            f"{plan.route.value} requires explicit approval before it can advance."
        )
    steps = [s.model_copy(update={"completed": True}) for s in plan.steps]
    return plan.model_copy(
        update={
            "status": WorkflowStatus.WAITING,
            "steps": steps,
            "summary": f"{plan.summary} Task marked complete in the simulation.",
        }
    )


def mark_recovered(plan: WorkflowPlan, *, declared_by: str) -> WorkflowPlan:
    """Move to SIMULATED_RECOVERED.

    Only a user action or a scenario's declared outcome may call this. Recovery
    is never inferred from the predicted route.
    """
    if plan.requires_approval and not plan.approved:
        raise WorkflowTransitionError(
            f"{plan.route.value} requires approval before it can be marked recovered."
        )
    return plan.model_copy(
        update={
            "status": WorkflowStatus.SIMULATED_RECOVERED,
            "steps": [s.model_copy(update={"completed": True}) for s in plan.steps],
            "summary": f"Marked recovered in the simulation by {declared_by}.",
        }
    )
