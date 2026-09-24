---
id: degraded-but-not-broken
title: A supporting service is failing and the shop still works
covers: [ad, email]
symptoms:
  - one supporting service erroring
  - no customer impact on the purchase path
  - alert fired but nothing is visibly wrong
updated: '2026-09-24'
---

## Symptoms

A supporting service is erroring, and the purchase path is unaffected.

## First checks

The useful question here is not what broke but how much it matters, because the
answer changes who gets woken up.

1. Establish what the failing service is on the path of. A page renders without
   adverts. An order completes without a confirmation email, though the customer
   notices later.
2. Confirm the purchase path is genuinely clean rather than merely quieter.
3. Check whether the failing service is the cause or a victim of something
   larger. A supporting service failing alone is usually its own problem; one
   failing alongside core services is usually not.

## Likely causes

Whatever the service's own runbook covers. The point of this runbook is the
triage decision rather than the diagnosis.

## Remediation

A restart on a supporting service is comparatively cheap, which is a reason to
be careful rather than a reason to reach for it: the low cost makes it tempting
to act before understanding, and the fault returns.

## Escalation

Growth owns ad and works business hours, which is correct for a service that
does not affect a purchase. Checkout owns email. A confirmation not sent is
worth more urgency than an advert not shown.
