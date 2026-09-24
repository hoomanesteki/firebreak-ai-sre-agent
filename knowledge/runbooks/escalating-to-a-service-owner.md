---
id: escalating-to-a-service-owner
title: Handing an investigation to the team that owns the service
covers: [frontend, checkout, payment, cart, product-catalog, shipping, ad, email]
symptoms:
  - a candidate service named but no safe action
  - evidence points somewhere nobody here can change
updated: '2026-09-24'
---

## Symptoms

The evidence names a service, and there is no safe automated action, or the
action needed is outside what may be taken without a person.

## First checks

This is the fallback, and it is available for every incident. Reaching it is not
a failure: a named service with cited evidence and a ruled out alternative is
most of the work, and it is considerably more than an alert on its own provides.

Before handing over, have these ready:

1. **The named service, and why it rather than its neighbours.** The service
   with the worst numbers is frequently not the one at fault, so say which
   comparison settled it.
2. **The window**, and whether it came from an alert or from a split of the
   recording.
3. **One trace or one log template**, not a summary of many.
4. **What was ruled out.** A recent change that did not survive the four
   questions in [assessing-a-recent-change](assessing-a-recent-change.md) is
   worth naming, so the receiving team does not spend their first ten minutes
   on it.

## Likely causes

Not applicable.

## Remediation

Page the owning team. `knowledge/teams.yaml` holds the escalation channel and
rotation for each.

Send it to one team. A page to two teams is a page each of them assumes the
other has taken.

## Escalation

This runbook is the escalation. If ownership is genuinely unclear, Platform
holds the shared plumbing and is the right default.
