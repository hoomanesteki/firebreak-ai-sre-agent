---
id: queue-consumers-falling-behind
title: Order consumers are falling behind
covers: [fraud-detection, accounting, checkout]
symptoms:
  - orders placed but not recorded downstream
  - consumer lag growing
  - no errors anywhere but data is missing
updated: '2026-09-24'
---

## Symptoms

Orders complete for the customer but are not recorded downstream. Often nothing
errors at all: the work is queued and waiting, which is why this family can run
for a long time before anyone notices.

## First checks

Checkout produces to the orders queue; fraud-detection and accounting consume
from it. Whether one or both consumers are affected is the question that
separates the two explanations.

1. Check whether both consumers are behind or only one. Both, together, points
   at the queue or at the producer. One points at that consumer.
2. Check whether checkout is still producing successfully. A customer
   completing an order does not prove the produce succeeded.
3. Look for lag rather than for errors. This family's signature is an absence:
   work accepted and not done.
4. Check the consumers' own resource trends. A consumer that is slow because it
   is under memory or CPU pressure is a different problem from one that cannot
   reach the queue.

## Likely causes

- The queue itself, if both consumers are affected together.
- One consumer, if only one is affected.
- The producer, if nothing is arriving at all.
- A consumer under resource pressure, which processes slowly rather than not at
  all. See
  [jvm-services-and-garbage-collection](jvm-services-and-garbage-collection.md)
  for fraud-detection.

## Remediation

Restarting a consumer that is behind makes it further behind before it catches
up, and does nothing if the queue is the problem. Establish which of the two it
is first.

## Escalation

Growth owns fraud-detection, Platform owns accounting, Checkout owns the
producer. Both consumers affected together is a Platform question.
