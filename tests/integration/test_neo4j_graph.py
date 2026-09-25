"""Every Cypher query in `firebreak.graph.queries`, run against a real Neo4j.

SPEC.md rule 3 says an API is confirmed rather than assumed, and Cypher is
an API. A query with a typo, a wrong relationship direction, or the
`OPTIONAL MATCH` collect trap that turns a missing match into a list holding
one null, all typecheck perfectly and all return the wrong answer. None of
that can be caught without a database.

Skipped when no Neo4j is reachable, so the ordinary test run needs no
container. Start one with `make graph-up`.

The load itself is the reason to be careful. SPEC.md Section 6.4 requires
loads to be idempotent, tested by comparing a checksum after two loads, and
non-idempotence here would be close to invisible: doubling every `CALLS`
edge changes no proportion, so the ranking would return identical results
right up until a partial reload doubled some edges and not others.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from firebreak.graph import queries
from firebreak.graph.knowledge import Knowledge
from firebreak.graph.load import (
    GraphError,
    apply_schema,
    connect,
    fetch_call_edges,
    graph_fingerprint,
    load_call_edges,
    load_feature_flags,
    load_knowledge_graph,
)
from firebreak.lab.bundle import EdgeRecord

pytestmark = pytest.mark.neo4j

NEO4J_URI = os.environ.get("FIREBREAK_NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("FIREBREAK_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("FIREBREAK_NEO4J_PASSWORD", "firebreak-local")

# An opaque window key. Never a scenario name: the graph is read by the
# agent, and a window called `payment-failure-50pct` would be the answer
# written on the wall.
WINDOW = "inc_0123456789ab"


KNOWLEDGE_YAML = {
    "services": [
        {
            "name": "frontend",
            "language": "typescript",
            "tier": "edge",
            "description": "Serves the storefront.",
            "owning_team": "Storefront",
            "container": "frontend",
            "endpoints": [{"route": "/"}, {"route": "/cart"}],
            "runbooks": ["frontend-latency"],
        },
        {
            "name": "cart",
            "language": "dotnet",
            "tier": "core",
            "description": "Holds carts.",
            "owning_team": "Storefront",
            "container": "cart",
            "datastores": [{"name": "valkey-cart", "kind": "keyvalue", "access": "reads_writes"}],
            "runbooks": ["frontend-latency"],
        },
        {
            "name": "checkout",
            "language": "go",
            "tier": "core",
            "description": "Places orders.",
            "owning_team": "Payments",
            "container": "checkout",
            "queues": [{"name": "orders", "role": "produces"}],
        },
        {
            "name": "accounting",
            "language": "dotnet",
            "tier": "supporting",
            "description": "Records orders.",
            "owning_team": "Payments",
            "container": "accounting",
            "queues": [{"name": "orders", "role": "consumes"}],
        },
    ],
    "teams": [
        {
            "name": "Storefront",
            "slug": "storefront",
            "description": "Owns what a customer sees.",
            "escalation": "#storefront-oncall",
            "services": ["frontend", "cart"],
        },
        {
            "name": "Payments",
            "slug": "payments",
            "description": "Owns money movement.",
            "escalation": "#payments-oncall",
            "services": ["checkout", "accounting"],
        },
    ],
    "remediations": [
        {
            "id": "page-owning-team",
            "kind": "page_owning_team",
            "title": "Page the owning team",
            "description": "Always available fallback.",
            "blast_radius": "One team's pager.",
            "reversible": True,
            "requires_approval": False,
        }
    ],
    "runbooks": [
        {
            "id": "frontend-latency",
            "title": "Storefront latency",
            "covers": ["frontend", "cart"],
            "symptoms": ["slow page loads"],
            "updated": "2026-09-24",
            "path": "knowledge/runbooks/frontend-latency.md",
            "body": "Check upstream dependencies before blaming the edge.",
        }
    ],
}

EDGES = [
    EdgeRecord(
        client="frontend", server="cart", requests_per_second=120.0, failures_per_second=0.0
    ),
    EdgeRecord(
        client="frontend", server="checkout", requests_per_second=40.0, failures_per_second=2.0
    ),
    EdgeRecord(client="checkout", server="cart", requests_per_second=35.0, failures_per_second=0.0),
    EdgeRecord(
        client="checkout", server="accounting", requests_per_second=35.0, failures_per_second=0.0
    ),
]


@pytest.fixture(scope="module")
def knowledge() -> Knowledge:
    """A small hand built knowledge set.

    Deliberately not `knowledge/` from the repository. These tests assert
    exact counts and exact query results, and pinning them to the real files
    would mean every edit to a runbook broke an unrelated integration test.
    """
    return Knowledge.model_validate(KNOWLEDGE_YAML)


# Set in CI, where a Neo4j service is provided and an unreachable database
# means the service is broken rather than absent.
#
# Skipping is the failure mode worth guarding against here, because a skipped
# suite and a passing one look identical in a summary line. Without this the
# Cypher would be verified only on whichever machine happened to have a
# database running, which is the same as not verifying it.
REQUIRE_NEO4J = os.environ.get("FIREBREAK_REQUIRE_NEO4J") == "1"


@pytest.fixture(scope="module")
def driver() -> Iterator[object]:
    try:
        with connect(NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD) as connected:
            yield connected
    except GraphError as error:
        if REQUIRE_NEO4J:
            pytest.fail(
                f"FIREBREAK_REQUIRE_NEO4J is set but no Neo4j is reachable at {NEO4J_URI}: {error}"
            )
        pytest.skip(f"no Neo4j at {NEO4J_URI}: {error}")


@pytest.fixture(scope="module")
def loaded(driver, knowledge):  # type: ignore[no-untyped-def]
    """A graph with the schema applied and the fixture loaded exactly once."""
    driver.execute_query("MATCH (n) DETACH DELETE n")
    apply_schema(driver)
    load_knowledge_graph(driver, knowledge)
    load_call_edges(driver, EDGES, window=WINDOW)
    return driver


class TestSchema:
    def test_every_statement_applies(self, driver) -> None:  # type: ignore[no-untyped-def]
        """And applies twice, since a schema change reaches a live graph this way."""
        first = apply_schema(driver)
        second = apply_schema(driver)
        assert first == second > 0

    def test_the_uniqueness_constraint_actually_bites(self, loaded) -> None:  # type: ignore[no-untyped-def]
        """Two `Service {name: "cart"}` nodes would split cart's edges in half."""
        from neo4j.exceptions import ClientError

        with pytest.raises(ClientError):
            loaded.execute_query("CREATE (:Service {name: 'cart'})")


