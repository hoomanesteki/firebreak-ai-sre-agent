---
id: datastore-dependency-failures
title: A service cannot reach its datastore
covers: [cart, product-catalog]
symptoms:
  - connection refused or timed out to a datastore
  - a service erroring on every request
  - latency dominated by one downstream call
updated: '2026-09-24'
---

## Symptoms

A service reports connection failures, timeouts, or a latency profile
dominated by a single downstream call to a database or keystore.

## First checks

1. Read the service's error templates. A store problem announces itself:
   connection refused, timed out, pool exhausted, deadline exceeded.
2. Look at a trace breakdown. If nearly all the time sits in one span and that
   span is the store call, the service's own code is not the problem.
3. Check whether every request fails or only some. Total failure suggests the
   store is unreachable; partial failure more often suggests saturation,
   contention or a pool limit.
4. Check whether other services sharing that store are affected. Only one
   service reading a store makes this harder to separate, so check the store's
   own signals directly.

## Likely causes

- The store is down, full, or refusing connections.
- The store is reachable but slow, from contention, a lock, or a query that
  became expensive.
- The connection pool between them is exhausted, which looks like the store
  being slow from the caller's side and like nothing at all from the store's.
- Credentials or configuration, which usually fails completely and from the
  first request rather than gradually.

## Remediation

Do not restart the calling service. It will not fix a store problem and it
will destroy what you need to tell the two apart.

If the store itself needs action, that is outside what Firebreak may do: page
the owning team.

## Escalation

The team that owns the calling service, because they own the relationship with
the store. Storefront for cart and Valkey, Catalogue for product-catalog and
the astronomy database.
