"""Load the knowledge graph into Neo4j, idempotently.

Every write is a `MERGE`, so loading twice produces the same graph as loading
once. SPEC.md Section 6.4 asks for that to be tested by comparing a checksum
after two loads rather than asserted, and `graph_fingerprint` is what the
test compares.

Idempotence matters more here than it looks. The graph is reloaded whenever
the knowledge files change or a new window of call data arrives, and a load
that appended instead of merging would double every `CALLS` edge. Nothing
would error. The ranking would simply start seeing twice the traffic on
every edge, which changes no proportions and so changes no result, right up
until a partial reload doubles some edges and not others.

**Neo4j is optional.** Bundle replay reads its topology from the bundle's own
`topology.json` and never touches this module, which is why the whole test
suite runs without a database. Neo4j carries what telemetry cannot: who owns
what, which runbook covers which service, and confirmed past incidents.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, LiteralString

from neo4j import Driver, GraphDatabase
from neo4j.exceptions import Neo4jError, ServiceUnavailable

from firebreak.graph import queries
from firebreak.graph.knowledge import Knowledge, load_knowledge
from firebreak.lab.bundle import EdgeRecord

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.cypher"

# Statement separator in schema.cypher. Kept as a constant so the file's own
# comment about not using a semicolon inside a string literal has something
# to point at.
STATEMENT_SEPARATOR = ";"


class GraphError(Exception):
    """The graph is unreachable, or a load did not do what it claimed."""


@dataclass(frozen=True)
class LoadCounts:
    """What a load wrote, for a report and for a sanity check."""

    services: int
    teams: int
    runbooks: int
    datastores: int
    queues: int
    containers: int
    calls: int

    def as_dict(self) -> dict[str, int]:
        return {
            "services": self.services,
            "teams": self.teams,
            "runbooks": self.runbooks,
            "datastores": self.datastores,
            "queues": self.queues,
            "containers": self.containers,
            "calls": self.calls,
        }


@contextmanager
def connect(uri: str, user: str, password: str, database: str = "neo4j") -> Iterator[Driver]:
    """Open a driver, verifying connectivity before handing it over.

    The driver connects lazily, so without `verify_connectivity` the first
    sign of a wrong URI is a confusing failure inside whatever query happens
    to run first, several layers from the configuration that caused it.
    """
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        driver.verify_connectivity()
    except (ServiceUnavailable, Neo4jError) as error:
        driver.close()
        raise GraphError(f"cannot reach Neo4j at {uri}: {error}") from error
    try:
        yield driver
    finally:
        driver.close()


def _statements(text: str) -> list[LiteralString]:
    """Split schema.cypher into executable statements, dropping comments.

    Returns `LiteralString` by assertion rather than by inference: the text
    comes from a file that ships inside the package, not from a caller, and
    the driver requires the type. This is the one place in the graph code
    where a query is not a literal in source, and it reads a file that is
    part of the source tree.
    """
    lines = [line for line in text.splitlines() if not line.strip().startswith("//")]
    chunks = "\n".join(lines).split(STATEMENT_SEPARATOR)
    return [chunk.strip() for chunk in chunks if chunk.strip()]


def apply_schema(driver: Driver, database: str = "neo4j") -> int:
    """Create constraints and indexes. Safe to run against a live graph."""
    statements = _statements(SCHEMA_PATH.read_text(encoding="utf-8"))
    for statement in statements:
        driver.execute_query(statement, database_=database)
    return len(statements)


# --- Writes -----------------------------------------------------------
#
# Each of these is a single parameterised statement over a list, rather than
# one statement per row. A per row loop across a few hundred services is a
# few hundred round trips, and more importantly it is not atomic: a load
# that failed halfway would leave a graph that is neither the old one nor
# the new one.

_MERGE_TEAMS: LiteralString = """
UNWIND $rows AS row
MERGE (team:Team {name: row.name})
SET team.slug = row.slug,
    team.description = row.description,
    team.escalation = row.escalation
"""

_MERGE_SERVICES: LiteralString = """
UNWIND $rows AS row
MERGE (service:Service {name: row.name})
SET service.language = row.language,
    service.tier = row.tier,
    service.description = row.description
