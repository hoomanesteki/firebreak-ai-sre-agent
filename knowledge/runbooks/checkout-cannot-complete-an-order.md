---
id: checkout-cannot-complete-an-order
title: Orders are not completing
covers: [checkout]
symptoms:
  - checkout returning errors
  - orders failing at the final step
  - checkout latency spiking
updated: '2026-09-24'
---

## Symptoms

Customers cannot complete a purchase, or checkout is slow enough that they give
up.

## First checks

Checkout calls cart, currency, payment, shipping, email and product-catalog,
then produces to Kafka. That fan out means a fault almost anywhere in the shop
reaches checkout, so it is frequently anomalous and infrequently the cause.

1. Take a failing trace and read the critical path. With this many
   dependencies, the span breakdown is the fastest route to an answer and rate
   comparison is the slowest.
2. Check which dependency is failing or slow, then follow that service's
   runbook rather than continuing here.
3. Note whether the order was placed. An error before the charge and an error
   after it are different incidents with different urgency, and the Kafka
   produce sits at the end.

## Likely causes

- One of its six dependencies. Payment and cart are on the path of every
  order, so their faults are the most visible here.
- Kafka, if the order completes but nothing downstream records it. That is a
  different symptom and belongs to
  [queue-consumers-falling-behind](queue-consumers-falling-behind.md).
- Checkout's own logic, which is worth checking once the dependencies are
  ruled out and not before.

## Remediation

Follow the runbook of whichever dependency the trace names. If checkout itself
is the origin, page Checkout.

## Escalation

Checkout owns it, and owns payment and email too, so most of these stay with
one team.