class TestLoadCounts:
    def test_nodes_are_what_was_described(self, loaded) -> None:  # type: ignore[no-untyped-def]
        result = loaded.execute_query(queries.COUNT_BY_LABEL)
        counts = {record["label"]: record["count"] for record in result.records}
        assert counts["Service"] == 4
        assert counts["Team"] == 2
        assert counts["Runbook"] == 1
        assert counts["Datastore"] == 1
        assert counts["Queue"] == 1
        assert counts["Container"] == 4
        assert counts["Endpoint"] == 2

    def test_relationships_are_what_was_described(self, loaded) -> None:  # type: ignore[no-untyped-def]
        result = loaded.execute_query(queries.COUNT_BY_RELATIONSHIP)
        counts = {record["kind"]: record["count"] for record in result.records}
        assert counts["OWNS"] == 4
        assert counts["CALLS"] == len(EDGES)
        assert counts["RUNS_IN"] == 4
        assert counts["COVERS"] == 2
        # reads_writes becomes two relationships, which is the point of
        # having separate types rather than an access property.
        assert counts["READS"] == 1
        assert counts["WRITES"] == 1
        assert counts["PRODUCES"] == 1
        assert counts["CONSUMES"] == 1


class TestIdempotence:
    def test_a_second_load_changes_nothing(self, loaded, knowledge) -> None:  # type: ignore[no-untyped-def]
        """SPEC.md Section 6.4 asks for exactly this, by checksum."""
        before = graph_fingerprint(loaded)
        load_knowledge_graph(loaded, knowledge)
        load_call_edges(loaded, EDGES, window=WINDOW)
        assert graph_fingerprint(loaded) == before

    def test_a_third_load_still_changes_nothing(self, loaded, knowledge) -> None:  # type: ignore[no-untyped-def]
        before = graph_fingerprint(loaded)
        load_knowledge_graph(loaded, knowledge)
        assert graph_fingerprint(loaded) == before

    def test_the_fingerprint_notices_a_changed_property(self, loaded) -> None:  # type: ignore[no-untyped-def]
        """Counting alone would not, which is why it is a fingerprint.

        A load that overwrote a property with a different value leaves every
        node and relationship count identical.
        """
        before = graph_fingerprint(loaded)
        loaded.execute_query("MATCH (s:Service {name: 'cart'}) SET s.tier = 'edge'")
        try:
            assert graph_fingerprint(loaded) != before
        finally:
            loaded.execute_query("MATCH (s:Service {name: 'cart'}) SET s.tier = 'core'")
        assert graph_fingerprint(loaded) == before

    def test_the_fingerprint_notices_a_changed_edge_weight(self, loaded) -> None:  # type: ignore[no-untyped-def]
        before = graph_fingerprint(loaded)
        loaded.execute_query(
            "MATCH (:Service {name: 'frontend'})-[c:CALLS]->(:Service {name: 'cart'}) "
            "SET c.requests_per_second = 999.0"
        )
        try:
            assert graph_fingerprint(loaded) != before
        finally:
            load_call_edges(loaded, EDGES, window=WINDOW)
        assert graph_fingerprint(loaded) == before


