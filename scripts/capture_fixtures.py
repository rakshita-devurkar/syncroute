"""Capture fixture responses for offline mode.

Fixtures here are real responses previously returned by the API, saved so the
app can be demonstrated without a key. They are still fixtures: the app labels
every one FIXTURE, strips their latency, and the evaluation view refuses to
present them as measured performance.

The fixture file holds no labels. This script never reads expected_route.

    python scripts/capture_fixtures.py --demos
    python scripts/capture_fixtures.py --split development --split heldout
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from syncroute.config import get_settings  # noqa: E402
from syncroute.evaluation import load_dataset  # noqa: E402
from syncroute.jev_client import LiveJevProvider, JevError, build_state  # noqa: E402
from syncroute.models import SyncFailureEvent  # noqa: E402
from syncroute.policy import derive_policy_facts  # noqa: E402
from syncroute.rules import evaluate_rules  # noqa: E402
from syncroute.sanitize import sanitize_event  # noqa: E402

FIXTURES = ROOT / "data" / "fixture_responses.json"
SCENARIOS = ROOT / "data" / "demo_scenarios.json"


def capture(events: list[SyncFailureEvent], provider: LiveJevProvider, store: dict) -> None:
    for event in events:
        clean, _ = sanitize_event(event)
        facts = derive_policy_facts(clean)
        # Skip anything the rules already decide: it never reaches the model,
        # so a fixture for it would be dead weight.
        if evaluate_rules(clean, facts).route is not None:
            print(f"  {event.event_id:<22} decided by rules, no fixture needed")
            continue
        try:
            answer = provider.classify(build_state(clean, facts), event.event_id)
        except JevError as exc:
            print(f"  {event.event_id:<22} FAILED: {exc}")
            continue
        store[event.event_id] = answer.raw_response
        print(f"  {event.event_id:<22} {answer.choice.value:<22} conf={answer.confidence:.2f}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--demos", action="store_true")
    parser.add_argument("--split", action="append", default=[])
    args = parser.parse_args()

    settings = get_settings(ROOT / ".env")
    if not settings.has_api_key:
        print("TYPESAFE_API_KEY is required to capture fixtures.", file=sys.stderr)
        return 2
    provider = LiveJevProvider(
        api_key=settings.api_key, model=settings.model, base_url=settings.base_url
    )

    existing = json.loads(FIXTURES.read_text()) if FIXTURES.exists() else {}
    store = existing.get("responses", {})

    if args.demos:
        print("demo scenarios:")
        scenarios = json.loads(SCENARIOS.read_text())["scenarios"]
        capture(
            [SyncFailureEvent.model_validate(s["event"]) for s in scenarios], provider, store
        )
    for split in args.split:
        print(f"{split} split:")
        capture([row.event for row in load_dataset(split)], provider, store)

    payload = {
        "note": (
            "Real API responses captured on the date below and replayed offline. The "
            "application labels every one FIXTURE. Fixture probabilities and timings must "
            "never be reported as measured Jev performance. Contains no evaluation labels."
        ),
        "captured_model": settings.model,
        "responses": store,
    }
    FIXTURES.write_text(json.dumps(payload, indent=2))
    print(f"\n{len(store)} fixture responses -> {FIXTURES}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
