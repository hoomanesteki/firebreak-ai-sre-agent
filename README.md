# Firebreak

A multi-agent AI SRE that investigates production incidents, finds the root
cause, and cites every piece of evidence, measured on a library of
reproducible fault-injected incidents.

## Status

Under construction. Phase 0 of 12 is complete (SPEC.md Section 17). Result
numbers below are placeholders until the phase that produces them runs.

## The problem

When an online business breaks, engineers are paged and spend most of the
incident searching across services, dashboards, logs, and recent changes. A
PagerDuty survey of 500 IT leaders found customer-facing incidents rose 43%
over 12 months, took 175 minutes on average to resolve, and cost about USD
794,000 per incident ([PagerDuty](https://www.pagerduty.com/newsroom/study-cost-of-incidents/)).
Catchpoint's SRE Report 2025 found median toil rose to 30% from 25%, the
first increase in five years ([Catchpoint](https://www.catchpoint.com/press-releases/the-sre-report-2025-highlighting-critical-trends-in-site-reliability-engineering)).

## What Firebreak does differently

1. Deterministic first, LLM second. Anomaly detection and topology ranking
   run before any model call and produce a candidate list.
2. Specialists plus an independent critic. Separate agents gather evidence
   per signal; a critic with its own prompt tries to refute each hypothesis.
3. Gates at both ends. An entry gate scopes the alert and treats log text as
   untrusted data. An exit gate removes any report sentence whose cited
   evidence does not re-run and match.
4. Calibrated confidence and the right to abstain, rather than a confident
   guess.
5. A cost-aware model cascade with a deterministic floor, so an on-call
   engineer always gets a report.
6. An eval harness over reproducible fault-injected incidents, with
   baselines, ablations, and a statistical CI gate.

## Demo

TBD (produced in Phase 12).

## Results

<!-- stats:start -->
TBD (produced in Phase 12). Every number here is generated from
`reports/site_stats.json` by `scripts/build_site_stats.py`.
<!-- stats:end -->

## Quickstart

```bash
git clone git@github.com:hoomanesteki/firebreak-ai-sre-agent.git
cd firebreak-ai-sre-agent
make setup
make verify
```

`make demo` (offline replay), `make demo-local` (local models), and
`make live` (the full OpenTelemetry Demo) arrive in later phases.

## Architecture

TBD (produced in Phase 6). The design is in SPEC.md Sections 4 to 6, and the
binding harness principles are in Section 5.

## How reports are verified

Every claim in a report is a structured object with evidence IDs. Before a
report is published, a code-only exit gate re-runs each cited query and
compares the result. Claims that fail after one repair pass are removed and
counted. See SPEC.md Section 6.9.

## Evaluation

Incidents are recorded from the OpenTelemetry Demo with faults injected
through its feature flags, which gives a true label for every incident.
Splits are held out by scenario, with a separate out-of-distribution split of
entirely unseen fault families. Metrics include top-1 and top-3 root-cause
accuracy, calibration, pass^3 reliability, evidence validity, cost, and time
to root cause. See SPEC.md Section 9.

## Security and limitations

Logs, traces, and alert text are written by software and sometimes by
attackers, so Firebreak treats them as untrusted input. Agents hold
read-only credentials; the only write path is a separate approval service.
See SPEC.md Section 11.

Known limits of the benchmark: faults injected through feature flags are
cleaner than real incidents, with one clear cause and a known onset. The
distractor, double-fault, and no-fault families exist to make it harder, and
the out-of-distribution split checks generalisation, but this is still easier
than production.

## Not in v1

Kubernetes tooling, paging and chat integrations, autonomous remediation
without approval, model fine-tuning, and multi-tenant deployment. See
SPEC.md Section 3.2.

## Development

- `SPEC.md` is the contract. `CLAUDE.md` and `AGENTS.md` hold the working
  rules.
- `make verify` runs lint, types, tests, and repository hygiene.
- Commit messages follow Conventional Commits and are checked by a hook.

## License

MIT. See `LICENSE`.
