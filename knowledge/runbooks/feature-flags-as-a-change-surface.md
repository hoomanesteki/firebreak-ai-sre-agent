---
id: feature-flags-as-a-change-surface
title: Feature flags as a change surface
covers: [frontend, cart, payment, product-catalog, ad, recommendation]
symptoms:
  - a flag value changed recently
  - behaviour differs for a share of requests
updated: '2026-09-24'
---

## Symptoms

A flag value changed near the incident, or behaviour differs between requests
in a way that suggests a percentage rollout.

## First checks

A flag is a change like any other. It is recorded, it is recent, and it is not
evidence of anything by itself. Treat it exactly as
[assessing-a-recent-change](assessing-a-recent-change.md) says to treat a
deploy, and apply all four of its questions.

Two properties of flags are worth knowing:

1. **Several are on at any time, by design.** Finding one that is on is not a
   finding. Finding one that changed, on the service the evidence already
   points at, might be.
2. **A percentage flag affects a share of traffic.** That produces a partial
   failure rate rather than a total one, and a partial rate is a useful clue in
   its own right: it suggests a conditional path rather than a broken
   dependency, because a broken dependency usually fails everything.

Do not assume a relationship between a flag's name and the incident. Read the
evidence and let it name a service.

## Likely causes

Whatever the affected service's own runbook covers. A flag changes which code
path runs; it does not introduce a new failure mode of its own.

## Remediation

Returning a flag to its previous value is the cheapest action in this system:
it takes effect in about a second and is undone the same way. That cheapness is
exactly why it needs the four questions first, because an action this easy
invites being taken before it is understood.

Expect the reversal to restore previous behaviour. If it would move the system
to a state it has not been in before, that is not a reversal.

## Escalation

The team owning the affected service. Platform can say who changed a flag and
when.