class TestCallGraph:
    def test_edges_round_trip_as_the_same_type_a_bundle_yields(self, loaded) -> None:  # type: ignore[no-untyped-def]
        """`rank_candidates` must not be able to tell the sources apart."""
        assert fetch_call_edges(loaded, WINDOW) == sorted(EDGES, key=lambda e: (e.client, e.server))

    def test_an_unknown_window_is_empty_rather_than_an_error(self, loaded) -> None:  # type: ignore[no-untyped-def]
        assert fetch_call_edges(loaded, "inc_ffffffffffff") == []

    def test_a_second_window_does_not_overwrite_the_first(self, loaded) -> None:  # type: ignore[no-untyped-def]
        """The window is part of the edge key, so both must survive.

        Without it in the MERGE key the graph could only ever hold one
        window of call data, and every load would silently discard the last.
        """
        other = "inc_aaaaaaaaaaaa"
        changed = [
            EdgeRecord(
                client="frontend", server="cart", requests_per_second=7.0, failures_per_second=1.0
            )
        ]
        load_call_edges(loaded, changed, window=other)
        try:
            assert fetch_call_edges(loaded, other) == changed
            assert fetch_call_edges(loaded, WINDOW) == sorted(
                EDGES, key=lambda e: (e.client, e.server)
            )
        finally:
            loaded.execute_query("MATCH ()-[c:CALLS {window: $window}]->() DELETE c", window=other)

    def test_windows_can_be_listed(self, loaded) -> None:  # type: ignore[no-untyped-def]
        result = loaded.execute_query(queries.CALL_GRAPH_WINDOWS, limit=10)
        assert [record["window"] for record in result.records] == [WINDOW]


class TestTopologyQueries:
    def test_downstream_finds_transitive_dependencies(self, loaded) -> None:  # type: ignore[no-untyped-def]
        result = loaded.execute_query(
            queries.DOWNSTREAM_DEPENDENCIES, service="frontend", depth=3, limit=queries.MAX_ROWS
        )
        reached = {record["service"]: record["depth"] for record in result.records}
        assert reached == {"cart": 1, "checkout": 1, "accounting": 2}

    def test_depth_one_stops_at_one_hop(self, loaded) -> None:  # type: ignore[no-untyped-def]
        result = loaded.execute_query(
            queries.DOWNSTREAM_DEPENDENCIES, service="frontend", depth=1, limit=queries.MAX_ROWS
        )
        assert {record["service"] for record in result.records} == {"cart", "checkout"}

    def test_upstream_walks_the_other_way(self, loaded) -> None:  # type: ignore[no-untyped-def]
        result = loaded.execute_query(
            queries.UPSTREAM_DEPENDENTS, service="cart", depth=3, limit=queries.MAX_ROWS
        )
        assert {record["service"] for record in result.records} == {"frontend", "checkout"}

    def test_blast_radius_is_not_depth_capped(self, loaded) -> None:  # type: ignore[no-untyped-def]
        """It answers "how many are affected", and a cap answers a smaller question."""
        result = loaded.execute_query(
            queries.BLAST_RADIUS, service="accounting", limit=queries.MAX_ROWS
        )
        assert {record["service"] for record in result.records} == {"frontend", "checkout"}

    def test_the_query_text_agrees_with_the_declared_depth_cap(self) -> None:
        """Cypher cannot parameterise a path bound, so the literal is checked.

        If `MAX_DEPTH` were raised without editing the Cypher, every caller
        would believe it could walk further than the query allows.
        """
        bound = f"*1..{queries.MAX_DEPTH}"
        assert bound in queries.DOWNSTREAM_DEPENDENCIES
        assert bound in queries.UPSTREAM_DEPENDENTS


