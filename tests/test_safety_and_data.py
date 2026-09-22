"""Safety, persistence and dataset-integrity tests.

These cover the properties that make the numbers trustworthy rather than the
routing logic itself: secrets do not escape, labels do not leak into prompts,
reruns do not duplicate actions, and splits are actually disjoint.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import NOW, FakeProvider, make_event

from syncroute.evaluation import DATA_DIR, load_dataset
from syncroute.jev_client import FixtureProvider, JevUnavailable, QUESTION_KEY, build_state
from syncroute.models import DecisionSource, Route, SyncFailureEvent
from syncroute.policy import derive_policy_facts
from syncroute.router import RouterConfig, new_run_id, route_event
from syncroute.sanitize import sanitize_event
from syncroute.storage import Storage
from syncroute.workflows import (
    WorkflowTransitionError,
    approve,
    complete_task,
    mark_recovered,
    plan_workflow,
)

SECRET = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


# ------------------------------------------------------------------ redaction


def test_secrets_are_redacted_before_the_api_call():
    provider = FakeProvider("REAUTHENTICATE")
    event = make_event(
        error_message=f"Auth failed. Authorization: Bearer {SECRET} was rejected."
    )

    decision = route_event(event, provider, now=NOW)

    assert provider.calls, "the provider should have been called"
    payload = json.dumps(provider.calls[0])
    assert SECRET not in payload, "a secret reached the model payload"
    assert "REDACTED" in payload
    assert decision.redactions, "the redaction should be recorded on the decision"


def test_secrets_are_redacted_before_the_database_write(tmp_path: Path):
    storage = Storage(tmp_path / "t.db")
    event = make_event(error_message=f"token={SECRET} rejected")

    clean, labels = sanitize_event(event)
    storage.save_event(clean)
    stored = storage.get_event(event.event_id)

    assert labels, "the pattern should have matched"
    assert SECRET not in stored.error_message
    raw = (tmp_path / "t.db").read_bytes()
    assert SECRET.encode() not in raw, "the secret is on disk"
    storage.close()


def test_original_error_text_survives_redaction():
    """Redaction removes secret shapes, not the operator's ability to read the error."""
    event = make_event(error_message=f"Connection to db failed: password={SECRET}")
    clean, _ = sanitize_event(event)

    assert "Connection to db failed" in clean.error_message
    assert "password=" in clean.error_message


# --------------------------------------------------------------- label safety


def test_evaluation_labels_cannot_enter_the_model_payload():
    """The event model has no label field, and the built state carries no label."""
    row = load_dataset("heldout")[0]

    assert not hasattr(row.event, "expected_route")
    with pytest.raises(Exception):
        SyncFailureEvent.model_validate(
            {**json.loads(row.event.model_dump_json()), "expected_route": "RETRY_LATER"}
        )

    facts = derive_policy_facts(row.event)
    payload = json.dumps(build_state(row.event, facts))
    for label_value in (row.meta.expected_route.value, row.meta.family_id,
                        row.meta.label_rationale):
        assert label_value not in payload, f"{label_value!r} leaked into the model state"


def test_fixture_provider_never_reads_expected_route():
    """The fixture file itself must contain no labels to read."""
    fixtures = json.loads((DATA_DIR / "fixture_responses.json").read_text())
    blob = json.dumps(fixtures)

    assert "expected_route" not in blob
    assert "family_id" not in blob
    assert "label_rationale" not in blob


# ------------------------------------------------------------------- datasets


def test_family_splits_are_disjoint():
    dev = {row.meta.family_id for row in load_dataset("development")}
    held = {row.meta.family_id for row in load_dataset("heldout")}

    assert dev and held
    assert not (dev & held), "a family appears in both splits"


def test_every_route_appears_in_both_splits():
    for split in ("development", "heldout"):
        routes = {row.meta.expected_route for row in load_dataset(split)}
        assert routes == set(Route), f"{split} is missing {set(Route) - routes}"


def test_all_labels_are_marked_unreviewed_and_synthetic():
    for split in ("development", "heldout"):
        for row in load_dataset(split):
            assert row.meta.synthetic is True
            assert row.meta.human_reviewed is False, (
                "a label claims human review; flip this only after an actual review"
            )


# ------------------------------------------------------------- fixture honesty


def test_fixture_answers_are_labelled_and_carry_no_latency():
    provider = FixtureProvider(path=DATA_DIR / "fixture_responses.json")
    event = SyncFailureEvent.model_validate(
        json.loads((DATA_DIR / "demo_scenarios.json").read_text())["scenarios"][1]["event"]
    )
    facts = derive_policy_facts(event)

    answer = provider.classify(build_state(event, facts), event.event_id)

    assert answer.is_fixture is True
    assert answer.latency_ms is None, "a fixture must not report a latency measurement"


