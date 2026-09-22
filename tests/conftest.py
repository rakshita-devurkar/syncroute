"""Shared fixtures. Every provider here is deterministic and offline."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

import pytest

from syncroute.jev_client import QUESTION_KEY, JevError, parse_answer
from syncroute.models import ModelAnswer, Route, SyncFailureEvent

NOW = datetime(2026, 9, 22, 12, 0, 0, tzinfo=timezone.utc)


def make_event(**overrides: Any) -> SyncFailureEvent:
    base: dict[str, Any] = {
        "event_id": "evt_test",
        "incident_id": "inc_test",
        "connector_id": "conn_test",
        "provider": "ExampleProvider",
        "connector_type": "saas_api",
        "occurred_at": NOW,
        "failure_stage": "fetch_records",
        "operation": "read records",
        "error_message": "Something went wrong.",
        "attempt_number": 1,
        "retry_budget_remaining": 3,
        "consecutive_failure_count": 1,
    }
    base.update(overrides)
    return SyncFailureEvent.model_validate(base)


def make_response(
    choice: str, probability: float, confidence: float, runner_up: str = "ENGINEER_REVIEW"
) -> dict[str, Any]:
    remainder = round(1.0 - probability, 4)
    if runner_up == choice:
        # Avoid collapsing both entries onto one key.
        runner_up = "REVIEW_CONFIGURATION" if choice != "REVIEW_CONFIGURATION" else "RETRY_LATER"
    probabilities = {choice: probability}
    if remainder > 0:
        probabilities[runner_up] = remainder
    return {
        "model": "jev-1.13.0-test",
        "answers": {
            QUESTION_KEY: {
                "type": "choice",
                "choice": choice,
                "probabilities": probabilities,
                "confidence": confidence,
            }
        },
        "usage": {"input_tokens": 300, "output_tokens": 30},
    }


class FakeProvider:
    """Returns a fixed answer and records the states it was given."""

    def __init__(
        self,
        choice: str,
        probability: float = 0.95,
        confidence: float = 0.95,
        mode: str = "fixture",
    ) -> None:
        self._payload = make_response(choice, probability, confidence)
        self.mode = mode
        self.calls: list[dict[str, Any]] = []

    def classify(self, state: dict[str, Any], event_id: str) -> ModelAnswer:
        self.calls.append(state)
        # Stay consistent with the declared mode so the router labels the
        # decision source the same way it would in the real app.
        return parse_answer(self._payload, is_fixture=self.mode == "fixture")


class FailingProvider:
    """Raises a chosen error. Used to prove failures are labelled, not mocked."""

    mode = "live"

    def __init__(self, error: Optional[Exception] = None) -> None:
        self.error = error or JevError("simulated service failure")
        self.calls = 0

    def classify(self, state: dict[str, Any], event_id: str) -> ModelAnswer:
        self.calls += 1
        raise self.error


@pytest.fixture
def now() -> datetime:
    return NOW
