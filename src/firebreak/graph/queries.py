"""Named, parameterised Cypher. The only place a query string is written.

SPEC.md Section 6.4 requires that all reads go through named, parameterised
queries in this module, for the same reason ADR-0004 refused a free-form
metric query language: a caller that can compose a query can compose one
from a log body, and log bodies are attacker influenced (SPEC.md Section 11,
ASI05).

The rule is mechanical here. Every constant below is a `Final[LiteralString]`,
and the driver types its `query_` argument as `LiteralString`, so mypy
rejects any attempt to pass a string built at runtime. A query that
interpolates a service name into Cypher will not typecheck, which is a
better guarantee than a review that has to notice it.

Every query is also bounded. A dependency walk with no depth limit and a
blast radius with no row cap are both ways to ask the database for most of
the graph and then put it in a model's context.
"""

from __future__ import annotations

from typing import Final, LiteralString

# Cypher requires a variable length path bound to be a literal, so `*1..3`
# below cannot be parameterised and cannot be built with an f-string either:
# interpolating an int would make the query a `str` rather than a
# `LiteralString`, and the type guarantee this module rests on would be gone
# for exactly the queries that walk furthest.
#
# So the bound is written out in the Cypher, and `test_graph_queries.py`
# asserts the text agrees with this constant. Matches the tool layer's own
# `MAX_DEPTH` in `firebreak.tools.topology`.
MAX_DEPTH: Final = 3
MAX_ROWS: Final = 50

# --- Topology ---------------------------------------------------------

# The call graph for a window, in the shape `EdgeRecord` parses. Returning
# the bundle's own field names rather than the graph's property names keeps
# one vocabulary across both sources: a tool must not be able to tell
# whether an edge came from Neo4j or from a Parquet file.
CALL_GRAPH: Final[LiteralString] = """
MATCH (client:Service)-[call:CALLS]->(server:Service)
WHERE call.window = $window
RETURN client.name AS client,
       server.name AS server,
       call.requests_per_second AS requests_per_second,
       call.failures_per_second AS failures_per_second
ORDER BY client, server
"""

# Every recorded window, newest first, so a caller can ask what is available
# instead of guessing a window key and getting an empty graph.
CALL_GRAPH_WINDOWS: Final[LiteralString] = """
MATCH (:Service)-[call:CALLS]->(:Service)
RETURN DISTINCT call.window AS window
ORDER BY window DESC
LIMIT $limit
"""

DOWNSTREAM_DEPENDENCIES: Final[LiteralString] = """
MATCH path = (:Service {name: $service})-[:CALLS*1..3]->(reached:Service)
WHERE length(path) <= $depth
WITH reached.name AS service, min(length(path)) AS depth
RETURN service, depth
ORDER BY depth, service
LIMIT $limit
"""

UPSTREAM_DEPENDENTS: Final[LiteralString] = """
MATCH path = (reached:Service)-[:CALLS*1..3]->(:Service {name: $service})
WHERE length(path) <= $depth
WITH reached.name AS service, min(length(path)) AS depth
RETURN service, depth
ORDER BY depth, service
LIMIT $limit
"""

# Blast radius is deliberately not depth capped: the question is how many
# services are affected, and a cap would silently answer a smaller question.
# The row limit still applies, and a caller that hits it is told so rather
# than handed a truncated set that looks complete.
BLAST_RADIUS: Final[LiteralString] = """
MATCH (affected:Service)-[:CALLS*]->(:Service {name: $service})
RETURN DISTINCT affected.name AS service, affected.tier AS tier
ORDER BY tier, service
LIMIT $limit
"""

# --- Ownership and knowledge ------------------------------------------

OWNING_TEAM: Final[LiteralString] = """
MATCH (team:Team)-[:OWNS]->(service:Service {name: $service})
RETURN team.name AS team, team.escalation AS escalation, team.slug AS slug
"""

