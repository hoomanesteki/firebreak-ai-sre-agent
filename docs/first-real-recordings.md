# What the first real recordings changed

Everything before this document was measured on synthetic fixtures. This
records what happened when the incident library started being recorded from
the running OpenTelemetry Demo, because the answer was not "the numbers moved
a little".

**Every figure here is from 12 recorded incidents**, 11 in validation and one
in `test_ood`. That is a small sample and it is real, which makes it worth more
than the 147 synthetic trials it replaced.

## The headline

| Measured on | Top-1 | Top-3 |
|---|---|---|
| synthetic fixtures, validation | 100% | 100% |
| **recorded incidents, validation** | **2 of 9** | **6 of 9** |

Deterministic triage is far weaker on real telemetry than the fixtures
suggested, and the gap is not a calibration detail. Three separate defects
stood between the pipeline and the real data, and a fourth problem is in the
library itself.

## Four defects that synthetic data hid

### 1. The scorer read whole series through the tool row cap

`MAX_ROWS = 50` exists to stop a tool flooding a model's context. The
deterministic scorer has no context to protect, and it was reading through that
cap anyway. With `ORDER BY timestamp` across 18 services each service received
**three samples**, `MIN_BASELINE_POINTS` discarded them as untrustworthy, and a
genuine 25x latency regression on the true culprit scored **zero**.

Nothing failed. On synthetic bundles with 8 services and short windows, 50 rows
left just enough per service, so this was invisible until real telemetry
arrived with three times the services and a 28 minute window.

Fixed by adding `metric_series` with its own bound. The scorer now sees 35
baseline points instead of 3, and on the bundle that exposed this the true
culprit moved from rank 5 to rank 1.

### 2. A signal appearing from nothing scored ten million

A flat-zero baseline has no spread, so the scale fell to a floor of 1e-9 and any
departure from it scored around 3.1e7. An error counter on the edge proxy moving
from exactly zero to 0.01 per second, which is one event per hundred seconds,
outranked the actual incident by six orders of magnitude.

Synthetic fixtures never hit this because the builder gives every service
background errors, so no baseline was ever exactly flat.

Bounded now at 20. **A pooled scale was tried and measured worse**, twice, and
the rejected alternative is recorded in `triage/anomaly.py` with its numbers.

### 3. The observability stack was a root-cause candidate

Real recordings put `jaeger` and `flagd-ui` in the top three. Excluded now by an
explicit list, with `flagd` excluded for a second reason: naming the flag
service as a root cause is saying "a feature flag did this", which is the answer
the agent is meant to derive.

An inclusion filter would have been the obvious approach and would have been
wrong: the service graph reports the cart keystore as `redis` and the broker as
`kafka`, while the knowledge file names them `valkey-cart` and `orders`, so
keeping only what the knowledge graph describes would have silently dropped two
legitimate culprits.

### 4. Twelve scenarios could never fire

`imageSlowLoad` is evaluated in the frontend's **browser-side React component**,
which sets an `x-envoy-fault-delay-request` header that Envoy honours. The load
generator is an HTTP client, never runs React, and never sets the header. It is
the only one of the fifteen fault flags evaluated that way.

Confirmed three ways rather than inferred once:

1. The source shows the flag read in browser code and nowhere server-side.
2. An 18 minute recording at 50 users with the flag on shows `image-provider`
   p95 flat at **1.90ms throughout**.
3. A direct request with the flag on takes **0.0026s**; the same request
   carrying the header a browser would set takes **5.005s**.

Twelve of 120 scenarios were built on it. Each recorded a bundle in which
nothing happened while its label named `image-provider` as the culprit, so a
system correctly reporting a healthy system would have been scored **wrong** on
all twelve.

The three double-fault scenarios were worse than merely wasted. Their primary
fault did nothing, so the only thing that actually happened was the flag
labelled a *harmless distractor*, and a system correctly naming `shipping` was
marked wrong for not naming a service that was fine.

The library is now 114 scenarios. Replacing rather than deleting was necessary:
deleting emptied three families, including `latency` in train entirely.

## What is still broken

**Abstention does not work on real telemetry.** The threshold of 22.7 was tuned
on fixtures where faulted incidents scored 66.8 at minimum and quiet systems
7.7 at most, perfectly separable. On recorded incidents the two populations
overlap completely:

| | Range of the largest anomaly |
|---|---|
| faulted (9 recordings) | 4.85 to 11,980 |
| no-fault (2 recordings) | 9.00 to 534 |

No threshold separates those. This is not a tuning problem that more careful
fitting solves; the signal itself does not discriminate, and a different
abstention signal is needed. Two no-fault recordings is too few to design one
against, which is the immediate reason to record the train split, where seven
more no-fault scenarios live.

**The system currently abstains on most real incidents**, because the synthetic
threshold demands a magnitude real telemetry rarely reaches. It is left as it is
rather than lowered to fit 11 recordings, since fitting a threshold to a sample
this small would be the same mistake in the other direction.

## What this changes about the project

The phase reports have said "the fixtures are easier than reality" since
Phase 2. That was true and too weak. The fixtures are a **different scale**:
anomaly magnitudes differ by more than an order of magnitude, populations that
separate cleanly in fixtures overlap completely in reality, and three of the
four defects above were invisible precisely because the fixtures were uniform
where real telemetry is ragged.

No figure produced before these recordings should be quoted. The eval reports
already carry `quotable_as_a_result: false`, and they were right to.

## Recording, as it stands

| | Scenarios | Recorded |
|---|---|---|
| validation | 14 | 12 |
| train | 41 | 0 |
| test_id | 29 | 0 |
| test_ood | 30 | 1 |

About 34 hours of wall clock for the whole library at 18 minutes each, which
cannot be compressed: the baseline, the incident and the recovery all have to
happen. `make lab-record-library` is resumable and skips what is already on
disk.

Validation and train are recorded before the held-out splits, which is the
reverse of the obvious order. A quotable number from a miscalibrated system is
zero and measures nothing, and the thresholds can only be re-tuned on validation
and train.
