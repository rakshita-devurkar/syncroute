"""The 'unknown is not false' contract, tested in all three directions.

Each structured evidence field has three states, and the distinction is the
foundation of the event model: a rule may fire on the negative state only.
A positive state must not fire it, and neither must an unknown one — for
different reasons. Positive means the fact was checked and was fine; unknown
means nobody looked.

The dataset exercises only the negative states, so without these tests the
positive branch would be entirely unverified.
"""

from __future__ import annotations

import pytest

from conftest import NOW, FakeProvider, make_event

from syncroute.models import DecisionSource, Route
from syncroute.policy import derive_policy_facts
from syncroute.rules import evaluate_rules
from syncroute.router import route_event

# field, negative value, route it should fire, positive value, rule id
TRISTATE_CASES = [
    ("resource_validation_state", "missing", Route.REVIEW_CONFIGURATION, "exists",
     "R040_RESOURCE_VALIDATED_MISSING"),
    ("network_probe_state", "unreachable", Route.CHECK_CONNECTIVITY, "reachable",
     "R030_NETWORK_PROBE_UNREACHABLE"),
    ("replication_position_state", "expired", Route.REVIEW_RESYNC, "available",
     "R020_REPLICATION_POSITION_EXPIRED"),
    ("auth_state", "revoked", Route.REAUTHENTICATE, "valid",
     "R010_AUTH_STATE_REVOKED"),
]


@pytest.mark.parametrize("field,negative,route,positive,rule_id", TRISTATE_CASES)
def test_negative_state_fires_its_rule(field, negative, route, positive, rule_id):
    event = make_event(**{field: negative})
    result = evaluate_rules(event, derive_policy_facts(event, now=NOW))

    assert result.route is route
    assert result.rule_id == rule_id


@pytest.mark.parametrize("field,negative,route,positive,rule_id", TRISTATE_CASES)
def test_positive_state_does_not_fire_the_rule(field, negative, route, positive, rule_id):
    """A checked-and-healthy fact must not trigger the failure workflow."""
    event = make_event(**{field: positive})
    result = evaluate_rules(event, derive_policy_facts(event, now=NOW))

    assert result.route is None, (
        f"{field}={positive} wrongly fired {result.rule_id}"
    )


@pytest.mark.parametrize("field,negative,route,positive,rule_id", TRISTATE_CASES)
def test_unknown_state_does_not_fire_the_rule(field, negative, route, positive, rule_id):
    """Unknown means nobody checked. It is not evidence of anything."""
    event = make_event(**{field: "unknown"})
    result = evaluate_rules(event, derive_policy_facts(event, now=NOW))

    assert result.route is None


@pytest.mark.parametrize("field,negative,route,positive,rule_id", TRISTATE_CASES)
def test_unknown_and_positive_are_not_interchangeable(field, negative, route, positive, rule_id):
    """Both decline the rule, but they must reach the model as different facts."""
    from syncroute.jev_client import build_state

    unknown_event = make_event(**{field: "unknown"})
    positive_event = make_event(**{field: positive})

    unknown_state = build_state(unknown_event, derive_policy_facts(unknown_event, now=NOW))
    positive_state = build_state(positive_event, derive_policy_facts(positive_event, now=NOW))

    assert unknown_state["evidence"] != positive_state["evidence"], (
        f"{field} unknown and {positive} are indistinguishable to the model"
    )
    assert "unknown" in str(unknown_state["evidence"])


def test_healthy_evidence_still_reaches_the_classifier():
    """All-healthy structured evidence plus an odd message is a model decision."""
    provider = FakeProvider("ENGINEER_REVIEW")
    event = make_event(
        error_message="Parser encountered an undocumented envelope.",
        auth_state="valid",
        resource_validation_state="exists",
        network_probe_state="reachable",
        replication_position_state="available",
    )

    decision = route_event(event, provider, now=NOW)

    assert decision.rule_id is None, "healthy evidence should match no rule"
    assert decision.model_called is True
    assert decision.source is DecisionSource.MODEL_FIXTURE


def test_healthy_evidence_does_not_suppress_a_status_code_rule():
    """Structured health says nothing about a 429; the transient rule still applies."""
    event = make_event(
        http_status=429,
        auth_state="valid",
        resource_validation_state="exists",
        network_probe_state="reachable",
    )
    result = evaluate_rules(event, derive_policy_facts(event, now=NOW))

    assert result.route is Route.RETRY_LATER
