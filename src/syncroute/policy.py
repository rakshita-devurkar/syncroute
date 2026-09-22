"""Execution policy: facts derived in code, and escalations ordinary code owns.

Two reasons this module exists separately from the model:

1. jev-1.13's documented weak spots are counting, numeric precision and date
   ordering. Every comparison of that kind happens here instead.
2. A classification must never be able to authorise a retry past its budget or
   an unapproved resync. Policy runs before the model (mandatory escalation)
   and again after it (post-route checks).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

from .models import (
    AttemptOutcome,
    PolicyFacts,
    Route,
    SyncFailureEvent,
)

POLICY_VERSION = "policy-2026.09.1"

#: How far ahead this proof of concept is willing to schedule a simulated retry.
DEFAULT_SCHEDULING_HORIZON_SECONDS = 3600.0

#: HTTP statuses treated as a transient, retry-shaped failure.
TRANSIENT_STATUSES = frozenset({429, 502, 503, 504})


@dataclass(frozen=True)
class PolicyOutcome:
    """A policy that fired and the route it forces."""

    policy_id: str
    route: Route
    reason: str


def parse_retry_after(raw: Optional[str], now: datetime) -> tuple[Optional[float], bool]:
    """Parse a Retry-After value into seconds from ``now``.

    Accepts delta-seconds or an HTTP-date, per RFC 9110. Returns
    ``(seconds, parse_failed)``. A past date yields 0.0, not a negative delay.
    """
    if raw is None:
        return None, False
    text = raw.strip()
    if not text:
        return None, False
    try:
        seconds = float(int(text))
    except ValueError:
        pass
    else:
        return (max(0.0, seconds), False)
    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None, True
    if when is None:
        return None, True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - now).total_seconds()), False


def derive_policy_facts(event: SyncFailureEvent, now: Optional[datetime] = None) -> PolicyFacts:
    """Compute every numeric and temporal fact the pipeline relies on."""
    now = now or datetime.now(timezone.utc)

    persisted = [
        attempt
        for attempt in event.previous_recovery_attempts
        if attempt.same_failure_persisted and attempt.outcome is AttemptOutcome.COMPLETED
    ]
    persisted_actions: list[Route] = []
    for attempt in persisted:
        if attempt.action not in persisted_actions:
            persisted_actions.append(attempt.action)

    # A reauthentication that finished after the failure, where the failure is
    # still recurring, counts as a persisted recovery even without an explicit
    # attempt record.
    if (
        event.reauthentication_completed_since_failure
        and event.consecutive_failure_count > 1
        and Route.REAUTHENTICATE not in persisted_actions
    ):
        persisted_actions.append(Route.REAUTHENTICATE)

    retry_after_seconds, parse_failed = parse_retry_after(event.retry_after, now)

    return PolicyFacts(
        retry_budget_exhausted=event.retry_budget_remaining <= 0,
        retry_attempts_made=max(0, event.attempt_number - 1),
        same_failure_persisted_after_recovery=bool(persisted_actions),
        persisted_recovery_actions=persisted_actions,
        recovery_loop_detected=len(persisted_actions) >= 2,
        distinct_recovery_actions_tried=len({a.action for a in event.previous_recovery_attempts}),
        minutes_since_first_failure=(
            (now - event.first_failure_at).total_seconds() / 60.0
            if event.first_failure_at
            else None
        ),
        minutes_since_last_success=(
            (now - event.last_success_at).total_seconds() / 60.0 if event.last_success_at else None
        ),
        retry_after_seconds=retry_after_seconds,
        retry_after_parse_failed=parse_failed,
    )


def _looks_retry_shaped(event: SyncFailureEvent) -> bool:
    """Is this the kind of failure whose recovery path is retrying?

    Used to scope the exhausted-budget escalation. A revoked credential with no
    retry budget left is not a retry loop; it still needs reconnecting.
    """
    if event.http_status in TRANSIENT_STATUSES:
        return True
    return any(a.action is Route.RETRY_LATER for a in event.previous_recovery_attempts)


def check_mandatory_escalation(
    event: SyncFailureEvent, facts: PolicyFacts
) -> Optional[PolicyOutcome]:
    """Escalations that run before rules and before any model call.

    Only recovery attempts that actually persisted the *same* failure count.
    An unrelated historical attempt must not escalate a fresh failure.
    """
    if facts.recovery_loop_detected:
        actions = ", ".join(r.value for r in facts.persisted_recovery_actions)
        return PolicyOutcome(
            policy_id="P003_RECOVERY_LOOP",
            route=Route.ENGINEER_REVIEW,
            reason=(
                f"The same failure persisted after {len(facts.persisted_recovery_actions)} "
                f"different completed recovery actions ({actions})."
            ),
        )

    if facts.same_failure_persisted_after_recovery:
        action = facts.persisted_recovery_actions[0].value
        return PolicyOutcome(
            policy_id="P002_PERSISTENT_AFTER_RECOVERY",
            route=Route.ENGINEER_REVIEW,
            reason=(
                f"The same failure continued after {action} completed, so repeating "
                "that workflow is not the next action."
            ),
        )

    if facts.retry_budget_exhausted and _looks_retry_shaped(event):
        return PolicyOutcome(
            policy_id="P001_RETRY_BUDGET_EXHAUSTED",
            route=Route.ENGINEER_REVIEW,
            reason=(
                f"The retry budget is exhausted after {facts.retry_attempts_made} attempt(s) "
                "and the failure is retry-shaped, so another retry is not permitted."
            ),
        )
    return None


def check_post_route_policy(
    route: Route,
    event: SyncFailureEvent,
    facts: PolicyFacts,
    scheduling_horizon_seconds: float = DEFAULT_SCHEDULING_HORIZON_SECONDS,
) -> Optional[PolicyOutcome]:
    """Re-apply policy to a proposed route before any workflow is planned.

    A model response never overrides a retry limit or a required approval.
    """
    if route is Route.RETRY_LATER:
        if facts.retry_budget_exhausted:
            return PolicyOutcome(
                policy_id="P010_RETRY_NOT_PERMITTED",
                route=Route.ENGINEER_REVIEW,
                reason="RETRY_LATER was proposed but the retry budget is exhausted.",
            )
        if (
            facts.retry_after_seconds is not None
            and facts.retry_after_seconds > scheduling_horizon_seconds
        ):
            return PolicyOutcome(
                policy_id="P011_RETRY_DELAY_EXCEEDS_HORIZON",
                route=Route.ENGINEER_REVIEW,
                reason=(
                    f"The provider asked for a {facts.retry_after_seconds:.0f}s delay, beyond "
                    f"the {scheduling_horizon_seconds:.0f}s scheduling horizon. Retrying "
                    "sooner than permitted is not an option, so this needs review."
                ),
            )

    if route is Route.REAUTHENTICATE and Route.REAUTHENTICATE in facts.persisted_recovery_actions:
        return PolicyOutcome(
            policy_id="P012_REAUTH_ALREADY_COMPLETED",
            route=Route.ENGINEER_REVIEW,
            reason="Reconnecting already completed and the same failure continued.",
        )
    return None
