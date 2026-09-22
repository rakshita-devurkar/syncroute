"""Uncertainty gating and failure handling."""

from __future__ import annotations

import httpx
import pytest

from conftest import NOW, FailingProvider, FakeProvider, make_event, make_response

from syncroute.jev_client import (
    JevAuthError,
    JevInvalidResponse,
    JevTransientError,
    LiveJevProvider,
    QUESTION_KEY,
)
from syncroute.models import DecisionSource, Route
from syncroute.router import RouterConfig, route_event


def test_low_confidence_falls_back_to_review():
    provider = FakeProvider("FIX_PERMISSIONS", probability=0.70, confidence=0.55)
    decision = route_event(make_event(error_message="Access denied."), provider, now=NOW)

    assert decision.proposed_route is Route.FIX_PERMISSIONS
    assert decision.final_route is Route.ENGINEER_REVIEW
    assert decision.source is DecisionSource.UNCERTAINTY_FALLBACK
    assert decision.fallback_reason == "confidence_below_threshold"
    assert decision.is_fallback_review is True


def test_small_margin_falls_back_even_when_confidence_is_high():
    """Confidence and margin are separate gates; either one can stop a route."""
    payload = make_response("FIX_PERMISSIONS", 0.52, 0.95)
    payload["answers"][QUESTION_KEY]["probabilities"]["REAUTHENTICATE"] = 0.48
    payload["answers"][QUESTION_KEY]["probabilities"].pop("ENGINEER_REVIEW", None)

    class TightProvider:
        mode = "fixture"

        def classify(self, state, event_id):
            from syncroute.jev_client import parse_answer

            return parse_answer(payload, is_fixture=True)

    decision = route_event(make_event(error_message="Access denied."), TightProvider(), now=NOW)

    assert decision.final_route is Route.ENGINEER_REVIEW
    assert decision.fallback_reason == "margin_below_threshold"


def test_explicit_model_review_is_not_recorded_as_a_fallback():
    provider = FakeProvider("ENGINEER_REVIEW", probability=0.96, confidence=0.95,)
    decision = route_event(make_event(error_message="Unparseable envelope."), provider, now=NOW)

    assert decision.final_route is Route.ENGINEER_REVIEW
    assert decision.source is DecisionSource.MODEL_FIXTURE
    assert decision.is_fallback_review is False, "an explicit choice is not an uncertainty fallback"


def test_api_error_produces_a_labelled_review_not_a_silent_mock():
    provider = FailingProvider(JevTransientError("service overloaded"))
    decision = route_event(make_event(error_message="Unfamiliar failure."), provider, now=NOW)

    assert decision.final_route is Route.ENGINEER_REVIEW
    assert decision.source is DecisionSource.API_ERROR_FALLBACK
    assert decision.fallback_reason == "api_error:JevTransientError"
    assert decision.answer is None
    assert decision.model_called is True


def test_auth_error_is_distinguished_from_a_transient_error():
    provider = FailingProvider(JevAuthError("bad key"))
    decision = route_event(make_event(error_message="Unfamiliar failure."), provider, now=NOW)

    assert decision.fallback_reason == "api_auth_error"
    assert decision.final_route is Route.ENGINEER_REVIEW


def test_invalid_payload_is_rejected_rather_than_coerced():
    provider = FailingProvider(JevInvalidResponse("probabilities summed to 0.4"))
    decision = route_event(make_event(error_message="Unfamiliar failure."), provider, now=NOW)

    assert decision.source is DecisionSource.API_ERROR_FALLBACK
    assert decision.final_route is Route.ENGINEER_REVIEW


def test_transient_status_retries_once_then_raises():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(429, json={"error": "rate limited"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = LiveJevProvider(api_key="test", client=client, sleep=lambda s: None)

    with pytest.raises(JevTransientError):
        provider.classify({"state": "x"}, "evt_1")

    assert calls["n"] == 2, "exactly one bounded retry"


def test_authentication_failure_is_never_retried():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"error": "unauthorized"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = LiveJevProvider(api_key="bad", client=client, sleep=lambda s: None)

    with pytest.raises(JevAuthError):
        provider.classify({"state": "x"}, "evt_1")

    assert calls["n"] == 1, "an auth failure must not be retried"


def test_retry_then_success_records_the_retry_count():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(529, json={"error": "overloaded"})
        return httpx.Response(200, json=make_response("REAUTHENTICATE", 0.9, 0.9))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = LiveJevProvider(api_key="test", client=client, sleep=lambda s: None)

    answer = provider.classify({"state": "x"}, "evt_1")

    assert answer.choice is Route.REAUTHENTICATE
    assert answer.retry_count == 1
    assert answer.latency_ms is not None, "latency must include the retry's contribution"