WITH service, row
MATCH (team:Team {name: row.owning_team})
MERGE (team)-[:OWNS]->(service)
"""

_MERGE_CONTAINERS: LiteralString = """
UNWIND $rows AS row
MERGE (container:Container {name: row.container})
SET container.service = row.name
WITH container, row
MATCH (service:Service {name: row.name})
MERGE (service)-[:RUNS_IN]->(container)
"""

_MERGE_ENDPOINTS: LiteralString = """
UNWIND $rows AS row
MERGE (endpoint:Endpoint {service: row.service, route: row.route})
WITH endpoint, row
MATCH (service:Service {name: row.service})
MERGE (service)-[:EXPOSES]->(endpoint)
"""

# READS, WRITES and READS_WRITES are separate relationship types in the
# schema, and Cypher will not take a relationship type as a parameter, so
# each gets its own statement rather than a string built from the access
# value. That restriction is a feature: it is what stops a YAML file from
# naming an arbitrary relationship type.
_MERGE_DATASTORE_READS: LiteralString = """
UNWIND $rows AS row
MERGE (datastore:Datastore {name: row.name})
SET datastore.kind = row.kind
WITH datastore, row
MATCH (service:Service {name: row.service})
MERGE (service)-[:READS]->(datastore)
"""

_MERGE_DATASTORE_WRITES: LiteralString = """
UNWIND $rows AS row
MERGE (datastore:Datastore {name: row.name})
SET datastore.kind = row.kind
WITH datastore, row
MATCH (service:Service {name: row.service})
MERGE (service)-[:WRITES]->(datastore)
"""

_MERGE_QUEUE_PRODUCES: LiteralString = """
UNWIND $rows AS row
MERGE (queue:Queue {name: row.name})
WITH queue, row
MATCH (service:Service {name: row.service})
MERGE (service)-[:PRODUCES]->(queue)
"""

_MERGE_QUEUE_CONSUMES: LiteralString = """
UNWIND $rows AS row
MERGE (queue:Queue {name: row.name})
WITH queue, row
MATCH (service:Service {name: row.service})
MERGE (service)-[:CONSUMES]->(queue)
"""

_MERGE_RUNBOOKS: LiteralString = """
UNWIND $rows AS row
MERGE (runbook:Runbook {id: row.id})
SET runbook.title = row.title, runbook.path = row.path
WITH runbook, row
UNWIND row.covers AS covered
MATCH (service:Service {name: covered})
MERGE (runbook)-[:COVERS]->(service)
"""

_MERGE_FEATURE_FLAGS: LiteralString = """
UNWIND $rows AS row
MERGE (flag:FeatureFlag {name: row.name})
"""

# The window is part of the edge's identity, so loading a second window adds
# edges rather than overwriting the first. Without it in the MERGE key, every
# load would rewrite the same edge and the graph could only ever hold one
# window of call data.
_MERGE_CALLS: LiteralString = """
UNWIND $rows AS row
MATCH (client:Service {name: row.client})
MATCH (server:Service {name: row.server})
MERGE (client)-[call:CALLS {window: row.window}]->(server)
SET call.requests_per_second = row.requests_per_second,
    call.failures_per_second = row.failures_per_second,
    call.error_ratio = row.error_ratio
