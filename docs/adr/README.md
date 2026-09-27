# Architecture decision records

Format: Context, Decision, Alternatives considered, Consequences, Sources.
The full list of required records is in SPEC.md Section 14.

| ADR | Decision | Status | Phase |
|---|---|---|---|
| [0001](0001-agent-framework.md) | LangGraph as the agent orchestration framework | Accepted | 0 |
| [0002](0002-target-system.md) | OpenTelemetry Demo 3.1.0 as the target system | Accepted | 1 |
| [0003](0003-incident-bundles.md) | Incident bundles as Parquet files, queried with DuckDB | Accepted | 2 |
| [0004](0004-typed-tool-templates.md) | Typed tool templates instead of free-form queries | Accepted | 3 |
| [0005](0005-service-graph.md) | Service dependency edges from the service_graph connector | Accepted | 1 |
| [0006](0006-log-templates.md) | A deterministic masker for log templates, not Drain | Accepted | 4 |
| [0007](0007-candidate-ranking.md) | Candidate ranking by personalised PageRank over the call graph | Accepted | 4 |
| [0008](0008-agent-graph-v1.md) | Agent graph v1, and why the loop is not yet a LangGraph | Accepted | 6 |
| [0009](0009-tiers-cascade-floor.md) | Model tiers, a rule-based cascade, and a deterministic floor | Accepted | 8 |
| [0010](0010-exit-gate.md) | Exit gate with re-execution, and abstention as a separate rule | Accepted | 7 |
| [0011](0011-approval-service-as-sole-writer.md) | The approval service as the only writer, and how recovery is verified | Accepted | 9 |
| [0012](0012-eval-gate-and-splits.md) | A non-inferiority eval gate, and the split design behind it | Accepted | 8 |
| [0013](0013-memory-and-prompt-optimization.md) | Incident memory, and GEPA prompt optimization gated by evals | Accepted | 10 |
