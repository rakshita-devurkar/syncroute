"""SyncRoute: Streamlit UI.

Three views: a failure inbox, an event inspector and an evaluation report.

Everything displayed here is synthetic. Every recovery workflow is simulated.
The mode indicator in the sidebar says whether classifications came from the
live API or from saved fixtures, and fixture results are never presented as
measurements.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import streamlit as st

from syncroute.config import get_settings
from syncroute.evaluation import DATA_DIR, load_dataset
from syncroute.jev_client import JevAuthError, make_provider
from syncroute.models import (
    ALL_ROUTES,
    DecisionSource,
    Route,
    RoutingDecision,
    SyncFailureEvent,
    WorkflowStatus,
)
from syncroute.router import RouterConfig, new_run_id, route_event
from syncroute.sanitize import REDACTION_NOTICE
from syncroute.storage import Storage
from syncroute.workflows import (
    WorkflowTransitionError,
    approve,
    complete_task,
    mark_recovered,
    plan_workflow,
)

ROOT = Path(__file__).parent
SCENARIOS_PATH = DATA_DIR / "demo_scenarios.json"
FIXTURES_PATH = DATA_DIR / "fixture_responses.json"
RESULTS_DIR = ROOT / "results"

ROUTE_COLOURS = {
    Route.RETRY_LATER: "#3d7a99",
    Route.REAUTHENTICATE: "#8a5a9e",
    Route.FIX_PERMISSIONS: "#b07d3a",
    Route.REVIEW_CONFIGURATION: "#4a7c59",
    Route.CHECK_CONNECTIVITY: "#5a7d9a",
    Route.REVIEW_RESYNC: "#a85d5d",
    Route.ENGINEER_REVIEW: "#6b6b6b",
}

st.set_page_config(page_title="SyncRoute", page_icon="🔀", layout="wide")


# ----------------------------------------------------------------- state setup


@st.cache_data
def load_scenarios() -> list[dict[str, Any]]:
    return json.loads(SCENARIOS_PATH.read_text())["scenarios"]


@st.cache_resource
def get_storage(db_path: str) -> Storage:
    return Storage(db_path)


def settings():
    return get_settings(ROOT / ".env")


def init_state() -> None:
    if "run_id" not in st.session_state:
        st.session_state.run_id = new_run_id("ui")
    # event_id -> (decision, plan). Keyed so a Streamlit rerun cannot duplicate
    # a decision or replay a workflow action.
    st.session_state.setdefault("decisions", {})
    st.session_state.setdefault("selected_event", None)
    st.session_state.setdefault("incident_chain", [])


def build_provider(mode: str):
    cfg = settings()
    if mode == "live":
        return make_provider("live", api_key=cfg.api_key, model=cfg.model, base_url=cfg.base_url)
    return make_provider("fixture", fixtures_path=FIXTURES_PATH)


def process_event(
    event: SyncFailureEvent, mode: str, config: RouterConfig, *, force_new: bool = False
) -> tuple[RoutingDecision, Any]:
    """Route an event once per run. Repeat submissions return the stored result."""
    store = st.session_state.decisions
    if not force_new and event.event_id in store:
        return store[event.event_id]

    provider = build_provider(mode)
    decision = route_event(event, provider, config, run_id=st.session_state.run_id)
    plan = plan_workflow(decision, event)

    storage = get_storage(settings().db_path)
    storage.save_event(event)
    storage.record_decision(decision)
    storage.save_workflow(decision.run_id, event.event_id, plan)

    store[event.event_id] = (decision, plan)
    return decision, plan


def route_badge(route: Route) -> str:
    colour = ROUTE_COLOURS[route]
    return (
        f"<span style='background:{colour};color:#fff;padding:2px 9px;"
        f"border-radius:11px;font-size:0.78rem;font-weight:600;white-space:nowrap'>"
        f"{route.value}</span>"
    )


# ------------------------------------------------------------------- sidebar


def sidebar() -> tuple[str, RouterConfig]:
    cfg = settings()
    st.sidebar.title("SyncRoute")
    st.sidebar.caption(
        "Routes synthetic data-sync failures to simulated recovery workflows."
    )

    options = ["fixture"] + (["live"] if cfg.has_api_key else [])
    default = options.index(cfg.default_mode) if cfg.default_mode in options else 0
    mode = st.sidebar.radio(
        "Classification mode", options, index=default,
        format_func=lambda m: "Fixture (offline)" if m == "fixture" else "Live Jev API",
    )

    if mode == "live":
        st.sidebar.success(f"**LIVE JEV** · model `{cfg.model}`")
        st.sidebar.caption("Unmatched events are sent to the real API.")
    else:
        st.sidebar.warning("**FIXTURE MODE**")
        st.sidebar.caption(
            "Saved responses replayed offline. Fixture probabilities and timings are "
            "not measurements of Jev performance."
        )
    if not cfg.has_api_key:
        st.sidebar.caption(
            "No `TYPESAFE_API_KEY` found. Add one to `.env` to enable live mode."
        )

    st.sidebar.divider()
    st.sidebar.subheader("Uncertainty gate")
    st.sidebar.caption("Demonstration defaults, not calibrated guarantees.")
    confidence = st.sidebar.slider("Confidence threshold", 0.0, 1.0, 0.80, 0.05)
    margin = st.sidebar.slider("Top-two margin", 0.0, 1.0, 0.15, 0.05)
    if (confidence, margin) != (0.80, 0.15):
        st.sidebar.info("Changed from the frozen settings. New decisions only; history is immutable.")

    st.sidebar.divider()
    st.sidebar.error("**All data here is synthetic.** Every workflow is simulated: nothing is "
                     "reconnected, restarted, resynced or contacted.")
    st.sidebar.caption(f"Run `{st.session_state.run_id}`")
    if st.sidebar.button("Start a new run", use_container_width=True):
        st.session_state.run_id = new_run_id("ui")
        st.session_state.decisions = {}
        st.session_state.incident_chain = []
        st.rerun()

    return mode, RouterConfig(confidence_threshold=confidence, margin_threshold=margin)


# --------------------------------------------------------------------- inbox


def view_inbox(mode: str, config: RouterConfig) -> None:
    st.header("Failure inbox")
    st.caption("Synthetic connector failures. Route them to see the simulated next action.")

    scenarios = load_scenarios()
    col1, col2, col3 = st.columns([1, 1, 2])
    with col1:
        if st.button("Route all demo scenarios", type="primary", use_container_width=True):
            for scenario in scenarios:
                process_event(
                    SyncFailureEvent.model_validate(scenario["event"]), mode, config
                )
            st.rerun()
    with col2:
        if st.button("Load 20 dataset events", use_container_width=True):
            for row in load_dataset("heldout")[:20]:
                process_event(row.event, mode, config)
            st.rerun()
    with col3:
        st.caption(
            "Demo scenarios cover all seven routes. Dataset events are held-out synthetic cases; "
            "their labels are never shown to the classifier."
        )

    decisions = st.session_state.decisions
    if not decisions:
        st.info("Nothing routed yet in this run. Use a button above to populate the inbox.")
        st.subheader("The six demo scenarios")
        for scenario in scenarios:
            with st.expander(scenario["title"]):
                st.write(scenario["shows"])
                st.caption(f"Expected behaviour: {scenario['expected_behaviour']}")
        return

    rows = []
    for event_id, (decision, plan) in decisions.items():
        event = get_storage(settings().db_path).get_event(event_id)
        rows.append({
            "event_id": event_id,
            "connector": event.connector_id if event else "",
            "provider": event.provider if event else "",
            "error": (event.error_message[:70] + "…") if event and len(event.error_message) > 70
                     else (event.error_message if event else ""),
            "final_route": decision.final_route.value,
            "source": decision.source.value,
            "workflow": plan.status.value,
            "fixture": bool(decision.answer and decision.answer.is_fixture),
            "decided_at": decision.decided_at.strftime("%H:%M:%S"),
        })
    frame = pd.DataFrame(rows)

    f1, f2, f3 = st.columns(3)
    with f1:
        routes = st.multiselect("Route", sorted(frame["final_route"].unique()))
    with f2:
        providers = st.multiselect("Provider", sorted(frame["provider"].unique()))
    with f3:
        review_only = st.checkbox("Needing review only")

    view = frame.copy()
    if routes:
        view = view[view["final_route"].isin(routes)]
    if providers:
        view = view[view["provider"].isin(providers)]
    if review_only:
        view = view[view["final_route"] == Route.ENGINEER_REVIEW.value]

    st.dataframe(
        view, use_container_width=True, hide_index=True,
        column_config={
            "fixture": st.column_config.CheckboxColumn("fixture?", disabled=True),
            "error": st.column_config.TextColumn("error preview", width="large"),
        },
    )

    chosen = st.selectbox(
        "Open in the event inspector", ["—"] + list(view["event_id"]),
    )
    if chosen != "—":
        st.session_state.selected_event = chosen
        st.success(f"Selected {chosen}. Open the **Event inspector** tab.")

    counts = frame["final_route"].value_counts()
    st.subheader("Routes in this run")
    cols = st.columns(len(ALL_ROUTES))
    for col, route in zip(cols, ALL_ROUTES):
        col.metric(route.value.replace("_", " ").title(), int(counts.get(route.value, 0)))


# ----------------------------------------------------------------- inspector


def render_decision(decision: RoutingDecision, event: SyncFailureEvent, plan) -> None:
    left, right = st.columns([3, 2])

    with left:
        st.markdown("##### Decision")
        a, b = st.columns(2)
        a.markdown("**Proposed route**<br>" + (
            route_badge(decision.proposed_route) if decision.proposed_route
            else "<span style='color:#888'>none — decided before classification</span>"
        ), unsafe_allow_html=True)
        b.markdown("**Final route**<br>" + route_badge(decision.final_route),
                   unsafe_allow_html=True)

        st.markdown("")
        meta = {"decision source": decision.source.value}
        if decision.rule_id:
            meta["rule"] = decision.rule_id
        if decision.policy_id:
            meta["policy"] = decision.policy_id
        if decision.fallback_reason:
            meta["fallback reason"] = decision.fallback_reason
        meta["model called"] = str(decision.model_called)
        meta["end-to-end"] = f"{decision.total_latency_ms:.0f} ms"
        st.table(pd.DataFrame(meta.items(), columns=["field", "value"]).set_index("field"))

        if decision.final_route is Route.ENGINEER_REVIEW:
            if decision.is_fallback_review:
                st.warning(
                    f"This is a **fallback** to review, not a decision that the failure needs "
                    f"an engineer. Cause: `{decision.fallback_reason}`."
                )
            elif decision.source in (DecisionSource.MODEL, DecisionSource.MODEL_FIXTURE):
                st.info("The classifier **explicitly selected** engineering review.")

        st.markdown("##### Why")
        st.caption(decision.explanation)

    with right:
        answer = decision.answer
        if answer is None:
            st.markdown("##### Classification")
            st.caption("No classification was made for this event.")
        else:
            label = "FIXTURE" if answer.is_fixture else "LIVE JEV"
            st.markdown(f"##### Classification · `{label}`")
            if answer.is_fixture:
                st.caption("A saved response. Not a measurement of Jev performance.")
            probs = pd.DataFrame(
                sorted(answer.probabilities.items(), key=lambda kv: -kv[1]),
                columns=["route", "probability"],
            )
            st.dataframe(
                probs, hide_index=True, use_container_width=True,
                column_config={
                    "probability": st.column_config.ProgressColumn(
                        "probability", min_value=0.0, max_value=1.0, format="%.3f"
                    )
                },
            )
            m1, m2, m3 = st.columns(3)
            m1.metric("Confidence", f"{answer.confidence:.2f}")
            m2.metric("Selected p", f"{answer.selected_probability:.2f}")
            m3.metric("Margin", f"{answer.top_two_margin:.2f}")
            st.caption(
                "Confidence is a statistic derived from the distribution's shape. It is not a "
                "guaranteed probability that the answer is correct."
            )
            if answer.latency_ms is not None:
                st.caption(
                    f"Model `{answer.model_version}` · {answer.latency_ms:.0f} ms · "
                    f"{answer.retry_count} retries · "
                    f"{answer.input_tokens or 0} input tokens"
                )


def render_workflow(decision: RoutingDecision, event: SyncFailureEvent, plan) -> None:
    st.markdown("##### Simulated workflow")
    status_colour = {
        WorkflowStatus.ESCALATED: "🔴",
        WorkflowStatus.PENDING_APPROVAL: "🟠",
        WorkflowStatus.NEEDS_USER_ACTION: "🟡",
        WorkflowStatus.WAITING: "🔵",
        WorkflowStatus.ROUTED: "⚪",
        WorkflowStatus.SIMULATED_RECOVERED: "🟢",
    }
    st.markdown(f"{status_colour.get(plan.status, '⚪')} **{plan.status.value}** — {plan.summary}")
    st.caption(f"Required actor: {plan.required_actor}")
    if plan.next_attempt_at:
        st.caption(f"Next simulated attempt: {plan.next_attempt_at.isoformat()}")

    for step in plan.steps:
        st.markdown(f"{'✅' if step.completed else '⬜'} **{step.label}** — {step.detail}")
    for note in plan.notes:
        st.caption(f"ℹ️ {note}")

    cols = st.columns(3)
    store = st.session_state.decisions
    if plan.requires_approval and not plan.approved:
        if cols[0].button("Approve (simulated)", key=f"ap_{event.event_id}", type="primary"):
            store[event.event_id] = (decision, approve(plan))
            get_storage(settings().db_path).save_workflow(
                decision.run_id, event.event_id, store[event.event_id][1]
            )
            st.rerun()
        cols[1].caption("A resync assessment cannot advance without explicit approval.")
    else:
        if cols[0].button("Mark task complete", key=f"ct_{event.event_id}"):
            try:
                store[event.event_id] = (decision, complete_task(plan))
                get_storage(settings().db_path).save_workflow(
                    decision.run_id, event.event_id, store[event.event_id][1]
                )
                st.rerun()
            except WorkflowTransitionError as exc:
                st.error(str(exc))
        if cols[1].button("Mark recovered (user action)", key=f"mr_{event.event_id}"):
            try:
                store[event.event_id] = (decision, mark_recovered(plan, declared_by="UI operator"))
                get_storage(settings().db_path).save_workflow(
                    decision.run_id, event.event_id, store[event.event_id][1]
                )
                st.rerun()
            except WorkflowTransitionError as exc:
                st.error(str(exc))
        cols[2].caption("Recovery is only ever declared by a person, never inferred from the route.")


def view_inspector(mode: str, config: RouterConfig) -> None:
    st.header("Event inspector")

    scenarios = load_scenarios()
    source = st.radio(
        "Event source", ["Demo scenario", "Already routed", "Paste JSON"], horizontal=True
    )

    event: Optional[SyncFailureEvent] = None
    scenario_note = None

    if source == "Demo scenario":
        titles = {s["title"]: s for s in scenarios}
        chosen = st.selectbox("Scenario", list(titles))
        scenario = titles[chosen]
        event = SyncFailureEvent.model_validate(scenario["event"])
        scenario_note = scenario
    elif source == "Already routed":
        routed = list(st.session_state.decisions)
        if not routed:
            st.info("Nothing routed yet in this run.")
            return
        default = (
            routed.index(st.session_state.selected_event)
            if st.session_state.selected_event in routed else 0
        )
        chosen = st.selectbox("Event", routed, index=default)
        event = get_storage(settings().db_path).get_event(chosen)
    else:
        raw = st.text_area(
            "Event JSON", height=200,
            placeholder='{"event_id": "evt_custom", "incident_id": "inc_1", ...}',
        )
        if raw.strip():
            try:
                event = SyncFailureEvent.model_validate_json(raw)
            except Exception as exc:
                st.error(f"Invalid event: {exc}")
                return
        else:
            st.caption("Paste a SyncFailureEvent. Unknown custom events have no fixture, so "
                       "fixture mode will return a clearly marked unavailable-classification result.")
            return

    if event is None:
        return

    if scenario_note:
        st.info(f"**{scenario_note['shows']}**\n\nExpected behaviour: {scenario_note['expected_behaviour']}")

    c1, c2 = st.columns([1, 3])
    with c1:
        go = st.button("Route this event", type="primary", use_container_width=True)
    with c2:
        rerun = st.button("Re-run as a new experiment", use_container_width=True,
                          help="Issues a new run id and routes again, rather than reusing the "
                               "stored decision.")

    if rerun:
        st.session_state.run_id = new_run_id("ui")
        process_event(event, mode, config, force_new=True)
    elif go:
        process_event(event, mode, config)

    if event.event_id not in st.session_state.decisions:
        st.caption("Not yet routed in this run.")
        return

    decision, plan = st.session_state.decisions[event.event_id]
    st.divider()
    render_decision(decision, event, plan)
    st.divider()
    render_workflow(decision, event, plan)
    st.divider()

    with st.expander("Sanitized event", expanded=False):
        st.caption(
            f"Redactions applied: {', '.join(decision.redactions) or 'none matched'}. "
            f"{REDACTION_NOTICE}"
        )
        st.markdown("**Error text as the classifier saw it** (original wording, post-redaction):")
        st.code(event.error_message, language=None)
        st.json(json.loads(event.model_dump_json()))

    with st.expander("Reviewer correction"):
        st.caption(
            "A correction is recorded as feedback. It does not change this decision, and it "
            "does not alter any locked evaluation label."
        )
        reviewer = st.text_input("Reviewer", value="operator")
        corrected = st.selectbox("Correct route", [r.value for r in ALL_ROUTES])
        note = st.text_area("Note", height=70)
        if st.button("Record correction"):
            get_storage(settings().db_path).add_feedback(
                decision.run_id, event.event_id, reviewer, corrected, note
            )
            st.success("Correction recorded as feedback.")


# ---------------------------------------------------------------- evaluation


def view_evaluation() -> None:
    st.header("Evaluation")
    st.caption(
        "Rules-only baseline against the hybrid system on exactly the same events. "
        "Both are scored against provisional synthetic labels."
    )

    files = sorted(RESULTS_DIR.glob("*_results.json"), reverse=True) if RESULTS_DIR.exists() else []
    if not files:
        st.warning("**No live results yet.** Run:\n\n"
                   "`python -m syncroute.cli evaluate --split heldout --mode live`")
        return

    chosen = st.selectbox("Result file", files, format_func=lambda p: p.name)
    payload = json.loads(Path(chosen).read_text())
    manifest = payload["manifest"]
    baseline, hybrid = payload["rules_only"], payload["hybrid"]

    if manifest["mode"] == "fixture":
        st.error(
            "**Fixture run.** These figures come from saved responses and are not a measurement "
            "of Jev performance. They are shown to demonstrate the report, nothing more."
        )
    else:
        st.success(f"Live run against `{manifest['model_version_returned']}`.")

    st.caption(
        f"split `{manifest['split']}` · dataset `{manifest['dataset_hash'][:16]}` · "
        f"prompt `{manifest['prompt_version']}` · rules `{manifest['rules_version']}` · "
        f"policy `{manifest['policy_version']}` · gate "
        f"{manifest['thresholds']['confidence']}/{manifest['thresholds']['margin']} · "
        f"{manifest['run_timestamp'][:19]}"
    )
    st.warning(
        "Labels are synthetic and provisional (`human_reviewed: false`). With "
        f"{manifest['dataset_events']} events this is a small benchmark, not a statistically "
        "decisive result, and it says nothing about any real product."
    )

    def pct(value) -> str:
        return "N/A" if value is None else f"{value * 100:.1f}%"

    st.subheader("Headline")
    cols = st.columns(4)
    for col, title, key in [
        (cols[0], "Exact accuracy", "exact_accuracy"),
        (cols[1], "Macro F1", "macro_f1"),
    ]:
        b, h = baseline[key], hybrid[key]
        if key == "macro_f1":
            col.metric(title, "N/A" if h is None else f"{h:.3f}",
                       delta=None if (b is None or h is None) else f"{h - b:+.3f}")
        else:
            col.metric(title, pct(h),
                       delta=None if (b is None or h is None) else f"{(h - b) * 100:+.1f} pts")
    bc, hc = baseline["non_review_coverage"], hybrid["non_review_coverage"]
    cols[2].metric("Non-review coverage", pct(hc["value"]),
                   delta=f"{(hc['value'] - bc['value']) * 100:+.1f} pts")
    cols[2].caption(f"{hc['numerator']}/{hc['denominator']} · workflow assignment, not remediation")
    be, he = baseline["incorrect_non_review_rate"], hybrid["incorrect_non_review_rate"]
    cols[3].metric("Incorrect non-review", pct(he["value"]),
                   delta=None if (he["value"] is None or be["value"] is None)
                   else f"{(he['value'] - be['value']) * 100:+.1f} pts", delta_color="inverse")
    cols[3].caption(f"{he['numerator']}/{he['denominator']}")

    st.subheader("Side by side")
    def summarise(m: dict) -> dict[str, Any]:
        sub = m.get("rules_unmatched_subset", {})
        return {
            "events": m["events"],
            "exact accuracy": pct(m["exact_accuracy"]),
            "macro F1": "N/A" if m["macro_f1"] is None else f"{m['macro_f1']:.3f}",
            "non-review coverage": f"{pct(m['non_review_coverage']['value'])} "
                                   f"({m['non_review_coverage']['numerator']}/"
                                   f"{m['non_review_coverage']['denominator']})",
            "incorrect non-review": f"{pct(m['incorrect_non_review_rate']['value'])} "
                                    f"({m['incorrect_non_review_rate']['numerator']}/"
                                    f"{m['incorrect_non_review_rate']['denominator']})",
            "ENGINEER_REVIEW recall": f"{pct(m['engineer_review_recall']['value'])} "
                                      f"({m['engineer_review_recall']['numerator']}/"
                                      f"{m['engineer_review_recall']['denominator']})",
            "accuracy on rules-unmatched": pct(sub.get("exact_accuracy")),
            "coverage on rules-unmatched": pct(sub.get("non_review_coverage", {}).get("value")),
            "model calls": m["counts"]["model_calls"],
            "rule hits": m["counts"]["rule_hits"],
            "uncertainty fallbacks": m["counts"]["uncertainty_fallbacks"],
            "policy escalations": m["counts"]["policy_escalations"],
            "API error fallbacks": m["counts"]["api_error_fallbacks"],
        }

    st.dataframe(
        pd.DataFrame({"rules only": summarise(baseline), "hybrid": summarise(hybrid)}),
        use_container_width=True,
    )
    st.caption(
        "The rules-unmatched rows are the ones that matter: they are the cases the "
        "deterministic layer could not decide, where a classifier can add or destroy value."
    )

    st.subheader("Latency and cost")
    lat, usage = hybrid["latency_ms"], hybrid["usage"]
    l1, l2, l3, l4 = st.columns(4)
    def ms(v): return "N/A" if v is None else f"{v:.0f} ms"
    l1.metric("End-to-end p50", ms(lat["end_to_end_p50"]))
    l2.metric("End-to-end p95", ms(lat["end_to_end_p95"]))
    l3.metric("Model call p50", ms(lat["model_call_p50"]))
    l4.metric("Model call p95", ms(lat["model_call_p95"]))
    st.caption(f"n={lat['end_to_end_samples']} end-to-end, n={lat['model_call_samples']} model "
               f"calls. {lat['note']}")
    tokens = "unavailable" if usage["input_tokens"] is None else f"{usage['input_tokens']:,}"
    cost = "N/A" if usage["estimated_cost_usd"] is None else f"${usage['estimated_cost_usd']:.6f}"
    st.caption(f"Input tokens: {tokens} · estimated cost: {cost} — {usage['cost_note']}")

    st.subheader("Confusion matrix (hybrid)")
    matrix = pd.DataFrame(hybrid["confusion_matrix"]).T
    matrix.index.name = "expected \\ predicted"
    st.dataframe(matrix, use_container_width=True)

    st.subheader("Per-route detail (hybrid)")
    per_route = pd.DataFrame(hybrid["per_route"]).T
    for column in ("precision", "recall", "f1"):
        per_route[column] = per_route[column].map(
            lambda v: "N/A" if v is None or pd.isna(v) else f"{v:.3f}"
        )
    st.dataframe(per_route, use_container_width=True)

    if payload.get("threshold_sweep"):
        st.subheader("Coverage / error tradeoff")
        sweep = pd.DataFrame(payload["threshold_sweep"])
        st.caption(
            "Replayed from the same captured answers, so no extra API calls were made. "
            "Thresholds may only be selected on the development split."
        )
        st.dataframe(sweep, use_container_width=True, hide_index=True)


# ------------------------------------------------------------------- main


def main() -> None:
    init_state()
    mode, config = sidebar()
    st.title("SyncRoute")
    st.caption(
        "A prototype that routes data-sync failures to one of seven recovery workflows using "
        "deterministic rules for familiar errors and a Jev System One classifier for unfamiliar "
        "error language. Ordinary code, not the model, controls retry limits and approvals."
    )
    inbox, inspector, evaluation = st.tabs(
        ["Failure inbox", "Event inspector", "Evaluation"]
    )
    with inbox:
        view_inbox(mode, config)
    with inspector:
        view_inspector(mode, config)
    with evaluation:
        view_evaluation()


main()
