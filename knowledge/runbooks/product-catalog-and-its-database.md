---
id: product-catalog-and-its-database
title: Product lookups are failing or slow
covers: [product-catalog]
symptoms:
  - product pages failing
  - product lookups slow
  - several services degraded together
updated: '2026-09-24'
---

## Symptoms

Product lookups fail or are slow. Because frontend, checkout and recommendation
all call this service, the visible symptom is usually several services
degrading at once.

## First checks

That breadth is the useful clue. A shared dependency failing looks different
from three independent faults.

1. Check whether the anomalous services are all callers of product-catalog. If
   so, the shared dependency is the more economical explanation.
2. Look at product-catalog's own error rate and latency against a baseline.
3. Read a trace breakdown. Time spent in the database call points at the
   database; time spent in the service's own spans points at the service.
4. Check the astronomy database directly before concluding.

## Likely causes

- The database: unreachable, slow, contended, or holding a lock.
- Query cost, if latency rose without errors and without a resource trend.
- The service's own code or configuration.

## Remediation

Restarting product-catalog will not help a database problem and will make the
next diagnosis harder. See
[datastore-dependency-failures](datastore-dependency-failures.md).

Bear the blast radius in mind when proposing anything: three services depend
on this one, so an action here is felt widely.

## Escalation

Catalogue owns product-catalog and the astronomy database.