class TestKnowledgeQueries:
    def test_ownership_resolves_to_a_pageable_team(self, loaded) -> None:  # type: ignore[no-untyped-def]
        result = loaded.execute_query(queries.OWNING_TEAM, service="checkout")
        record = result.records[0]
        assert record["team"] == "Payments"
        assert record["escalation"] == "#payments-oncall"

    def test_runbooks_are_found_by_service(self, loaded) -> None:  # type: ignore[no-untyped-def]
        result = loaded.execute_query(
            queries.RUNBOOKS_FOR_SERVICE, service="cart", limit=queries.MAX_ROWS
        )
        assert [record["id"] for record in result.records] == ["frontend-latency"]

    def test_a_service_profile_collects_its_attachments(self, loaded) -> None:  # type: ignore[no-untyped-def]
        result = loaded.execute_query(queries.SERVICE_PROFILE, service="cart")
        record = result.records[0]
        assert record["team"] == "Storefront"
        assert record["containers"] == ["cart"]
        assert sorted(entry["access"] for entry in record["datastores"]) == ["READS", "WRITES"]

    def test_a_service_with_no_datastore_collects_an_empty_list(self, loaded) -> None:  # type: ignore[no-untyped-def]
        """The OPTIONAL MATCH trap, asserted.

        A plain collect over a missing optional match returns a list holding
        one map of nulls, so anything counting the result would report one
        datastore that does not exist.
        """
        result = loaded.execute_query(queries.SERVICE_PROFILE, service="checkout")
        record = result.records[0]
        assert record["datastores"] == []
        assert [entry["name"] for entry in record["queues"]] == ["orders"]

    def test_an_unknown_service_returns_nothing(self, loaded) -> None:  # type: ignore[no-untyped-def]
        result = loaded.execute_query(queries.SERVICE_PROFILE, service="does-not-exist")
        assert result.records == []


class TestFeatureFlags:
    def test_flags_are_names_and_nothing_else(self, loaded) -> None:  # type: ignore[no-untyped-def]
        """SPEC.md Section 6.4: a change surface, with no fault labels.

        The demo's own flag table maps each flag to the fault class it
        injects. Copying that into the graph would put the answer in the
        database the agent queries, so this asserts the node carries a name
        and nothing more.
        """
        load_feature_flags(loaded, ["paymentFailure", "adHighCpu"])
        try:
            result = loaded.execute_query(
                "MATCH (f:FeatureFlag) RETURN f.name AS name, properties(f) AS props "
                "ORDER BY f.name"
            )
            assert [record["name"] for record in result.records] == [
                "adHighCpu",
                "paymentFailure",
            ]
            for record in result.records:
                assert set(record["props"]) == {"name"}
        finally:
            loaded.execute_query("MATCH (f:FeatureFlag) DETACH DELETE f")

    def test_loading_no_flags_is_not_an_error(self, loaded) -> None:  # type: ignore[no-untyped-def]
        assert load_feature_flags(loaded, []) == 0


class TestConnection:
    def test_an_unreachable_database_fails_with_its_address(self) -> None:
        """The driver connects lazily, so without an explicit check the first
        sign of a wrong URI is a confusing error several layers away."""
        with (
            pytest.raises(GraphError, match="cannot reach Neo4j"),
            connect("bolt://127.0.0.1:1", NEO4J_USER, NEO4J_PASSWORD),
        ):
            pass
