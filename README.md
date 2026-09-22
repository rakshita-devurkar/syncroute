# SyncRoute

Routes data-sync connector failures to one of seven recovery workflows.
Deterministic rules decide familiar errors; [Jev](https://typesafe.ai),
TypeSafe AI's System One model, classifies unfamiliar error language and
returns a typed choice with calibrated probabilities instead of text.

**Jev decides what kind of problem this is. Code decides what is allowed to
happen next** — retry limits and approvals live in plain Python that a
classification cannot override.

Everything is synthetic and every workflow simulated: nothing is reconnected,
restarted, resynced or contacted. The only outbound call is the Jev request.

## Results

Frozen held-out split, 60 synthetic events, live `jev-1.13.0`, identical events
for both systems. Bracketed figures are the 44 cases the rules could not
decide — where a classifier either adds or destroys value.

| | Rules only | Rules + policy + Jev |
| --- | --- | --- |
| Exact accuracy | 26.7% [6.8%] | **88.3%** [**90.9%**] |
| Macro F1 | 0.445 | **0.889** |
| Non-review coverage | 21.7% [0%] | **83.3%** [**84.1%**] |
| Incorrect non-review | 23.1% (3/13) | **6.0%** (3/50) [**0/37**] |
| ENGINEER_REVIEW recall | 66.7% (6/9) | 66.7% (6/9) |

p50 349 ms / p95 435 ms end-to-end. 45,394 input tokens ≈ **$0.0019** at the
$0.042/M price documented 2026-09-21. 13 rule hits, 44 model calls, 5
uncertainty fallbacks, 3 policy escalations, 0 API errors.

**The most interesting result is negative: every incorrect assignment came from
my own rules, not the model.** All three are `R040_RESOURCE_VALIDATED_MISSING`
firing where structured evidence says a resource is missing but the text *also*
alleges a rejected credential — the rule short-circuits before the classifier
sees the conflict. Across all 44 rules-unmatched cases Jev made zero incorrect
assignments. The rule is deliberately **not fixed**: rules were frozen before
the held-out run, and narrowing one to fit held-out cases would void the number.
Written up as exploratory in [`docs/HUMAN_REVIEW.md`](docs/HUMAN_REVIEW.md).

**What these numbers are not.** Not production accuracy (102 synthetic events,
one author). Not statistically decisive (several gaps rest on 2–3 events). Not
validated — every label is `human_reviewed: false`, the biggest threat to
everything above. Not a statement about any real product. And a route is a
*next action*, not a fix, a root cause, or authorisation to change anything.

## The seven routes

| Route | Chosen when evidence indicates |
| --- | --- |
| `RETRY_LATER` | Transient failure or rate limit, retries still permitted |
| `REAUTHENTICATE` | Credential invalid, expired or revoked |
| `FIX_PERMISSIONS` | Authenticated identity lacks a grant or scope |
| `REVIEW_CONFIGURATION` | Resource selection or settings wrong or obsolete |
| `CHECK_CONNECTIVITY` | DNS, reachability, connection or TLS problem |
| `REVIEW_RESYNC` | Cursor or retained logs cannot support continuing |
| `ENGINEER_REVIEW` | Unknown, ambiguous, conflicting, or repeatedly unresolved |

Routes are next actions, not mutually exclusive root causes. When causes imply
incompatible actions and evidence doesn't resolve them, the answer is
`ENGINEER_REVIEW`.

## How it routes

One pipeline, shared by UI, CLI and evaluation. The order is the product:

1. **Sanitize** — redact secret shapes before anything leaves the process.
2. **Derive** — counters, elapsed time and retry delays computed *in code*;
   `jev-1.13` is documented as unreliable at counting and date ordering.
3. **Escalate** — mandatory policy, before any model call: exhausted
   retry-shaped budgets, and failures that persisted after a completed recovery.
4. **Rules** — high precision only; conflicting rules escalate rather than guess.
5. **Classify** — one Jev Choice over the seven routes, for whatever remains.
6. **Gate** — confidence ≥ 0.80 *and* top-two margin ≥ 0.15, else review.
7. **Re-check** — policy runs again; a classification can never lift a retry
   limit or bypass an approval.
8. **Explain** — deterministic templates, never presented as model reasoning.

`proposed_route` and `final_route` stay separate, and every `ENGINEER_REVIEW`
records which kind it was: model selection, uncertainty fallback, policy
escalation, rule conflict, or API failure.

**Deliberately not rules:** 401 → reconnect, 403 → permissions, 404 → config,
timeout → connectivity. Provider behaviour makes all four ambiguous.
`FIX_PERMISSIONS` has no rule at all — separating "lacks a grant" from "bad
credential" needs the error language. Every rule carries a written `assumption`.

## Setup

Python 3.10+. No API key needed.

```bash
git clone https://github.com/rakshita-devurkar/syncroute.git && cd syncroute
uv venv --python 3.12 && uv pip install -e ".[dev]"

.venv/bin/streamlit run app.py        # fully offline, fixture mode
.venv/bin/python -m pytest            # 58 offline tests
.venv/bin/python -m pytest -m live    # 2 opt-in tests against the real API
```

For live classification, create a key at
[console.typesafe.ai](https://console.typesafe.ai):

```bash
cp .env.example .env                  # add key; .env is gitignored
.venv/bin/python -m syncroute.cli evaluate --split heldout --mode live --sweep
```

The app switches to live automatically and shows **LIVE JEV** instead of
**FIXTURE MODE**. Without a key everything still runs and the evaluation view
says "No live results yet" rather than inventing numbers.

## Three-minute demo

`streamlit run app.py` → **Route all demo scenarios**, then:

1. **Known 429** — rule `R001`, `model_called = false`. Recognised errors cost nothing.
2. **Exhausted retries** — the *same* 429, budget gone. Policy `P001` escalates
   before any model call; the model is never consulted about exceeding a limit.
3. **Missing scope** — no rule matches a 403 by design; the classifier proposes
   `FIX_PERMISSIONS` and the task names no grant it wasn't given.
4. **Vague access error** — best answer at confidence 0.49, gate rejects it, and
   the inspector labels this a *fallback*, not a judgement that an engineer is needed.
5. **Expired cursor** — `PENDING_APPROVAL`, refused until Approve is pressed.

**Scenario 2 is worth watching honestly:** consent-withdrawal wording gets the
*correct* proposal at confidence 0.79 against a 0.80 gate, and falls back to
review by one hundredth — the safety gate costing real coverage, displayed
rather than tuned away.

## Evaluation methodology

102 synthetic events in 39 **families** (paraphrases of one failure). A family
never straddles the split, so tuning cannot see a near-duplicate of a held-out
case: 42 development / 60 held-out, all seven routes in both.

Labels live on a separate object — `SyncFailureEvent` has no label field, and a
test asserts no label string reaches the model payload. Prompt and thresholds
were tuned on development only, then frozen; rewriting the prompt so an
`unknown` field no longer reads as "no evidence" moved development accuracy
64.3% → 95.2% before held-out was ever run. Every run records a manifest
(dataset hash, split, model requested *and* returned, prompt/rules/policy
versions, thresholds, mode, timestamp). Sweeps replay cached answers through the
real pipeline — no extra API calls, no drift from production semantics.

Undefined metrics render `N/A`, never 0. Cost appears only when the API returned
usage. **Fixture runs are tagged and are not measurements**: the fixture replay
scores 90.0% against live's 88.3% on identical events, which is exactly why the
distinction is enforced in code.

## Known limitations

- **Labels are unverified** — one author wrote both the events and the answers.
- **60 held-out events** cannot support a decisive claim.
- **Redaction is best-effort** — a fixed list of shapes, never tested on a real corpus.
- **One global threshold is the wrong shape** — a wrong `REVIEW_RESYNC` doesn't
  cost what a wrong `RETRY_LATER` costs.
- **The margin gate has never independently fired** in 68 classifications. Kept
  because a split distribution is possible, but it has earned no credit.
- **The dataset never sets a positive evidence state**, so "unknown is not
  false" is verified by unit tests, not by the benchmark.
- **Confidence is not correctness** — a shape statistic, as TypeSafe's docs state.
- **`ENGINEER_REVIEW` recall is identical for both systems**: the three misses
  are rule-driven, so the classifier never saw them.
- **No connector platform integration** — this reads invented events from JSON.

## Layout

```
app.py                  Streamlit UI: inbox, inspector, evaluation
src/syncroute/          models · sanitize · rules · policy · router
                        jev_client · workflows · storage · evaluation · cli
data/                   Dataset, demo scenarios, fixtures, review sheet
docs/HUMAN_REVIEW.md    What a person still has to sign off on
tests/                  58 offline tests, 2 opt-in live
```

MIT