RUNBOOKS_FOR_SERVICE: Final[LiteralString] = """
MATCH (runbook:Runbook)-[:COVERS]->(:Service {name: $service})
RETURN runbook.id AS id, runbook.title AS title, runbook.path AS path
ORDER BY runbook.id
LIMIT $limit
"""

# The list comprehensions are not decoration. An OPTIONAL MATCH that finds
# nothing still contributes one row of nulls, so a plain collect over a
# service with no datastore returns `[{name: null, kind: null}]` rather than
# the empty list a caller would reasonably expect, and anything counting the
# result would report one datastore that does not exist.
SERVICE_PROFILE: Final[LiteralString] = """
MATCH (service:Service {name: $service})
OPTIONAL MATCH (team:Team)-[:OWNS]->(service)
OPTIONAL MATCH (service)-[store:READS|WRITES]->(datastore:Datastore)
OPTIONAL MATCH (service)-[queue_edge:PRODUCES|CONSUMES]->(queue:Queue)
OPTIONAL MATCH (service)-[:RUNS_IN]->(container:Container)
RETURN service.name AS name,
       service.language AS language,
       service.tier AS tier,
       service.description AS description,
       team.name AS team,
       [entry IN collect(DISTINCT {name: datastore.name, kind: datastore.kind,
                                   access: type(store)})
        WHERE entry.name IS NOT NULL] AS datastores,
       [entry IN collect(DISTINCT {name: queue.name, role: type(queue_edge)})
        WHERE entry.name IS NOT NULL] AS queues,
       [name IN collect(DISTINCT container.name) WHERE name IS NOT NULL] AS containers
"""

# --- Incident memory --------------------------------------------------

# Only confirmed past incidents are stored (SPEC.md Section 6.4), so this
# never returns a guess that a previous investigation made and nobody
# checked. Phase 10 fills this in; the query is here because the schema is.
SIMILAR_INCIDENTS: Final[LiteralString] = """
MATCH (incident:Incident)-[:ROOT_CAUSE]->(service:Service)
WHERE service.name IN $services
RETURN incident.id AS id,
       incident.started_at AS started_at,
       incident.fault_class AS fault_class,
       service.name AS root_cause
ORDER BY incident.started_at DESC
LIMIT $limit
"""

# --- Integrity --------------------------------------------------------

# The two halves of a checksum over the whole graph, used to prove a second
# load changed nothing (SPEC.md Section 6.4 requires idempotent loads tested
# this way). Counting alone would not catch it: a load that overwrote a
# property with a different value leaves every count identical, so the
# properties themselves are folded in.
#
# Two queries rather than one, because combining them means a second MATCH
# after an aggregation, and on a graph with no relationships at all that
# pattern returns no rows and the fingerprint of an empty graph becomes
# indistinguishable from a failed query. Python sorts and hashes the two
# results, which also keeps the ordering rules in one language instead of
# depending on how Cypher orders a map rendered as a string.
# Aliased to `node_labels` rather than `labels`. The leakage check refuses a
# bare "labels" string literal anywhere the agent can reach, because that is
# the ground truth directory's name, and a Cypher column that happened to
# share it would mean either a false alarm every run or a weakened rule.
# Renaming the column is free; weakening the rule is not.
NODE_FINGERPRINT: Final[LiteralString] = """
MATCH (n)
RETURN labels(n) AS node_labels, properties(n) AS properties
"""

RELATIONSHIP_FINGERPRINT: Final[LiteralString] = """
MATCH (source)-[r]->(target)
RETURN type(r) AS kind,
       coalesce(source.name, source.id, '') AS source,
       coalesce(target.name, target.id, '') AS target,
       properties(r) AS properties
"""

COUNT_BY_LABEL: Final[LiteralString] = """
MATCH (n)
UNWIND labels(n) AS label
RETURN label, count(*) AS count
ORDER BY label
"""

COUNT_BY_RELATIONSHIP: Final[LiteralString] = """
MATCH ()-[r]->()
RETURN type(r) AS kind, count(*) AS count
ORDER BY kind
"""
