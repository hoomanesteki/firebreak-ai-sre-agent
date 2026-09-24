---
id: cart-and-its-keystore
title: The basket is failing or losing items
covers: [cart]
symptoms:
  - errors adding to basket
  - baskets appearing empty
  - basket operations slow
updated: '2026-09-24'
---

## Symptoms

Adding to or reading a basket fails, is slow, or returns an empty basket for a
customer who had items in it.

## First checks

Cart cannot serve a request without its Valkey keystore, so the first question
is always whether the service or the store is the problem. They fail
differently.

1. Look at the cart error rate and latency against a baseline.
2. Check whether cart's errors mention the keystore. A service failing to
   reach its store says so in its logs, and that single fact resolves most of
   these incidents.
3. Cluster cart's error lines into templates. One dominant template is a
   single mechanism; several unrelated ones are more often load.
4. Check the keystore's own health before concluding anything about cart.

## Likely causes

- The keystore is unreachable, slow, or refusing connections, and cart is
  reporting that faithfully.
- Cart's own logic or its connection handling.
- Cart is fine and its caller is the problem. Check who is calling and at what
  rate.

## Remediation

Restarting cart when the keystore is the problem clears the symptom, resets
the metrics, and leaves the fault in place. Check the store first. See
[datastore-dependency-failures](datastore-dependency-failures.md).

## Escalation

Storefront owns cart.
