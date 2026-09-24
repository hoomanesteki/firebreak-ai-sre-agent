---
id: restarting-a-service-safely
title: When a restart is the right action, and when it hides the fault
covers: [cart, recommendation, email, ad, fraud-detection, payment]
symptoms:
  - a service degraded rather than erroring
  - resource trend rising
  - considering a restart
updated: '2026-09-24'
---

## Symptoms

A service has degraded rather than broken, and a restart looks appealing.

## First checks

A restart very often makes the symptom go away, and that is the problem with
it. It resets the metrics, clears the accumulated state, and destroys the
evidence, so a fault it did not fix returns later with nothing left to diagnose
it from.

Before proposing one:

1. **Is the signal a resource trend or a stall, rather than an error rate?** A
   restart addresses accumulated state. It does nothing for a logic error and
   nothing for a broken dependency.
2. **Have the datastores and queues it depends on been checked?** This is the
   question that matters most. A restart on a service whose store is the real
   problem is the classic way an incident gets closed and reopened.
3. **Is losing the in process state acceptable?** Any cache goes, and for
   recommendation that is most of the point of the service.
4. **Has the evidence been captured?** The memory curve, the CPU trend and the
   window. After the restart they are gone.

## Likely causes

Not a diagnosis runbook. It exists to slow down one specific action.

## Remediation

The restart itself, once all four questions have an answer.

Weigh the blast radius by tier. A supporting tier service can absorb it; a core
tier service means callers see errors until it is serving again; a queue
consumer falls further behind before it catches up.

This is the one action in the allowlist that is not reversible, which is why it
always requires approval.

## Escalation

The owning team, always, since a restart is their service's downtime.
