"""Offline prompt optimization, gated by the eval harness.

SPEC.md Section 10.4. Reads the train split and nothing else, writes a candidate
prompt file and a report, and ships nothing: a candidate goes through a pull request
and the eval gate on validation first.
"""