def test_unknown_event_in_fixture_mode_is_marked_unavailable():
    provider = FixtureProvider(fixtures={"responses": {}})
    decision = route_event(make_event(error_message="Never seen before."), provider, now=NOW)

    assert decision.final_route is Route.ENGINEER_REVIEW
    assert decision.source is DecisionSource.CLASSIFICATION_UNAVAILABLE
    assert decision.model_called is False
    assert "TYPESAFE_API_KEY" in decision.explanation


# ------------------------------------------------------------------ workflows


def test_resync_cannot_advance_without_approval():
    event = make_event(
        error_message="Saved replication offset predates the oldest retained log segment",
        replication_position_state="expired",
    )
    decision = route_event(event, FakeProvider("RETRY_LATER"), now=NOW)
    plan = plan_workflow(decision, event, now=NOW)

    assert decision.final_route is Route.REVIEW_RESYNC
    assert plan.requires_approval is True
    assert plan.approved is False

    with pytest.raises(WorkflowTransitionError):
        complete_task(plan)
    with pytest.raises(WorkflowTransitionError):
        mark_recovered(plan, declared_by="test")

    assert approve(plan).approved is True


def test_recovery_is_never_inferred_from_the_route():
    """Only an explicit action reaches SIMULATED_RECOVERED."""
    event = make_event(error_message="Too many requests.", http_status=429, retry_after="30")
    decision = route_event(event, None, now=NOW)
    plan = plan_workflow(decision, event, now=NOW)

    assert plan.status.value != "SIMULATED_RECOVERED"
    assert mark_recovered(plan, declared_by="operator").status.value == "SIMULATED_RECOVERED"


def test_retry_uses_deterministic_backoff_when_injected():
    import random

    event = make_event(error_message="Service unavailable.", http_status=503, attempt_number=2)
    decision = route_event(event, None, now=NOW)

    first = plan_workflow(decision, event, now=NOW, rng=random.Random(1))
    second = plan_workflow(decision, event, now=NOW, rng=random.Random(1))

    assert first.next_attempt_at == second.next_attempt_at, "seeded rng must be reproducible"
    assert first.next_attempt_at > NOW


# ---------------------------------------------------------------- persistence


def test_duplicate_submission_does_not_duplicate_decisions(tmp_path: Path):
    storage = Storage(tmp_path / "t.db")
    run_id = new_run_id("test")
    event = make_event(error_message="Too many requests.", http_status=429)
    decision = route_event(event, None, run_id=run_id, now=NOW)

    assert storage.record_decision(decision) is True
    assert storage.record_decision(decision) is False, "a rerun must not duplicate a decision"
    assert len(storage.list_decisions(run_id)) == 1

    plan = plan_workflow(decision, event, now=NOW)
    storage.save_workflow(run_id, event.event_id, plan)
    storage.save_workflow(run_id, event.event_id, plan)
    rows = storage._conn.execute("SELECT COUNT(*) c FROM workflow_runs").fetchone()["c"]
    assert rows == 1, "a rerun must not duplicate a workflow action"
    storage.close()


def test_configuration_change_creates_a_new_run_not_a_rewrite(tmp_path: Path):
    storage = Storage(tmp_path / "t.db")
    event = make_event(error_message="Access denied.", http_status=403)

    strict = route_event(event, FakeProvider("FIX_PERMISSIONS", 0.9, 0.7),
                         RouterConfig(confidence_threshold=0.8), run_id="run_a", now=NOW)
    loose = route_event(event, FakeProvider("FIX_PERMISSIONS", 0.9, 0.7),
                        RouterConfig(confidence_threshold=0.5), run_id="run_b", now=NOW)
    storage.record_decision(strict)
    storage.record_decision(loose)

    assert strict.final_route is Route.ENGINEER_REVIEW
    assert loose.final_route is Route.FIX_PERMISSIONS
    assert storage.get_decision("run_a", event.event_id).final_route is Route.ENGINEER_REVIEW, (
        "history was rewritten by a later configuration"
    )
    storage.close()


def test_feedback_does_not_alter_the_decision(tmp_path: Path):
    storage = Storage(tmp_path / "t.db")
    event = make_event(error_message="Too many requests.", http_status=429)
    decision = route_event(event, None, run_id="run_a", now=NOW)
    storage.record_decision(decision)

    storage.add_feedback("run_a", event.event_id, "reviewer", "ENGINEER_REVIEW", "disagree")

    assert storage.get_decision("run_a", event.event_id).final_route is Route.RETRY_LATER
    assert len(storage.list_feedback(event.event_id)) == 1
    storage.close()
