---
id: recommendation-and-its-cache
title: Recommendations are slow or the service is growing
covers: [recommendation]
symptoms:
  - recommendation latency rising
  - recommendation memory growing
  - product pages slower than usual
updated: '2026-09-24'
---

## Symptoms

Recommendation is slow, or its memory use is climbing, or both. Often there are
no errors at all, which is what makes it easy to miss.

## First checks

The cache is in process rather than in a datastore, so its behaviour shows up
in the container's memory rather than in a dependency's metrics.

1. Look at the container memory trend across the whole window, not just the
   incident. A cache problem is a trend rather than a step.
2. Compare latency against a baseline. A cache that has stopped being effective
   raises latency without raising errors.
3. Check whether product-catalog, which recommendation calls, is also
   anomalous. If it is, recommendation may be inheriting the problem.
4. Check whether the request rate changed. More distinct lookups means more
   cache pressure without anything being broken.

## Likely causes

- The cache is not doing its job, so every request becomes a call to
  product-catalog.
- Memory growth without release, which raises latency as the process comes
  under pressure. See
  [service-using-too-much-memory](service-using-too-much-memory.md).
- product-catalog being slow, inherited.

## Remediation

A restart genuinely helps a process under memory pressure, and here it costs
only the cache, which is rebuildable. It is one of the few places in this
system where a restart is a reasonable first action rather than a way of hiding
evidence. Capture the memory trend first, since the restart erases it.

## Escalation

Catalogue owns recommendation.
