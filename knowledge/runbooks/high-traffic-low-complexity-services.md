---
id: high-traffic-low-complexity-services
title: A small shared service is degrading everything
covers: [currency]
symptoms:
  - many services slightly slower at once
  - no single service looks badly wrong
  - latency rose across the board
updated: '2026-09-24'
---

## Symptoms

Many services are slightly slower and none looks badly wrong on its own. The
system feels degraded without an obvious culprit.

## First checks

Currency is called by frontend and checkout on nearly every request, so it
carries very high traffic for a service that does very little. A small
regression there is multiplied across everything.

1. Look at the anomaly list and notice the shape: many small deviations rather
   than one large one. That shape points at a shared dependency.
2. Check currency's latency against a baseline. A change of a few milliseconds
   matters here in a way it would not on a service called once per page.
3. Check whether the affected services all call it. If some do not, look
   elsewhere.
4. Check the request rate before concluding anything is wrong with it.

## Likely causes

- Currency, if every affected service calls it.
- Another shared dependency. Product-catalog has three callers and is worth
  ruling out the same way.
- Traffic, not the system. See
  [traffic-changed-but-nothing-broke](traffic-changed-but-nothing-broke.md).

## Remediation

Weigh the blast radius carefully: nearly every request touches this service, so
an action here is felt everywhere at once.

## Escalation

Platform owns currency.
