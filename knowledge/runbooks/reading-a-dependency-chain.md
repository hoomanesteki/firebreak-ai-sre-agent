---
id: reading-a-dependency-chain
title: Telling a cause from a symptom in a call chain
covers: [frontend, checkout]
symptoms:
  - several services unwell at once
  - unsure which service in a chain is at fault
updated: '2026-09-24'
---

## Symptoms

Several services look unwell at the same time and it is not obvious which one
broke. This is the normal shape of an incident here, not an unusual one.

## First checks

A fault propagates towards the customer, so the services with the most
alarming numbers are usually the ones furthest from the cause.

1. Write down which services are anomalous and how they call each other.
2. Walk the chain from the customer inward. At each hop ask whether this
   service's own work is failing, or whether it is waiting on the next hop.
   A span breakdown answers this and a metric rate does not.
3. Compare onset times. A cause starts before its symptoms. If two services
   appear to have started at the same instant, the comparison window probably
   opened after both had already gone wrong, and onset tells you nothing.
4. Look for the deepest anomalous service in the chain, then check whether
   anything it depends on is anomalous too. Stop where the anomalies stop.

## Likely causes

- The deepest anomalous service in the chain, most often.
- A shared dependency, if the anomalous services do not form a single chain
  but do have something in common.
- Two unrelated faults at once, which looks like a chain that does not quite
  connect. Worth considering before forcing one explanation onto everything.

## Remediation

None directly. This runbook produces a named service; that service's runbook
says what to do about it.

## Escalation

Whoever owns the service the chain ends at. Bring the chain, the onset order,
and one trace.
