"""Remediation: proposing a change, approving it, and carrying it out.

Split across three modules along the privilege boundary SPEC.md Section 6.10
requires, and the split is the design:

- `proposal` builds a statement about what should happen. It holds no credential
  and has no method that changes anything.
- `audit` is the hash-chained record of every decision.
- `approval` is the only thing that can execute, and it is meant to run as a
  separate process holding the only credentials that can change flagd or restart a
  container.

An agent imports `proposal`. It must never import `approval`, and a test asserts
that.
"""
