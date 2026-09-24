---
id: static-assets-and-perceived-slowness
title: The site feels slow but every API call succeeds
covers: [image-provider, frontend]
symptoms:
  - pages feel slow
  - images loading slowly
  - customers complain while dashboards look healthy
updated: '2026-09-24'
---

## Symptoms

Customers report the site is slow while the service dashboards look broadly
healthy. API latency is normal and error rates are normal.

## First checks

This is the family where the metrics disagree with the customer, and the
customer is right.

1. Check the image provider's latency specifically. It serves static assets and
   is easy to overlook precisely because it is not on any API path.
2. Confirm that the frontend's own API calls are healthy. If they are, and the
   page is still slow, the slowness is in what the page loads rather than in
   what it computes.
3. Do not stop at a healthy error rate. Nothing fails in this family; things
   take longer.

## Likely causes

- The asset service is slow.
- The assets themselves changed in size or number.
- Something else entirely, and the reports are about a different problem. Worth
  confirming the complaint before spending long here.

## Remediation

Assets are served by a templated nginx configuration rather than by application
code, so the likely actions are configuration rather than a restart.

## Escalation

Platform owns the image provider. Storefront owns the frontend, and it is worth
telling them the API path is clean so they do not look there.