"""


def load_knowledge_graph(
    driver: Driver,
    knowledge: Knowledge | None = None,
    database: str = "neo4j",
) -> LoadCounts:
    """Write the hand authored knowledge into the graph.

    Returns what was written. A caller comparing two loads should compare
    `graph_fingerprint`, not these counts: counts would stay identical if a
    property were overwritten with a different value, which is exactly the
    kind of non-idempotence worth catching.
    """
    facts = knowledge or load_knowledge()

    teams = [
        {
            "name": team.name,
            "slug": team.slug,
            "description": team.description,
            "escalation": team.escalation,
        }
        for team in facts.teams
    ]
    services = [
        {
            "name": service.name,
            "language": service.language,
            "tier": service.tier.value,
            "description": service.description,
            "owning_team": service.owning_team,
            "container": service.container,
        }
        for service in facts.services
    ]
    endpoints = [
        {"service": service.name, "route": endpoint.route}
        for service in facts.services
        for endpoint in service.endpoints
    ]
    runbooks = [
        {
            "id": runbook.id,
            "title": runbook.title,
            "path": runbook.path,
            "covers": list(runbook.covers),
        }
        for runbook in facts.runbooks
    ]

    reads, writes = [], []
    for service in facts.services:
        for store in service.datastores:
            row = {"service": service.name, "name": store.name, "kind": store.kind}
            if store.access.value in {"reads", "reads_writes"}:
                reads.append(row)
            if store.access.value in {"writes", "reads_writes"}:
                writes.append(row)

    produces = [
        {"service": s.name, "name": q.name}
        for s in facts.services
        for q in s.queues
        if q.role.value == "produces"
    ]
    consumes = [
        {"service": s.name, "name": q.name}
        for s in facts.services
        for q in s.queues
        if q.role.value == "consumes"
    ]

    # Ordered so that a MATCH always finds what it needs: teams before the
    # services that reference them, services before everything that hangs
    # off a service.
    for statement, rows in (
        (_MERGE_TEAMS, teams),
        (_MERGE_SERVICES, services),
        (_MERGE_CONTAINERS, services),
        (_MERGE_ENDPOINTS, endpoints),
        (_MERGE_DATASTORE_READS, reads),
        (_MERGE_DATASTORE_WRITES, writes),
        (_MERGE_QUEUE_PRODUCES, produces),
        (_MERGE_QUEUE_CONSUMES, consumes),
        (_MERGE_RUNBOOKS, runbooks),
    ):
        if rows:
            driver.execute_query(statement, rows=rows, database_=database)

    return LoadCounts(
        services=len(services),
        teams=len(teams),
        runbooks=len(runbooks),
        datastores=len({row["name"] for row in reads + writes}),
        queues=len({row["name"] for row in produces + consumes}),
        containers=len({row["container"] for row in services}),
        calls=0,
    )


def load_feature_flags(driver: Driver, names: list[str], database: str = "neo4j") -> int:
    """Record which feature flags exist, and nothing else about them.

    Names only. SPEC.md Section 6.4 specifies `FeatureFlag` as a change
    surface "with no fault labels", and the reason is direct: the demo's own
    flag table maps each flag to the fault class it injects, and copying
    that mapping into the graph would put the answer in the database the
    agent queries. A flag is a thing that changed. Whether a change caused
    an incident is the question, not an attribute.

    Taken as an argument rather than read from a file here, so the caller
    has to decide where the names came from and nothing can quietly start
    reading a scenario library.
    """
    if not names:
        return 0
    rows = [{"name": name} for name in sorted(set(names))]
    driver.execute_query(_MERGE_FEATURE_FLAGS, rows=rows, database_=database)
    return len(rows)


def load_call_edges(
    driver: Driver,
    edges: list[EdgeRecord],
    window: str,
    database: str = "neo4j",
) -> int:
    """Write observed call traffic for one window.

    `window` is an opaque key naming the period the edges were measured
    over. It must not be a scenario name: the graph is read by the agent,
    and a window called `payment-failure-50pct` would be the answer written
    on the wall. A bundle id or an ISO interval is the right shape.
    """
    if not edges:
        return 0
    rows = [
        {
            "client": edge.client,
            "server": edge.server,
            "requests_per_second": edge.requests_per_second,
            "failures_per_second": edge.failures_per_second,
            "error_ratio": edge.error_ratio,
            "window": window,
        }
        for edge in edges
    ]
    driver.execute_query(_MERGE_CALLS, rows=rows, database_=database)
    return len(rows)


def fetch_call_edges(driver: Driver, window: str, database: str = "neo4j") -> list[EdgeRecord]:
    """Read one window's call graph back as the same type a bundle yields.

    Returning `EdgeRecord` rather than a graph specific shape is deliberate:
    `rank_candidates` must not be able to tell whether its edges came from
    Neo4j or from a Parquet file, or the two paths will drift and only one
    of them will be the one that was measured.
    """
    result = driver.execute_query(queries.CALL_GRAPH, window=window, database_=database)
    return [
        EdgeRecord(
            client=record["client"],
            server=record["server"],
            requests_per_second=record["requests_per_second"],
            failures_per_second=record["failures_per_second"],
        )
        for record in result.records
    ]


def graph_fingerprint(driver: Driver, database: str = "neo4j") -> str:
    """A hash of everything in the graph, for proving a load is idempotent.

    Covers node labels and properties and relationship types, endpoints and
    properties. Sorting happens here rather than in Cypher so the ordering
    rule is one thing in one language, and so the hash does not depend on
    how a particular Neo4j version renders a map as a string.
    """
    nodes = driver.execute_query(queries.NODE_FINGERPRINT, database_=database)
    relationships = driver.execute_query(queries.RELATIONSHIP_FINGERPRINT, database_=database)

    node_lines = sorted(
        json.dumps(
            {
                "node_labels": sorted(record["node_labels"]),
                "properties": _stable(record["properties"]),
            },
            sort_keys=True,
        )
        for record in nodes.records
    )
    edge_lines = sorted(
        json.dumps(
            {
                "kind": record["kind"],
                "source": record["source"],
                "target": record["target"],
                "properties": _stable(record["properties"]),
            },
            sort_keys=True,
        )
        for record in relationships.records
    )

    digest = hashlib.sha256()
    for line in node_lines:
        digest.update(line.encode("utf-8"))
    digest.update(b"\x00")
    for line in edge_lines:
        digest.update(line.encode("utf-8"))
    return digest.hexdigest()


def _stable(properties: Any) -> dict[str, str]:
    """Render property values as strings, so a hash does not depend on types.

    Neo4j returns an integer for a value written as an integer and a float
    for the same number written as a float, and two loads that differ only
    that way are the same graph. Comparing rendered text rather than typed
    values keeps the fingerprint answering the question it was asked.
    """
    if not isinstance(properties, dict):
        return {}
    return {str(key): str(value) for key, value in sorted(properties.items())}
