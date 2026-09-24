---
id: shipping-and-quote-latency
title: Delivery pricing is slow or failing
covers: [shipping, quote]
symptoms:
  - shipping latency high
  - delivery costs failing to load
  - checkout slow at the shipping step
updated: '2026-09-24'
---

## Symptoms

Shipping is slow or failing, which shows up as a slow checkout step or a
delivery cost that will not load.

## First checks

Shipping calls quote and quote is called by nothing else, so this pair is
unusually easy to reason about: there are only two places the fault can be.

1. Compare shipping's latency against a baseline, then quote's.
2. Read a trace breakdown across the pair. Time inside the quote span points at
   quote; time in shipping's own spans points at shipping.
3. Check quote's error rate separately. Quote failing reaches a customer only
   through shipping, so shipping's numbers can look worse than quote's while
   quote is the cause.

## Likely causes

- Quote, since it does the arithmetic and shipping mostly waits for it.
- Shipping's own handling of the call, including how it behaves when quote is
  slow.
- Load, since both are called on the checkout path.

## Remediation

Neither service holds a datastore, so a restart has little to recommend it
without a resource trend.

## Escalation

Fulfilment owns both, so a fault in either is one team's problem.
