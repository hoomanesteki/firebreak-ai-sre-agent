---
id: assessing-a-recent-change
title: Deciding whether a recent change caused this
covers: [frontend, checkout, payment, cart, product-catalog]
symptoms:
  - a deploy or config change near the incident
  - a feature flag changed recently
  - unsure whether a change is the cause
updated: '2026-09-24'
---

## Symptoms

Something changed close to when the incident started, and the temptation is to
stop there.

## First checks

Chasing the most recent change is the single most common way root cause
analysis goes wrong. Something changes almost all the time, so finding a change
near an incident is expected rather than informative.

Four questions, and a change has to pass all four:

1. **Does it affect the right service?** A change to a service that is not
   anomalous is not a candidate, however recent it is.
2. **Is the ordering right?** The anomaly must begin after the change, not
   before. A change made in response to an incident already under way looks
   identical in a change log.
3. **Does the mechanism connect?** Say out loud how this change produces this
   symptom. If the sentence does not work, the correlation is not enough.
4. **Is anything else a better fit?** If a service deeper in the chain is also
   anomalous and nothing changed there, the change may be a coincidence sitting
   next to a real fault.

## Likely causes

- The change, when all four questions pass.
- Coincidence, which is the more common outcome and the one worth stating
  plainly rather than leaving as an unexplained loose end.

## Remediation

If the change is a feature flag and all four questions pass, reversing it is
the cheapest safe action available. See
[feature-flags-as-a-change-surface](feature-flags-as-a-change-surface.md).

If the change passes the questions but is not a flag, that is a conversation
with the owning team rather than an automated action.

## Escalation

Whoever made the change, together with whoever owns the service.
