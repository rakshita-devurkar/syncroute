"""Generate the human label-review sheet.

Every metric in this project rests on labels that were generated, not verified.
This produces one row per family (not per event) so a reviewer signs off on 39
judgements rather than 102 near-duplicates.

    python scripts/make_label_review_sheet.py
"""

from __future__ import annotations

import csv
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from syncroute.evaluation import load_dataset  # noqa: E402

OUT = ROOT / "data" / "label_review_sheet.csv"


def main() -> int:
    families: dict[str, dict] = {}
    counts: dict[str, int] = defaultdict(int)
    examples: dict[str, list[str]] = defaultdict(list)

    for split in ("development", "heldout"):
        for row in load_dataset(split):
            fid = row.meta.family_id
            counts[fid] += 1
            examples[fid].append(row.event.error_message)
            families.setdefault(
                fid,
                {
                    "family_id": fid,
                    "expected_route": row.meta.expected_route.value,
                    "split": row.meta.split,
                    "difficulty": row.meta.difficulty,
                    "connector_type": row.event.connector_type.value,
                    "label_rationale": row.meta.label_rationale,
                },
            )

    with OUT.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([
            "family_id", "expected_route", "split", "events", "difficulty", "connector_type",
            "label_rationale", "example_error_message",
            "reviewer", "agree_Y_N", "corrected_route", "reviewer_notes",
        ])
        for fid, fam in sorted(families.items(), key=lambda kv: (kv[1]["expected_route"], kv[0])):
            writer.writerow([
                fid, fam["expected_route"], fam["split"], counts[fid], fam["difficulty"],
                fam["connector_type"], fam["label_rationale"], examples[fid][0],
                "", "", "", "",
            ])

    print(f"{len(families)} families covering {sum(counts.values())} events -> {OUT}")
    print("All rows are unreviewed. Fill agree_Y_N and re-run evaluation after any correction.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
