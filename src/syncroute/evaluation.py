"""Evaluation: run both systems over identical events and report honestly.

Rules this module enforces so the numbers mean something:

* Both systems see exactly the same events.
* Labels never reach the model. They live on ``LabeledEvent.meta`` and are only
  read when scoring, after every decision is made.
* A metric with an empty denominator is reported as ``None`` and rendered N/A,
  never as 0.0.
* Token usage and cost are reported only when the API actually returned usage.
  Cost additionally requires a configured price and the date it was taken from.
* Fixture runs are tagged and must not be presented as measured Jev performance.

Threshold sweeps replay cached answers through the real pipeline rather than
recomputing gates by hand, so a sweep costs no extra API calls and cannot drift
from production semantics.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from .jev_client import (
    DEFAULT_MODEL,
    PROMPT_VERSION,
    JevProvider,
    JevUnavailable,
)
from .models import (
    ALL_ROUTES,
    DecisionSource,
    LabeledEvent,
    ModelAnswer,
    Route,
    RoutingDecision,
)
from .policy import POLICY_VERSION
from .router import PIPELINE_VERSION, RouterConfig, new_run_id, route_event
from .rules import RULES_VERSION

DATA_DIR = Path(__file__).resolve().parents[2] / "data"

EVAL_VERSION = "eval-2026.09.1"


# --------------------------------------------------------------------- loading


def load_dataset(split: str, data_dir: Path = DATA_DIR) -> list[LabeledEvent]:
    path = data_dir / f"{split}.jsonl"
    if not path.exists():
        raise FileNotFoundError(
            f"No dataset at {path}. Run: python scripts/build_dataset.py"
        )
    rows = [LabeledEvent.model_validate_json(line) for line in path.read_text().splitlines() if line.strip()]
    return rows


def dataset_hash(split: str, data_dir: Path = DATA_DIR) -> str:
    path = data_dir / f"{split}.jsonl"
    return hashlib.sha256(path.read_bytes()).hexdigest()


# -------------------------------------------------------------------- replay


class ReplayProvider:
    """Serves previously captured answers. Used for threshold sweeps.

    Replaying through the real pipeline keeps sweep results faithful to
    production semantics while costing nothing.
    """

    def __init__(self, answers: dict[str, ModelAnswer], mode: str = "live") -> None:
        self._answers = answers
        self.mode = mode

    def classify(self, state: dict[str, Any], event_id: str) -> ModelAnswer:
        answer = self._answers.get(event_id)
        if answer is None:
            raise JevUnavailable("No cached answer for this event in the replay set.")
        return answer


# --------------------------------------------------------------------- running


@dataclass
class ScoredDecision:
    labeled: LabeledEvent
    decision: RoutingDecision

    @property
    def expected(self) -> Route:
        return self.labeled.meta.expected_route

    @property
    def predicted(self) -> Route:
        return self.decision.final_route

    @property
    def correct(self) -> bool:
        return self.expected is self.predicted


def run_system(
    events: Sequence[LabeledEvent],
    provider: Optional[JevProvider],
    config: RouterConfig,
    *,
    run_id: Optional[str] = None,
    now: Optional[datetime] = None,
) -> list[ScoredDecision]:
    """Route every event with one configuration. Labels are not consulted."""
    run_id = run_id or new_run_id("eval")
    scored: list[ScoredDecision] = []
    for labeled in events:
        decision = route_event(
            labeled.event, provider, config, run_id=run_id, now=now
        )
        scored.append(ScoredDecision(labeled=labeled, decision=decision))
    return scored


def collect_answers(scored: Iterable[ScoredDecision]) -> dict[str, ModelAnswer]:
    return {
        s.decision.event_id: s.decision.answer
        for s in scored
        if s.decision.answer is not None
    }


# --------------------------------------------------------------------- metrics


def _percentile(values: Sequence[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct
    low, high = int(rank), min(int(rank) + 1, len(ordered) - 1)
    weight = rank - low
    return ordered[low] * (1 - weight) + ordered[high] * weight


def _safe_div(numerator: float, denominator: float) -> Optional[float]:
    """Undefined metrics are None, never 0.0."""
    if denominator == 0:
        return None
    return numerator / denominator


def confusion_matrix(scored: Sequence[ScoredDecision]) -> dict[str, dict[str, int]]:
    matrix = {e.value: {p.value: 0 for p in ALL_ROUTES} for e in ALL_ROUTES}
    for s in scored:
        matrix[s.expected.value][s.predicted.value] += 1
    return matrix


def per_route_metrics(scored: Sequence[ScoredDecision]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for route in ALL_ROUTES:
        tp = sum(1 for s in scored if s.expected is route and s.predicted is route)
        fp = sum(1 for s in scored if s.expected is not route and s.predicted is route)
        fn = sum(1 for s in scored if s.expected is route and s.predicted is not route)
        precision = _safe_div(tp, tp + fp)
        recall = _safe_div(tp, tp + fn)
        if precision is None or recall is None or (precision + recall) == 0:
            f1 = None
        else:
            f1 = 2 * precision * recall / (precision + recall)
        out[route.value] = {
            "support": tp + fn,
            "predicted": tp + fp,
            "true_positives": tp,
            "precision": precision,
            "recall": recall,
            "f1": f1,
        }
    return out


def compute_metrics(
    scored: Sequence[ScoredDecision],
    *,
    label: str,
    unmatched_event_ids: Optional[set[str]] = None,
    input_price_per_mtok: Optional[float] = None,
    price_as_of: Optional[str] = None,
) -> dict[str, Any]:
    """Everything the evaluation view displays, for one system."""
    total = len(scored)
    correct = sum(1 for s in scored if s.correct)

    per_route = per_route_metrics(scored)
    f1s = [m["f1"] for m in per_route.values() if m["f1"] is not None]
    macro_f1 = statistics.fmean(f1s) if f1s else None

    non_review = [s for s in scored if s.predicted is not Route.ENGINEER_REVIEW]
    incorrect_non_review = [s for s in non_review if not s.correct]

    review_expected = [s for s in scored if s.expected is Route.ENGINEER_REVIEW]
    review_caught = [s for s in review_expected if s.predicted is Route.ENGINEER_REVIEW]

    sources = [s.decision.source for s in scored]
    fixture_used = any(
        s.decision.answer is not None and s.decision.answer.is_fixture for s in scored
    )

    end_to_end = [s.decision.total_latency_ms for s in scored]
    model_latencies = [
        s.decision.answer.latency_ms
        for s in scored
        if s.decision.answer is not None and s.decision.answer.latency_ms is not None
    ]

    usage_rows = [
        s.decision.answer
        for s in scored
        if s.decision.answer is not None and s.decision.answer.input_tokens is not None
    ]
    total_input_tokens = sum(a.input_tokens or 0 for a in usage_rows) if usage_rows else None
    total_output_tokens = (
        sum(a.output_tokens or 0 for a in usage_rows if a.output_tokens is not None)
        if usage_rows
        else None
    )

    cost: Optional[float] = None
    cost_note = "Unavailable: the API returned no token usage for this run."
    if total_input_tokens is None:
        pass
    elif input_price_per_mtok is None:
        cost_note = "Unavailable: no input price is configured."
    elif fixture_used:
        cost_note = "Not applicable: fixture responses carry no real token usage."
    else:
        cost = (total_input_tokens / 1_000_000.0) * input_price_per_mtok
        cost_note = (
            f"Estimated at ${input_price_per_mtok}/M input tokens"
            + (f" (price as of {price_as_of})." if price_as_of else ".")
            + " Output tokens are documented as free."
        )

    metrics: dict[str, Any] = {
        "label": label,
        "events": total,
        "fixture_responses_used": fixture_used,
        "exact_accuracy": _safe_div(correct, total),
        "correct": correct,
        "macro_f1": macro_f1,
        "per_route": per_route,
        "confusion_matrix": confusion_matrix(scored),
        "non_review_coverage": {
            "value": _safe_div(len(non_review), total),
            "numerator": len(non_review),
            "denominator": total,
            "meaning": "Share of events assigned a workflow other than engineering review. "
                       "This is workflow assignment, not automatic remediation.",
        },
        "incorrect_non_review_rate": {
            "value": _safe_div(len(incorrect_non_review), len(non_review)),
            "numerator": len(incorrect_non_review),
            "denominator": len(non_review),
            "meaning": "Share of non-review assignments that went to the wrong workflow.",
        },
        "engineer_review_recall": {
            "value": _safe_div(len(review_caught), len(review_expected)),
            "numerator": len(review_caught),
            "denominator": len(review_expected),
        },
        "decision_sources": {
            source.value: sources.count(source) for source in DecisionSource if source in sources
        },
        "counts": {
            "rule_hits": sum(1 for s in sources if s is DecisionSource.DETERMINISTIC_RULE),
            "model_calls": sum(1 for s in scored if s.decision.model_called),
            "uncertainty_fallbacks": sum(
                1 for s in sources if s is DecisionSource.UNCERTAINTY_FALLBACK
            ),
            "policy_escalations": sum(
                1 for s in sources if s is DecisionSource.POLICY_ESCALATION
            ),
            "post_route_policy_overrides": sum(
                1 for s in sources if s is DecisionSource.POST_ROUTE_POLICY
            ),
            "api_error_fallbacks": sum(
                1 for s in sources if s is DecisionSource.API_ERROR_FALLBACK
            ),
            "classification_unavailable": sum(
                1 for s in sources if s is DecisionSource.CLASSIFICATION_UNAVAILABLE
            ),
        },
        "latency_ms": {
            "end_to_end_p50": _percentile(end_to_end, 0.50),
            "end_to_end_p95": _percentile(end_to_end, 0.95),
            "end_to_end_samples": len(end_to_end),
            "model_call_p50": _percentile(model_latencies, 0.50),
            "model_call_p95": _percentile(model_latencies, 0.95),
            "model_call_samples": len(model_latencies),
            "note": (
                "Fixture responses have no measured latency and are excluded from model-call "
                "figures." if fixture_used else
                "End-to-end figures include API error fallbacks, which are also broken out "
                "under counts."
            ),
        },
        "usage": {
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "responses_with_usage": len(usage_rows),
            "estimated_cost_usd": cost,
            "cost_note": cost_note,
        },
    }

    if unmatched_event_ids is not None:
        subset = [s for s in scored if s.decision.event_id in unmatched_event_ids]
        subset_non_review = [s for s in subset if s.predicted is not Route.ENGINEER_REVIEW]
        subset_incorrect = [s for s in subset_non_review if not s.correct]
        metrics["rules_unmatched_subset"] = {
            "events": len(subset),
            "exact_accuracy": _safe_div(sum(1 for s in subset if s.correct), len(subset)),
            "non_review_coverage": {
                "value": _safe_div(len(subset_non_review), len(subset)),
                "numerator": len(subset_non_review),
                "denominator": len(subset),
            },
            "incorrect_non_review_rate": {
                "value": _safe_div(len(subset_incorrect), len(subset_non_review)),
                "numerator": len(subset_incorrect),
                "denominator": len(subset_non_review),
            },
            "meaning": "The cases the deterministic rules could not decide. This is where a "
                       "classifier can add or destroy value; overall figures hide it.",
        }

    return metrics


def unmatched_ids(baseline: Sequence[ScoredDecision]) -> set[str]:
    """Event ids the rules-only baseline could not decide."""
    return {
        s.decision.event_id
        for s in baseline
        if s.decision.source is DecisionSource.BASELINE_UNMATCHED
    }


# ---------------------------------------------------------------------- sweeps


def threshold_sweep(
    events: Sequence[LabeledEvent],
    answers: dict[str, ModelAnswer],
    thresholds: Sequence[tuple[float, float]],
    *,
    mode: str = "live",
) -> list[dict[str, Any]]:
    """Coverage/error tradeoff, replayed from cached answers at no extra cost."""
    provider = ReplayProvider(answers, mode=mode)
    rows: list[dict[str, Any]] = []
    for confidence, margin in thresholds:
        config = RouterConfig(confidence_threshold=confidence, margin_threshold=margin)
        scored = run_system(events, provider, config)
        non_review = [s for s in scored if s.predicted is not Route.ENGINEER_REVIEW]
        incorrect = [s for s in non_review if not s.correct]
        rows.append(
            {
                "confidence_threshold": confidence,
                "margin_threshold": margin,
                "exact_accuracy": _safe_div(sum(1 for s in scored if s.correct), len(scored)),
                "non_review_coverage": _safe_div(len(non_review), len(scored)),
                "incorrect_non_review_rate": _safe_div(len(incorrect), len(non_review)),
                "incorrect_non_review_count": len(incorrect),
                "non_review_count": len(non_review),
            }
        )
    return rows


# -------------------------------------------------------------------- manifest


@dataclass
class ExperimentManifest:
    run_id: str
    split: str
    mode: str
    dataset_hash: str
    dataset_events: int
    model: str = DEFAULT_MODEL
    model_version_returned: Optional[str] = None
    prompt_version: str = PROMPT_VERSION
    rules_version: str = RULES_VERSION
    policy_version: str = POLICY_VERSION
    pipeline_version: str = PIPELINE_VERSION
    eval_version: str = EVAL_VERSION
    confidence_threshold: float = 0.80
    margin_threshold: float = 0.15
    run_timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "split": self.split,
            "mode": self.mode,
            "dataset_hash": self.dataset_hash,
            "dataset_events": self.dataset_events,
            "model_requested": self.model,
            "model_version_returned": self.model_version_returned,
            "prompt_version": self.prompt_version,
            "rules_version": self.rules_version,
            "policy_version": self.policy_version,
            "pipeline_version": self.pipeline_version,
            "eval_version": self.eval_version,
            "thresholds": {
                "confidence": self.confidence_threshold,
                "margin": self.margin_threshold,
            },
            "run_timestamp": self.run_timestamp,
            "notes": self.notes,
        }


# ---------------------------------------------------------------------- export


def export_results(
    out_dir: Path,
    run_id: str,
    manifest: dict[str, Any],
    baseline: dict[str, Any],
    hybrid: dict[str, Any],
    scored_pairs: dict[str, Sequence[ScoredDecision]],
    sweep: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Path]:
    """Write JSON and CSV, including raw sanitized model responses."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    payload = {
        "manifest": manifest,
        "rules_only": baseline,
        "hybrid": hybrid,
        "threshold_sweep": sweep,
    }
    json_path = out_dir / f"{run_id}_results.json"
    json_path.write_text(json.dumps(payload, indent=2, default=str))
    written["json"] = json_path

    csv_path = out_dir / f"{run_id}_decisions.csv"
    header = [
        "system", "event_id", "family_id", "split", "expected_route", "proposed_route",
        "final_route", "correct", "source", "rule_id", "policy_id", "fallback_reason",
        "confidence", "selected_probability", "top_two_margin", "model_version",
        "is_fixture", "model_called", "total_latency_ms", "model_latency_ms",
        "input_tokens", "output_tokens",
    ]
    lines = [",".join(header)]
    for system, scored in scored_pairs.items():
        for s in scored:
            a = s.decision.answer
            row = [
                system,
                s.decision.event_id,
                s.labeled.meta.family_id,
                s.labeled.meta.split,
                s.expected.value,
                s.decision.proposed_route.value if s.decision.proposed_route else "",
                s.predicted.value,
                str(s.correct),
                s.decision.source.value,
                s.decision.rule_id or "",
                s.decision.policy_id or "",
                s.decision.fallback_reason or "",
                f"{a.confidence:.4f}" if a else "",
                f"{a.selected_probability:.4f}" if a else "",
                f"{a.top_two_margin:.4f}" if a else "",
                a.model_version if a and a.model_version else "",
                str(a.is_fixture) if a else "",
                str(s.decision.model_called),
                f"{s.decision.total_latency_ms:.2f}",
                f"{a.latency_ms:.2f}" if a and a.latency_ms is not None else "",
                str(a.input_tokens) if a and a.input_tokens is not None else "",
                str(a.output_tokens) if a and a.output_tokens is not None else "",
            ]
            lines.append(",".join(f'"{v}"' if "," in str(v) else str(v) for v in row))
    csv_path.write_text("\n".join(lines) + "\n")
    written["csv"] = csv_path

    raw_path = out_dir / f"{run_id}_raw_responses.json"
    raw = {
        s.decision.event_id: s.decision.answer.raw_response
        for s in scored_pairs.get("hybrid", [])
        if s.decision.answer is not None and s.decision.answer.raw_response is not None
    }
    raw_path.write_text(json.dumps(raw, indent=2, default=str))
    written["raw"] = raw_path

    return written
