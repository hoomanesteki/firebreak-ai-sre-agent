"""Firebreak's own telemetry: spans about the agent, not about the incident.

SPEC.md Section 6.12 keeps these separate from the telemetry under investigation, and
the separation matters in both directions: an agent span in the incident's pipeline
would look like evidence, and incident evidence in an agent span would ship content
somewhere it does not belong.
"""
