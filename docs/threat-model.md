# Threat model v1

SPEC.md Section 11. Mapped to the OWASP Top 10 for Agentic Applications [R22].

**The premise this document rests on.** Logs, traces, span attributes and alert text
are written by software, and sometimes by users, and sometimes by an attacker who
noticed that a product name ends up in an error message. All of it arrives through
the same path as a genuine stack trace, and none of it can be distinguished from one
by inspection. So Firebreak treats every piece of telemetry as untrusted input, and
the interesting question is never "is this line safe" but "what can this line cause".

**What the answer is, in one paragraph.** A log line can cause Firebreak to look
somewhere, to say something, and to propose one of three actions to a person. It
cannot cause Firebreak to change anything: the agent's process holds no credential
that can, the action space is a three-entry allowlist, and a separate process makes
every change after a human decides. That is the whole design, and everything below
is either an instance of it or an honest statement of where it is thin.

## Scope

**In scope.** The Firebreak agent, its tools, the evaluation harness, the approval
service, and the audit log. The OpenTelemetry Demo as a target, insofar as Firebreak
can act on it.

**Out of scope.** Attacks on the OpenTelemetry Demo itself. Firebreak does not
defend the target system; it investigates it. An attacker who already controls a
demo service has better things to do than manipulate a report about it.

**Assumed trusted.** The repository and its CI, the knowledge files under
`knowledge/`, the scenario specs, and whoever operates the approval service. Each of
those is a real assumption, and the last is the one worth naming: a malicious
approver can do anything the approval service can do, and nothing here prevents it.
What the audit chain gives is that the record of what they did cannot be quietly
edited afterwards.

## Threats and controls

| OWASP | Threat | Controls | Where | Test |
|---|---|---|---|---|
| ASI01 Agent Goal Hijack | A log line or span attribute contains instructions | Untrusted text wrapped and labelled; pattern classifier; flagged items replaced with "suspicious content, withheld" keeping their count | `security/injection.py`, `tools/logs.py` | `tests/security/test_injection_suite.py` |
| ASI02 Tool Misuse | Extreme time ranges, heavy queries, a loop | Typed templates with no free-form query; window and row caps; per-tool call cap; repetition limit; budgets | `tools/base.py`, `backends/base.py`, `agent/budget.py` | `test_tool_base.py`, `test_budget.py` |
| ASI03 Identity and Privilege Abuse | The agent gains write access | The agent's import graph cannot reach anything that writes; the approval service holds the only write path | `remediation/`, import boundary | `tests/security/test_privilege_separation.py` |
| ASI05 Unexpected Code Execution | Free-form PromQL, Cypher, or shell | No free-form query of any kind; no code execution tool; `subprocess` unreachable from the agent | `tools/`, `graph/queries.py` | `test_privilege_separation.py`, `test_tool_registry.py` |
| ASI06 Memory and Context Poisoning | A wrong root cause is saved and reused | Phase 10. Memory will be train-only, advisory, and re-tested rather than trusted | not built | not built |
| ASI08 Cascading Failures | Loops, runaway cost | Budgets on rounds, tool calls, tokens, dollars and wall clock; stall detection; identical-call limit; deterministic floor | `agent/budget.py`, `agent/floor.py` | `test_budget.py`, `test_floor.py` |
| ASI09 Human-Agent Trust Exploitation | A persuasive report leads to a harmful approval | The approval screen shows the action, the blast radius and the re-read evidence as separate fields from the agent's rationale; a reason is required; evidence that cannot be re-read is named | `remediation/approval.py`, `workflow.py` | `test_remediation.py`, `test_approval_workflow.py` |
| ASI10 Rogue Agents | Behaviour drifts outside its scope | Per-node tool allowlists enforced in the brief; every tool call recorded as evidence; an out-of-scope tool request raises | `agent/nodes.py`, `tools/base.py` | `test_agent_graph.py` |

## The controls that actually carry the weight

Three of the above are load-bearing and the rest are depth. Worth separating,
because a reader who believes the injection classifier is the defence will draw the
wrong conclusion from its detection rate.

**1. The agent cannot write.** `tests/security/test_privilege_separation.py` walks
the import graph of every module under `firebreak/agent/` and asserts that nothing
reaching flagd, the container runtime, the load generator or the approval service is
reachable at any depth, and that `subprocess` is not imported. This is a property of
the code rather than of the path a run happened to take.

**2. The action space is three entries.** `knowledge/remediations.yaml` lists every
action Firebreak may propose: turn a named feature flag to a named variant, restart
a named service, or page a team. `build_proposal` refuses any other id, and the
approval service refuses it again. An instruction to drop a table is not a thing
that can be expressed, however persuasively it is phrased, which is why the
strongest test in the injection suite does not depend on the classifier at all.

**3. Every change goes through a person, once.** The proposal id is a hash of the
action and its target, so two approvals of one proposal execute once, and the state
that enforces it is the audit log rather than a process-local set, so a restart does
not execute again.

## Where this model is thin

Stated plainly, because a threat model that lists only what it handles is marketing.

**The injection classifier has false negatives by construction.** It is a pattern
matcher over natural language. On the current suite it catches 8 of 8 crafted
payloads and flags 0 of 10 normal lines, and both numbers are properties of that
suite rather than of the space of attacks. A payload written after reading
`security/injection.py` would very likely get through. The reason this is tolerable
is control 1 and control 2 above: getting through means being shown to a model that
cannot act on it.

**The audit chain is tamper evident, not tamper proof.** Anyone who can write the
file can rewrite the chain from the point they changed. What they cannot do is change
one record and leave the rest verifying. Signing would be stronger and needs a key
this project does not have; `verify_chain` reports the first broken link rather than
claiming the file is authentic.

**A malicious approver is not defended against.** They hold the credentials by
design. The control is that their decisions are recorded with a required reason and
cannot be silently removed.

**The classifier's rates are measured on a suite written by the same person who
wrote the classifier.** That is the weakest thing about the numbers in this
document. An independent red team would produce a different and more useful figure.

**Prompt-level controls are unverified against a real model.** Wrapping untrusted
text and labelling it as data is the control that does not depend on detection, and
whether a given model respects the labelling has not been measured here, because no
model has been run. That measurement needs credentials.

**Nothing here addresses a compromised dependency.** `pip-audit` and `gitleaks` run
in CI, which catches known advisories and committed secrets, and neither is a defence
against a malicious package that has not been reported.

## Operational rules

- No secrets in prompts or in telemetry. The leakage scan checks the first; nothing
  checks the second, and it is a rule rather than a control.
- `.env` is git-ignored. `gitleaks` and `pip-audit` run on every push.
- The demo's services are bound to localhost and never exposed further.
- The approval service's audit log is append-only, one JSON record per line, so
  appending cannot corrupt what is already there.

## Sources

- SPEC.md Sections 6.10, 11, 12.
- [R22] OWASP Top 10 for Agentic Applications.
- ADR-0011 for the privilege separation decision and its alternatives.
- `tests/security/` for every test named above.
