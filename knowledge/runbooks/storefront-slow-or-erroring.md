---
id: storefront-slow-or-erroring
title: The storefront is slow or returning errors
covers: [frontend]
symptoms:
  - pages slow to load
  - errors on the storefront
  - customers reporting the site is down
updated: '2026-09-24'
---

## Symptoms

Page loads are slow, or the storefront returns errors, or both. This is the
alert that fires for most incidents in this system, whatever caused them.

## First checks

Start by establishing whether the frontend is failing or reporting. It calls
almost every other service to render a page, so it inherits their latency and
their errors, and it is far more often the messenger than the culprit.

1. Compare the frontend's error rate and p95 against a baseline before the
   incident. Note the size of the change; you will compare it against its
   dependencies.
2. List anomalies across every service in the same window. If a service the
   frontend calls has moved further than the frontend has, look there first.
3. Take one failing trace and read the critical path. The span where time is
   actually spent, or where the error originates, names a service directly and
   is worth more than any amount of rate comparison.

## Likely causes

In rough order of how often they turn out to be the answer:

- A service the frontend calls is failing or slow, and the frontend is
  faithfully reporting it. Check the span breakdown before anything else.
- A shared dependency several callers use, which shows as multiple services
  degrading at once rather than one.
- A change to the frontend itself, which is worth checking but is a
  coincidence until the frontend is also the service whose numbers moved
  most.
- Traffic changed rather than the system. See
  [traffic-changed-but-nothing-broke](traffic-changed-but-nothing-broke.md).

## Remediation

Do not restart the frontend to make an inherited error go away. It will
succeed at clearing the symptom and lose the evidence.

Once the failing service is named, follow that service's own runbook. If the
frontend really is the origin, page Storefront.

## Escalation

Storefront owns the frontend and takes the alert. If the evidence names
another team's service, hand it over with the trace and the window, not with
the alert.
