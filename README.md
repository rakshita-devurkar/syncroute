# SyncRoute

A prototype that routes data-sync connector failures to one of seven recovery
workflows. Familiar, unambiguous errors are decided by deterministic rules.
Unfamiliar error language is classified by [Jev](https://typesafe.ai), TypeSafe
AI's System One model, which returns a typed choice with calibrated
probabilities instead of text. Ordinary code — not the model — owns retry
limits, escalation and approvals.

**Everything here is synthetic and every recovery workflow is simulated.**
Nothing is reconnected, restarted, resynced, contacted or modified. The only
outbound call the application ever makes is the Jev classification request, and
only when you turn it on.

---

## Results

Measured on a frozen held-out split of 60 synthetic events, against live
`jev-1.13.0`. Both systems saw exactly the same events.

| | Rules only | Rules + policy + Jev |
| --- | --- | --- |
| Exact accuracy (7 routes) | 26.7% (16/60) | **88.3%** (53/60) |
| Macro F1 | 0.445 | **0.889** |
| Non-review coverage | 21.7% (13/60) | **83.3%** (50/60) |
| Incorrect non-review rate | 23.1% (3/13) | **6.0%** (3/50) |
| ENGINEER_REVIEW recall | 66.7% (6/9) | 66.7% (6/9) |

On the **44 cases the deterministic rules could not decide** — the subset where
a classifier either adds or destroys value:

| | Rules only | Rules + policy + Jev |
| --- | --- | --- |
| Exact accuracy | 6.8% | **90.9%** |
| Non-review coverage | 0% (0/44) | **84.1%** (37/44) |
| Incorrect non-review rate | N/A (no assignments) | **0.0% (0/37)** |

Latency: end-to-end p50 349 ms / p95 435 ms (n=60); model call p50 366 ms / p95
438 ms (n=44). 45,394 input tokens for the whole run, an estimated **$0.0019**
at the $0.042/M input price documented on 2026-09-21. Output tokens are free.
13 rule hits, 44 model calls, 5 uncertainty fallbacks, 3 policy escalations,
0 API errors.

### The most interesting result is a negative one

**Every incorrect workflow assignment came from my own deterministic rules, not
from the model.** All three errors are one rule, `R040_RESOURCE_VALIDATED_MISSING`,
firing on a family where structured evidence says the configured resource is
missing while the error text *also* alleges the credential was rejected. The
rule short-circuits before the classifier ever sees the conflict. Across all 44
rules-unmatched cases, Jev produced **zero** incorrect non-review assignments.

That rule has deliberately **not** been fixed. Rules were frozen before the
held-out run, and narrowing one to match held-out cases would make the number
meaningless. The candidate change is written up as exploratory in
[`docs/HUMAN_REVIEW.md`](docs/HUMAN_REVIEW.md) and needs a fresh held-out set
before it can support any new claim.

### What these numbers are not

- Not production accuracy. 102 synthetic events written by one author.
- Not statistically decisive. Several differences above rest on 2–3 events.
- Not validated. Every label is `human_reviewed: false`. See
  [`docs/HUMAN_REVIEW.md`](docs/HUMAN_REVIEW.md) — the labels are the single
  biggest threat to every figure on this page.
- Not a statement about any real product, vendor or connector platform.
- Not remediation. A route is a *next action*, not a fix, a root cause, or
  authorisation to change anything.

---

## The seven routes

| Route | Chosen when evidence indicates | Simulated workflow |
| --- | --- | --- |
| `RETRY_LATER` | Transient failure or rate limit, retries still permitted | Next attempt time from Retry-After or capped backoff |
| `REAUTHENTICATE` | Credential invalid, expired or revoked | Reconnect request for the connection owner |
| `FIX_PERMISSIONS` | Authenticated identity lacks a grant or scope | Permission-review task; never names a grant it wasn't given |
| `REVIEW_CONFIGURATION` | Resource selection or settings wrong or obsolete | Shows the implicated configuration fields |
| `CHECK_CONNECTIVITY` | DNS, reachability, connection or TLS problem | Mock diagnostic checklist, labelled as simulated |
| `REVIEW_RESYNC` | Cursor, retained logs or resume position can't continue | Resync assessment gated behind human approval |
| `ENGINEER_REVIEW` | Unknown, ambiguous, conflicting, or repeatedly unresolved | Sanitized context in an investigation queue |

Routes are next actions, not mutually exclusive root causes. When several causes
imply incompatible next actions and the evidence doesn't resolve them, the
answer is `ENGINEER_REVIEW`.

---

## How it routes

One pipeline, shared by the UI, the CLI and evaluation. Order is the product:

1. **Sanitize** — redact secret shapes before anything leaves the process.
2. **Derive** — compute counters, elapsed time and retry delays *in code*.
   `jev-1.13` is documented to be unreliable at counting and date ordering, so
   it is never asked to do either.
3. **Escalate** — mandatory policy, before any classification. Exhausted
   retry-shaped budgets and failures that persisted after a completed recovery
   go straight to review. Unrelated historical attempts do not trigger this.
4. **Rules** — high-precision deterministic rules on explicit structured
   evidence or narrow documented signatures. Conflicting rules escalate rather
   than guess.
5. **Classify** — one Jev Choice over the seven routes, for whatever is left.
6. **Gate** — confidence ≥ 0.80 *and* top-two margin ≥ 0.15, or fall back to review.
7. **Re-check** — policy runs again over the proposed route. A classification
   can never lift a retry limit or bypass a required approval.
8. **Explain** — deterministic template text, labelled as routing policy, never
   presented as the model's reasoning.

`proposed_route` and `final_route` are always recorded separately, and an
`ENGINEER_REVIEW` outcome always records *which kind* it was: an explicit model
selection, an uncertainty fallback, a policy escalation, a rule conflict, or an
API failure.

### What is deliberately not a rule

There is no rule mapping 401 → reconnect, 403 → permissions, 404 →
configuration, or timeout → connectivity. Provider behaviour makes all four
ambiguous, and `FIX_PERMISSIONS` has no deterministic rule at all — separating
"lacks a grant" from "bad credential" needs the error language. Each rule that
does exist carries a written `assumption` string stating what it takes for
granted.

---

## Setup

Requires Python 3.10+. No API key is needed to run the app.

```bash
git clone https://github.com/rakshita-devurkar/syncroute.git
cd syncroute
uv venv --python 3.12 && uv pip install -e ".[dev]"
```

Run the app (fixture mode, fully offline):

```bash
.venv/bin/streamlit run app.py
```

Run the tests:

```bash
.venv/bin/python -m pytest          # 58 offline tests
.venv/bin/python -m pytest -m live  # 2 opt-in tests that call the real API
```

### Enabling live classification

```bash
cp .env.example .env     # then add your key
```

Create a key at [console.typesafe.ai](https://console.typesafe.ai) (keys live
under Settings → API keys). `.env` is gitignored. The app switches to live mode
automatically once a key is present, and the sidebar shows **LIVE JEV** instead
of **FIXTURE MODE**.

```bash
.venv/bin/python -m syncroute.cli evaluate --split heldout --mode live --sweep
```

Without a key, every command still works in fixture mode and the evaluation view
says so rather than inventing numbers.

---

## Three-minute demo

```bash
.venv/bin/streamlit run app.py
```

1. **Failure inbox** → *Route all demo scenarios*. Seven routes, six scenarios,
   each showing its decision source. Note the mode badge in the sidebar.
2. **Scenario 1 (known 429)** → Event inspector. Decided by rule
   `R001`, `model_called = false`. A recognised error costs nothing.
3. **Scenario 5 (exhausted retries)** → the *same* 429, but the budget is gone.
   Policy `P001` escalates before rules and before any model call. The model is
   never consulted about whether to exceed a retry limit.
4. **Scenario 3 (missing scope)** → no rule matches a 403 by design. The
   classifier proposes `FIX_PERMISSIONS`; the probability bars and confidence
   are shown, and the simulated task names no grant it wasn't given.
5. **Scenario 4 (vague access error)** → the classifier's best answer arrives at
   confidence 0.49, the gate rejects it, and the inspector labels this a
   *fallback*, not a judgement that an engineer is needed.
6. **Scenario 6 (expired cursor)** → `REVIEW_RESYNC`, workflow status
   `PENDING_APPROVAL`. Try to advance it: the transition is refused until
   Approve is pressed. Approval changes simulation state only.
7. **Evaluation tab** → rules-only against hybrid on identical events, with the
   rules-unmatched subset called out separately.

**Scenario 2 is worth watching honestly.** The consent-withdrawal wording
("consent was withdrawn" — no rule matches, because no rule keys on that phrase)
gets the *correct* proposal, `REAUTHENTICATE`, at confidence 0.79. The frozen
gate is 0.80. It falls back to review by one hundredth. That is the safety gate
costing real coverage, displayed rather than tuned away — on the development
split the same family scored 0.72, 0.74 and 0.85.

---

## Evaluation methodology

- 102 synthetic events across API, database and file-source connectors, in 39
  **families**. A family is several paraphrases of one underlying failure and
  never straddles the split, so tuning cannot see a near-duplicate of a
  held-out case.
- 42 development / 60 held-out. All seven routes appear in both.
- Labels live on a separate object from the event. `SyncFailureEvent` has no
  label field at all, and a test asserts no label string reaches the model
  payload.
- Prompt and thresholds were tuned on development only, then frozen. Rewriting
  the prompt so that an `unknown` structured field no longer reads as "no
  evidence" moved development accuracy from 64.3% to 95.2%; that change was made
  before the held-out set was ever run.
- The manifest for every run records dataset hash, split, model version
  requested *and* returned, prompt/rules/policy/pipeline versions, thresholds,
  mode and timestamp. Results export to JSON and CSV with raw sanitized
  responses kept for traceability.
- Threshold sweeps replay cached answers through the real pipeline, so they cost
  no extra API calls and cannot drift from production semantics.

Undefined metrics render as `N/A`, never 0. Token usage and cost appear only
when the API actually returned usage. **Fixture runs are tagged everywhere and
must never be cited as Jev performance** — the fixture replay of the held-out
split scores 90.0% against live's 88.3% on identical events, which is precisely
why the distinction is enforced in code.

---

## Known limitations

- **The labels are unverified.** One author wrote both the events and their
  correct answers. This is the weakest link in every number above.
- **Small dataset.** 60 held-out events cannot support a decisive claim, and
  differences of a few points are noise.
- **Redaction is best-effort.** `sanitize.py` matches a fixed list of secret
  shapes. It has never been tested against a real error corpus and cannot be
  described as complete.
- **One global threshold is the wrong shape.** A wrong `REVIEW_RESYNC` does not
  cost what a wrong `RETRY_LATER` costs; real use should gate per consequence.
- **Confidence is not correctness.** It is a statistic derived from the
  distribution's shape, as TypeSafe's own documentation states.
- **The top-two margin gate has never independently fired.** Across 68 real
  classifications, no case had confidence >= 0.80 *and* margin < 0.15; the one
  low-margin case (0.11) was already rejected on confidence. The gate is kept
  because a confidently-split distribution is possible in principle, but on
  this evidence it has done no work and should not be credited with any.
- **The dataset never exercises the positive evidence states.** No event sets
  `exists`, `reachable` or `available`, so the "unknown is not false" contract
  is verified by unit tests rather than by the benchmark itself.
- **Synthetic error text is cleaner than production logs.** Real messages
  arrive truncated, multilingual, wrapped in stack traces and mixed with
  unrelated output.
- **ENGINEER_REVIEW recall is 66.7% and identical for both systems**, because
  the three misses are rule-driven and the classifier never saw them.
- **No connector platform integration.** This reads invented events from a JSON
  file.

---

## Layout

```
app.py                      Streamlit UI: inbox, inspector, evaluation
src/syncroute/
  models.py                 Route enum, event schema, decision records
  sanitize.py               Best-effort secret redaction
  rules.py                  Frozen deterministic rules, each with an assumption
  policy.py                 Derived facts and mandatory escalations
  router.py                 The one pipeline
  jev_client.py             State building, prompt, transport, validation
  workflows.py              Seven simulated workflows and their transitions
  storage.py                SQLite: immutable per-run decisions
  evaluation.py             Metrics, sweeps, manifest, export
  cli.py                    evaluate / sweep / route / init-db
data/                       Dataset, demo scenarios, fixtures, review sheet
docs/HUMAN_REVIEW.md        What a person still has to sign off on
scripts/                    Dataset builder, fixture capture, review sheet
tests/                      58 offline tests, 2 opt-in live tests
```

## License

MIT
