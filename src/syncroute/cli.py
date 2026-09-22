"""Command line entry point.

    python -m syncroute.cli evaluate --split development --mode live
    python -m syncroute.cli route --scenario demo_429
    python -m syncroute.cli sweep --split development
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

from .config import get_settings
from .evaluation import (
    DATA_DIR,
    ExperimentManifest,
    ReplayProvider,
    collect_answers,
    compute_metrics,
    dataset_hash,
    export_results,
    load_dataset,
    run_system,
    threshold_sweep,
    unmatched_ids,
)
from .jev_client import JevAuthError, make_provider
from .models import Route, SyncFailureEvent
from .router import RouterConfig, new_run_id, route_event
from .storage import Storage
from .workflows import plan_workflow

FIXTURES_PATH = DATA_DIR / "fixture_responses.json"
SCENARIOS_PATH = DATA_DIR / "demo_scenarios.json"
RESULTS_DIR = Path("results")

SWEEP_GRID = [
    (0.50, 0.00), (0.60, 0.05), (0.70, 0.10), (0.75, 0.10),
    (0.80, 0.15), (0.85, 0.20), (0.90, 0.25), (0.95, 0.35),
]


def _fmt(value: Optional[float], digits: int = 3, pct: bool = False) -> str:
    if value is None:
        return "N/A"
    if pct:
        return f"{value * 100:.1f}%"
    return f"{value:.{digits}f}"


def build_provider(mode: str, settings) -> Any:
    if mode == "live":
        return make_provider("live", api_key=settings.api_key, model=settings.model,
                             base_url=settings.base_url)
    return make_provider("fixture", fixtures_path=FIXTURES_PATH)


def _print_system(metrics: dict[str, Any]) -> None:
    print(f"\n  {metrics['label']}  ({metrics['events']} events)")
    print(f"    exact accuracy          {_fmt(metrics['exact_accuracy'], pct=True)}"
          f"   ({metrics['correct']}/{metrics['events']})")
    print(f"    macro F1 (7 routes)     {_fmt(metrics['macro_f1'])}")
    cov = metrics["non_review_coverage"]
    print(f"    non-review coverage     {_fmt(cov['value'], pct=True)}"
          f"   ({cov['numerator']}/{cov['denominator']})")
    err = metrics["incorrect_non_review_rate"]
    print(f"    incorrect non-review    {_fmt(err['value'], pct=True)}"
          f"   ({err['numerator']}/{err['denominator']})")
    rec = metrics["engineer_review_recall"]
    print(f"    ENGINEER_REVIEW recall  {_fmt(rec['value'], pct=True)}"
          f"   ({rec['numerator']}/{rec['denominator']})")
    if "rules_unmatched_subset" in metrics:
        sub = metrics["rules_unmatched_subset"]
        print(f"    -- on the {sub['events']} rules-unmatched cases --")
        print(f"       accuracy             {_fmt(sub['exact_accuracy'], pct=True)}")
        print(f"       non-review coverage  {_fmt(sub['non_review_coverage']['value'], pct=True)}"
              f"   ({sub['non_review_coverage']['numerator']}/"
              f"{sub['non_review_coverage']['denominator']})")
        print(f"       incorrect non-review {_fmt(sub['incorrect_non_review_rate']['value'], pct=True)}"
              f"   ({sub['incorrect_non_review_rate']['numerator']}/"
              f"{sub['incorrect_non_review_rate']['denominator']})")
    c = metrics["counts"]
    print(f"    rule hits {c['rule_hits']} | model calls {c['model_calls']} | "
          f"uncertainty {c['uncertainty_fallbacks']} | policy {c['policy_escalations']} | "
          f"post-route {c['post_route_policy_overrides']} | api errors {c['api_error_fallbacks']}")
    lat = metrics["latency_ms"]
    print(f"    end-to-end p50/p95      {_fmt(lat['end_to_end_p50'], 1)} / "
          f"{_fmt(lat['end_to_end_p95'], 1)} ms  (n={lat['end_to_end_samples']})")
    print(f"    model call p50/p95      {_fmt(lat['model_call_p50'], 1)} / "
          f"{_fmt(lat['model_call_p95'], 1)} ms  (n={lat['model_call_samples']})")
    u = metrics["usage"]
    tokens = "unavailable" if u["input_tokens"] is None else f"{u['input_tokens']:,} in"
    cost = "N/A" if u["estimated_cost_usd"] is None else f"${u['estimated_cost_usd']:.6f}"
    print(f"    tokens {tokens} | cost {cost} - {u['cost_note']}")


def cmd_evaluate(args: argparse.Namespace) -> int:
    settings = get_settings()
    mode = args.mode or settings.default_mode
    if mode == "live" and not settings.has_api_key:
        print("Live mode needs TYPESAFE_API_KEY. Put it in .env, or pass --mode fixture.",
              file=sys.stderr)
        return 2

    events = load_dataset(args.split)
    run_id = new_run_id(f"eval_{args.split}")
    config = RouterConfig(
        confidence_threshold=args.confidence, margin_threshold=args.margin
    )

    print(f"\nSyncRoute evaluation | split={args.split} | mode={mode} | events={len(events)}")
    if mode == "fixture":
        print("FIXTURE MODE: canned responses. These are not measurements of Jev.")

    baseline_scored = run_system(events, None, RouterConfig(system="rules_only"), run_id=run_id)
    unmatched = unmatched_ids(baseline_scored)

    provider = build_provider(mode, settings)
    try:
        hybrid_scored = run_system(events, provider, config, run_id=run_id)
    except JevAuthError as exc:
        print(f"Authentication failed: {exc}", file=sys.stderr)
        return 2

    baseline_metrics = compute_metrics(
        baseline_scored, label="Rules only (baseline)", unmatched_event_ids=unmatched
    )
    hybrid_metrics = compute_metrics(
        hybrid_scored,
        label=f"Hybrid: rules + policy + Jev ({mode})",
        unmatched_event_ids=unmatched,
        input_price_per_mtok=settings.input_price_per_mtok,
        price_as_of=settings.price_as_of,
    )

    _print_system(baseline_metrics)
    _print_system(hybrid_metrics)

    returned_versions = {
        s.decision.answer.model_version
        for s in hybrid_scored
        if s.decision.answer and s.decision.answer.model_version
    }
    manifest = ExperimentManifest(
        run_id=run_id,
        split=args.split,
        mode=mode,
        dataset_hash=dataset_hash(args.split),
        dataset_events=len(events),
        model=settings.model,
        model_version_returned=", ".join(sorted(returned_versions)) or None,
        confidence_threshold=args.confidence,
        margin_threshold=args.margin,
        notes=args.notes or "",
    ).to_dict()

    sweep = None
    if args.sweep:
        answers = collect_answers(hybrid_scored)
        sweep = threshold_sweep(events, answers, SWEEP_GRID, mode=mode)
        print("\n  Coverage / error tradeoff (replayed from the same answers)")
        print("    conf  margin   accuracy  coverage  incorrect-non-review")
        for row in sweep:
            print(f"    {row['confidence_threshold']:.2f}  {row['margin_threshold']:.2f}"
                  f"     {_fmt(row['exact_accuracy'], pct=True):>7}"
                  f"   {_fmt(row['non_review_coverage'], pct=True):>7}"
                  f"   {_fmt(row['incorrect_non_review_rate'], pct=True):>7}"
                  f"  ({row['incorrect_non_review_count']}/{row['non_review_count']})")

    written = export_results(
        args.output, run_id, manifest, baseline_metrics, hybrid_metrics,
        {"rules_only": baseline_scored, "hybrid": hybrid_scored}, sweep,
    )
    storage = Storage(settings.db_path)
    storage.record_evaluation_run(
        run_id, args.split, mode, manifest,
        {"rules_only": baseline_metrics, "hybrid": hybrid_metrics},
    )
    for scored in hybrid_scored:
        storage.save_event(scored.labeled.event)
        storage.record_decision(scored.decision)
    storage.close()

    print(f"\n  manifest: dataset {manifest['dataset_hash'][:16]} | "
          f"prompt {manifest['prompt_version']} | rules {manifest['rules_version']} | "
          f"model returned {manifest['model_version_returned']}")
    for kind, path in written.items():
        print(f"  wrote {kind}: {path}")
    return 0


def cmd_sweep(args: argparse.Namespace) -> int:
    args.sweep = True
    return cmd_evaluate(args)


def cmd_route(args: argparse.Namespace) -> int:
    settings = get_settings()
    mode = args.mode or settings.default_mode

    if args.scenario:
        scenarios = json.loads(SCENARIOS_PATH.read_text())["scenarios"]
        match = next((s for s in scenarios if s["scenario_id"] == args.scenario), None)
        if match is None:
            print(f"Unknown scenario {args.scenario!r}. Available: "
                  f"{', '.join(s['scenario_id'] for s in scenarios)}", file=sys.stderr)
            return 2
        event = SyncFailureEvent.model_validate(match["event"])
    elif args.event_file:
        event = SyncFailureEvent.model_validate_json(Path(args.event_file).read_text())
    else:
        print("Pass --scenario or --event-file.", file=sys.stderr)
        return 2

    provider = build_provider(mode, settings)
    decision = route_event(event, provider, RouterConfig(), run_id=new_run_id("cli"))
    plan = plan_workflow(decision, event)

    print(f"\nevent          {event.event_id}  ({event.provider}, {event.connector_type.value})")
    print(f"error          {event.error_message[:110]}")
    print(f"proposed       {decision.proposed_route.value if decision.proposed_route else '-'}")
    print(f"final          {decision.final_route.value}")
    print(f"source         {decision.source.value}"
          f"{'  [FIXTURE]' if decision.answer and decision.answer.is_fixture else ''}")
    if decision.rule_id:
        print(f"rule           {decision.rule_id}")
    if decision.policy_id:
        print(f"policy         {decision.policy_id}")
    if decision.answer:
        a = decision.answer
        print(f"confidence     {a.confidence:.2f} | selected p {a.selected_probability:.2f} "
              f"| margin {a.top_two_margin:.2f} | model {a.model_version}")
    if decision.fallback_reason:
        print(f"fallback       {decision.fallback_reason}")
    print(f"redactions     {', '.join(decision.redactions) or 'none matched'}")
    print(f"\nworkflow       {plan.status.value} (simulated)")
    print(f"               {plan.summary}")
    for step in plan.steps:
        print(f"   [{'x' if step.completed else ' '}] {step.label}: {step.detail}")
    print(f"\n{decision.explanation}")
    return 0


def cmd_init_db(args: argparse.Namespace) -> int:
    settings = get_settings()
    storage = Storage(settings.db_path)
    storage.close()
    print(f"Initialised {settings.db_path}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="syncroute", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--mode", choices=["fixture", "live"], default=None,
                       help="Default: live when TYPESAFE_API_KEY is set, otherwise fixture.")

    ev = sub.add_parser("evaluate", help="Score rules-only and hybrid on the same events.")
    ev.add_argument("--split", choices=["development", "heldout"], default="development")
    ev.add_argument("--confidence", type=float, default=0.80)
    ev.add_argument("--margin", type=float, default=0.15)
    ev.add_argument("--output", default=str(RESULTS_DIR))
    ev.add_argument("--sweep", action="store_true", help="Also print the threshold tradeoff.")
    ev.add_argument("--notes", default="")
    add_common(ev)
    ev.set_defaults(func=cmd_evaluate)

    sw = sub.add_parser("sweep", help="Evaluate and always print the threshold tradeoff.")
    sw.add_argument("--split", choices=["development", "heldout"], default="development")
    sw.add_argument("--confidence", type=float, default=0.80)
    sw.add_argument("--margin", type=float, default=0.15)
    sw.add_argument("--output", default=str(RESULTS_DIR))
    sw.add_argument("--notes", default="")
    add_common(sw)
    sw.set_defaults(func=cmd_sweep)

    rt = sub.add_parser("route", help="Route a single scenario or JSON event.")
    rt.add_argument("--scenario")
    rt.add_argument("--event-file")
    add_common(rt)
    rt.set_defaults(func=cmd_route)

    db = sub.add_parser("init-db", help="Create the SQLite schema.")
    db.set_defaults(func=cmd_init_db)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
