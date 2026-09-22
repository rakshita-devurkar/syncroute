# What still needs a human

Nothing in this repository has been validated by a person. The numbers in the
README were produced by a frozen pipeline over labels that were *generated*,
and a generated label is an assumption, not ground truth. This file lists what
a reviewer has to sign off on before any result here should be repeated
elsewhere, in priority order.

## 1. The 102 labels (highest priority)

Every metric in this project is a comparison against `expected_route`. If a
label is wrong, the metric is wrong in both directions at once: the system is
penalised for being right and credited for matching a mistake.

- Sheet: [`data/label_review_sheet.csv`](../data/label_review_sheet.csv)
- 39 rows, one per family, covering all 102 events. Families are paraphrases of
  a single underlying failure, so reviewing the family reviews every variant.
- Each row carries the rationale used when the label was written and one
  example message. Fill in `agree_Y_N`, and `corrected_route` where you disagree.
- Every event currently carries `human_reviewed: false`. That flag should only
  be flipped by the person who actually read the row.

After any correction: rebuild the dataset, then re-run evaluation. The dataset
hash in the manifest changes, which correctly invalidates the earlier run
rather than silently mixing the two.

The families most worth an expert eye, because the label is a judgement call
rather than a reading of the text:

| Family | Labelled | Why it is arguable |
| --- | --- | --- |
| `fam_conflicting_evidence` | ENGINEER_REVIEW | Could reasonably be REVIEW_CONFIGURATION if you trust the structured field over the text. See item 2. |
| `fam_db_password_rejected` | REAUTHENTICATE | A rejected database password may be a rotated secret (reconnect) or a dropped account (permissions). |
| `fam_entitlement_403` | FIX_PERMISSIONS | A plan-tier entitlement gap is arguably a configuration or commercial problem, not a permission grant. |
| `fam_prefix_no_match` | REVIEW_CONFIGURATION | Zero matched files may be a legitimately empty window rather than a misconfiguration. |
| `fam_db_connection_saturation` | RETRY_LATER | Chronic pool exhaustion is arguably an engineering problem once it stops being occasional. |

## 2. The one rule that produced every held-out error

All three incorrect non-review assignments on the held-out split came from
`R040_RESOURCE_VALIDATED_MISSING`, and none came from the classifier.

The rule fires on `resource_validation_state == "missing"` and assumes that
resource listing succeeded, so the identity could see the namespace. On the
`fam_conflicting_evidence` family that assumption does not hold: the error text
*also* alleges the credential was rejected. The rule short-circuits before the
classifier ever sees the conflict.

**This has deliberately not been fixed.** The rules were frozen before the
held-out run, and narrowing a rule to match held-out cases would make the
reported number meaningless. The candidate change — have `R040` decline when the
error text also alleges an authentication failure, and let the conflict escalate
— is exploratory. It needs a fresh held-out set to support any new claim.

A reviewer should decide: is the label wrong, or is the rule wrong?

## 3. The uncertainty thresholds

Frozen at confidence `0.80` and top-two margin `0.15`. These are demonstration
defaults, not calibrated guarantees, and they were chosen on the development
split before the held-out run.

The held-out sweep suggests a lower gate would have scored slightly better
(85.0% coverage at 0.70/0.10 against 83.3% at 0.80/0.15, with the incorrect
non-review count unchanged at 3). The difference is two events. This dataset
cannot distinguish those settings, and choosing the better-looking one after
seeing held-out results would be fitting to the test set.

A real deployment should set this per consequence: the cost of a wrong
REVIEW_RESYNC is not the cost of a wrong RETRY_LATER, and one global threshold
is the wrong shape for that.

## 4. Each rule's stated assumption

Every rule in `src/syncroute/rules.py` carries an `assumption` string saying what
it takes for granted. Those assumptions were written against invented providers.
Anyone applying this to real connectors should check each one against the actual
provider behaviour, particularly:

- that `429` always means a rate limit, and never a quota that will not refill
- that `503` never encodes a permanent configuration fault
- that `invalid_grant` always means the grant, not the client registration

## 5. The redactor, before any non-synthetic input

`src/syncroute/sanitize.py` recognises a fixed list of secret shapes. It has not
been tested against a real error corpus, and it cannot be described as complete.
Before pointing this at anything but synthetic events, someone should run it over
a representative sample and look at what it misses.

## 6. The claims in the README

The README states results from a 102-event synthetic dataset. A reviewer should
confirm that nothing in it reads as a production claim, a statistically decisive
result, or a statement about any real product's capabilities. It is none of those
things.
