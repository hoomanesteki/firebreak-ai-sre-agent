---
id: traffic-changed-but-nothing-broke
title: The numbers moved because the traffic moved
covers: [load-generator, frontend]
symptoms:
  - every service busier at once
  - latency up with a proportional rise in requests
  - alert fired with no failure anywhere
updated: '2026-09-24'
---

## Symptoms

Metrics across many services move together and nothing is failing. Latency is
up, request rates are up, error rates are flat.

## First checks

Reporting a culprit here is worse than reporting nothing, because it teaches an
operator to distrust the next report that is correct.

1. Check the request rate before the latency. If requests rose and latency rose
   roughly with them, the system is doing more work rather than doing it worse.
2. Check whether error rates moved at all. A system under more load that is
   still succeeding is a capacity observation, not an incident.
3. Check whether the change is uniform across services. A real fault is
   concentrated; a traffic change is broad.
4. Check what the load generator is doing. In this lab it is the reason there
   is any traffic at all, so a change in its behaviour changes every other
   service's numbers without anything being wrong.

## Likely causes

- More traffic, from whatever is driving it.
- A capacity limit reached, which is a real finding and a different one from a
  fault: the system is behaving correctly at a volume it cannot serve.
- A genuine fault that happens to coincide with a traffic change, which is why
  the error rate is worth checking rather than assumed.

## Remediation

Usually none. The correct report says the system was busier and nothing failed,
and names no culprit.

Restarting a service that is merely busy will make things worse by removing
capacity.

## Escalation

Nobody, in most cases. If a capacity limit was genuinely reached, that is a
Platform conversation in working hours rather than a page.
