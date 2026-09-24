---
id: payment-declines-and-errors
title: Payments are failing
covers: [payment]
symptoms:
  - payment errors
  - charges failing
  - checkout failing at the payment step
updated: '2026-09-24'
---

## Symptoms

Payment returns errors, or checkout reports that the payment step failed.

## First checks

Payment is called only by checkout and has no datastore, which narrows things
considerably: its failures are its own logic, its configuration, or its
inability to reach something it depends on.

1. Compare payment's error rate against a baseline. Note whether it is total
   or partial, because a fixed share of requests failing is a different
   mechanism from all of them failing.
2. Cluster the error templates. One dominant template means one mechanism.
3. Check the onset. Payment failing before checkout started reporting problems
   makes payment the origin; the reverse makes it a victim of load.
4. Check what changed in the window, and then resist the pull of the first
   change you find. See
   [assessing-a-recent-change](assessing-a-recent-change.md).

## Likely causes

- A configuration or code path in payment itself.
- A partial failure rate, which often points at a conditional path rather than
  at a broken dependency, since a broken dependency usually fails everything.
- Load from checkout beyond what payment handles.

## Remediation

There is no store to check and no cache to clear, so a restart has little to
recommend it unless there is a resource trend. If the evidence names a recent
change to payment and payment is also the service whose numbers moved, a
reversal is the cheapest safe action.

## Escalation

Checkout owns payment. This is their highest urgency service: a failure here
costs orders rather than page views.
