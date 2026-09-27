"""Incident memory: what past incidents taught, and the rule that keeps it honest.

SPEC.md Section 10.3. Memory holds train-split incidents only, because it is written
by the system itself and is therefore the one place a held-out answer can leak into a
run without anybody editing a split file.
"""
