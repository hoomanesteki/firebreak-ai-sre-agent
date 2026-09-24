---
id: jvm-services-and-garbage-collection
title: A JVM service is stalling or saturating a core
covers: [ad, fraud-detection]
symptoms:
  - CPU saturated on one service
  - latency spiky rather than uniformly high
  - pauses with no errors
updated: '2026-09-24'
---

## Symptoms

CPU is saturated on a JVM service, or its latency has become spiky rather than
uniformly higher, or it pauses periodically while returning correct responses.

## First checks

The JVM services are the only ones here where garbage collection is a visible
operational concern, and its signature is distinctive.

1. Look at container CPU. A collector working hard saturates a core while the
   service's own work has not increased.
2. Compare p50 against p95. Collection pauses hit some requests hard and leave
   others untouched, so a p95 that has moved much more than p50 points this
   way. A uniform increase across both points elsewhere.
3. Check the error rate. Pauses produce timeouts at the caller, not errors at
   the service, so the caller may look worse than the service does.
4. Check memory alongside CPU. Pressure and collection go together, and either
   one alone means something different.

## Likely causes

- Heap pressure driving collection, from retained memory or from a workload
  that needs more heap than it has.
- CPU saturation from the service's own work, which is a different problem with
  a similar CPU curve and a different latency shape.
- Neither: a caller sending far more traffic than usual.

## Remediation

A restart clears accumulated heap and will make the symptom go away for a
while. That makes it both useful and misleading, so capture the CPU and memory
trends first.

Weigh the blast radius. Ad is supporting tier and a page renders without it, so
a restart there is cheap. Fraud detection consumes a queue, so a restart means
a consumer falls further behind before it catches up.

## Escalation

Growth owns both. Neither is on the path of a customer completing a purchase,
so these rarely need waking anyone.
