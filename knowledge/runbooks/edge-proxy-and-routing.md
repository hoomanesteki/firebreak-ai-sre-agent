---
id: edge-proxy-and-routing
title: Requests failing at the edge
covers: [frontend-proxy]
symptoms:
  - every route failing at once
  - errors before any application service is reached
  - observability tools also unreachable
updated: '2026-09-24'
---

## Symptoms

Requests fail at the edge, before reaching an application service. The
distinguishing feature is breadth: the storefront, the load generator UI and
the flag service are all affected together, which no single application
service can cause.

## First checks

1. Check whether anything at all is being served through the proxy. If the
   observability tools are unreachable too, this is the proxy or the host and
   not the application.
2. Look for whether the application services are still emitting telemetry.
   A healthy application behind a broken proxy keeps reporting; a broken
   application goes quiet.
3. Confirm the proxy is routing to where you expect. It is configured by a
   template, so a routing problem is a configuration problem rather than a
   code problem.

## Likely causes

- Proxy configuration, since it routes by template and a template change
  affects every route at once.
- The proxy process itself, or the host it is on.
- Nothing to do with the proxy: an application service failing so completely
  that every route through it fails, which looks similar until you check
  whether the non-application routes work.

## Remediation

A proxy restart is a wide action: everything entering the system goes through
it, so a restart is an outage for every route. Establish that the proxy is
the fault before proposing it, not merely that it is where the errors appear.

## Escalation

Platform owns the proxy. This is not a case to route to an application team
until the application has been shown to be the cause.
