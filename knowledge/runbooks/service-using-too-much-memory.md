---
id: service-using-too-much-memory
title: A service is using more memory than it should
covers: [email, recommendation, ad]
symptoms:
  - container memory climbing steadily
  - latency rising with no errors
  - a service restarting on its own
updated: '2026-09-24'
---

## Symptoms

Container memory climbs steadily rather than stepping up, latency rises with
few or no errors, and eventually the process may be killed and restarted.

## First checks

This family is the hardest to catch and the easiest to explain once caught,
because the signal is a slope rather than an event.

1. Look at container memory over the whole recording rather than the incident
   window. A slope needs a long baseline to be visible; comparing two short
   windows will understate it.
2. Check the error rate. A leaking process usually keeps serving correct
   responses until it cannot, so an error rate near normal alongside a rising
   memory curve is characteristic rather than contradictory.
3. Check whether latency rose in step with memory, which distinguishes a
   process under pressure from one that is merely holding more than it used to.
4. Check whether the container was restarted during the window. A sawtooth
   memory curve is a process being killed and coming back.

## Likely causes

- Memory retained and not released, by the application or by a cache without
  a bound.
- A workload change that legitimately needs more memory, which looks the same
  for the first few minutes and then levels off where a leak would not.
- On a JVM service, heap and garbage collection rather than a leak. See
  [jvm-services-and-garbage-collection](jvm-services-and-garbage-collection.md).

## Remediation

A restart is the standard response and it is genuinely appropriate here, since
the problem is accumulated state. It is also destructive of the evidence:
capture the memory trend and the window before proposing it, because without
them the same incident recurs in an hour with nothing left to diagnose.

## Escalation

The owning team for the affected service. Checkout for email, Catalogue for
recommendation, Growth for ad.
