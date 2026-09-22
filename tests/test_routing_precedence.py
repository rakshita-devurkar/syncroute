"""Precedence tests: the order of the pipeline is the product."""

from __future__ import annotations

import pytest

from conftest import NOW, FailingProvider, FakeProvider, make_event

from syncroute.models import DecisionSource, Route
from syncroute.router import RouterConfig, route_event


def test_known_transient_bypasses_the_model_and_respects_retry_after():
    provider = FakeProvider("REAUTHENTICATE")
    event = make_event(error_message="Too many requests.", http_status=429, retry_after="60")

    decision = route_event(event, provider, now=NOW)

    assert decision.final_route is Route.RETRY_LATER
    assert decision.source is DecisionSource.DETERMINISTIC_RULE
    assert decision.rule_id == "R001_HTTP_429_RATE_LIMIT"
    assert decision.model_called is False
    assert provider.calls == [], "a rule-decided event must never reach the model"
    assert decision.policy_facts.retry_after_seconds == 60.0


def test_exhausted_budget_overrides_a_retry_selection():
    provider = FakeProvider("RETRY_LATER")
    event = make_event(
        error_message="Service temporarily unavailable.",
        http_status=503,
        retry_budget_remaining=0,
        attempt_number=4,
    )

    decision = route_event(event, provider, now=NOW)

    assert decision.final_route is Route.ENGINEER_REVIEW
    assert decision.source is DecisionSource.POLICY_ESCALATION
    assert decision.policy_id == "P001_RETRY_BUDGET_EXHAUSTED"
    assert decision.model_called is False


def test_model_cannot_lift_an_exhausted_retry_budget():
    """A non-transient failure reaches the model; the model says retry anyway."""
    provider = FakeProvider("RETRY_LATER")
    event = make_event(
        error_message="An unfamiliar condition occurred while reading.",
        retry_budget_remaining=0,
        attempt_number=3,
    )

    decision = route_event(event, provider, now=NOW)

    assert decision.proposed_route is Route.RETRY_LATER
    assert decision.final_route is Route.ENGINEER_REVIEW
    assert decision.source is DecisionSource.POST_ROUTE_POLICY
    assert decision.policy_id == "P010_RETRY_NOT_PERMITTED"


def test_repeated_failure_after_completed_reauthentication_escalates():
    provider = FakeProvider("REAUTHENTICATE")
    event = make_event(
        error_message="The application's consent was withdrawn.",
        reauthentication_completed_since_failure=True,
        consecutive_failure_count=4,
    )

    decision = route_event(event, provider, now=NOW)

    assert decision.final_route is Route.ENGINEER_REVIEW
    assert decision.source is DecisionSource.POLICY_ESCALATION
    assert decision.policy_id == "P002_PERSISTENT_AFTER_RECOVERY"
    assert decision.model_called is False


def test_unrelated_past_recovery_does_not_escalate():
    """An old attempt that did NOT persist the same failure is not evidence of a loop."""
    provider = FakeProvider("REAUTHENTICATE")
    event = make_event(
        error_message="The application's consent was withdrawn.",
        previous_recovery_attempts=[
            {
                "action": "REVIEW_CONFIGURATION",
                "timestamp": "2026-09-01T10:00:00Z",
                "outcome": "completed",
                "same_failure_persisted": False,
            },
            {
                "action": "RETRY_LATER",
                "timestamp": "2026-09-02T10:00:00Z",
                "outcome": "failed",
                "same_failure_persisted": False,
            },
        ],
    )

    decision = route_event(event, provider, now=NOW)

    assert decision.final_route is Route.REAUTHENTICATE
    assert decision.source is DecisionSource.MODEL_FIXTURE
    assert decision.policy_id is None


def test_two_persisted_recoveries_are_a_loop():
    event = make_event(
        error_message="The operation failed again after remediation.",
        previous_recovery_attempts=[
            {"action": "REAUTHENTICATE", "timestamp": "2026-09-22T06:00:00Z",
             "outcome": "completed", "same_failure_persisted": True},
            {"action": "FIX_PERMISSIONS", "timestamp": "2026-09-22T07:00:00Z",
             "outcome": "completed", "same_failure_persisted": True},
        ],
    )

    decision = route_event(event, FakeProvider("RETRY_LATER"), now=NOW)

    assert decision.policy_id == "P003_RECOVERY_LOOP"
    assert decision.final_route is Route.ENGINEER_REVIEW


@pytest.mark.parametrize("status", [401, 403, 404])
def test_ambiguous_status_codes_have_no_rule(status: int):
    """403 must not imply permissions, 401 must not imply reconnect, 404 must not imply config."""
    provider = FakeProvider("FIX_PERMISSIONS")
    event = make_event(error_message="Access denied.", http_status=status)

    decision = route_event(event, provider, now=NOW)

    assert decision.rule_id is None
    assert decision.model_called is True, f"HTTP {status} must be judged, not rule-mapped"


def test_conflicting_rules_escalate_instead_of_guessing():
    event = make_event(
        error_message="Too many requests.", http_status=429, auth_state="revoked"
    )

    decision = route_event(event, FakeProvider("RETRY_LATER"), now=NOW)

    assert decision.final_route is Route.ENGINEER_REVIEW
    assert decision.rule_id == "CONFLICT"
    assert decision.fallback_reason == "rule_conflict"
    assert decision.model_called is False


def test_retry_delay_beyond_horizon_is_reviewed_not_retried_early():
    event = make_event(
        error_message="Service temporarily unavailable.", http_status=503, retry_after="7200"
    )

    decision = route_event(event, FakeProvider("RETRY_LATER"), now=NOW)

    assert decision.proposed_route is Route.RETRY_LATER
    assert decision.final_route is Route.ENGINEER_REVIEW
    assert decision.policy_id == "P011_RETRY_DELAY_EXCEEDS_HORIZON"
