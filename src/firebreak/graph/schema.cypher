// Firebreak knowledge graph schema: constraints and indexes.
//
// Applied by `firebreak.graph.load.apply_schema` before any load. Every
// statement is idempotent, so running it against a populated database is
// safe and is in fact how a schema change reaches an existing deployment.
//
// Constraints rather than plain indexes wherever a key must be unique,
// because a uniqueness constraint creates its own index and additionally
// makes a duplicate an error rather than a silently doubled node. A graph
// holding two `Service {name: "cart"}` nodes would split that service's
// edges between them, and a dependency query would return half the truth
// with nothing to indicate it.
//
// The loader splits this file on semicolons, so do not put one inside a
// string literal here.

// --- Identity ---------------------------------------------------------

CREATE CONSTRAINT service_name IF NOT EXISTS
FOR (s:Service) REQUIRE s.name IS UNIQUE;

CREATE CONSTRAINT datastore_name IF NOT EXISTS
FOR (d:Datastore) REQUIRE d.name IS UNIQUE;

CREATE CONSTRAINT queue_name IF NOT EXISTS
FOR (q:Queue) REQUIRE q.name IS UNIQUE;

CREATE CONSTRAINT container_name IF NOT EXISTS
FOR (c:Container) REQUIRE c.name IS UNIQUE;

CREATE CONSTRAINT team_name IF NOT EXISTS
FOR (t:Team) REQUIRE t.name IS UNIQUE;

CREATE CONSTRAINT runbook_id IF NOT EXISTS
FOR (r:Runbook) REQUIRE r.id IS UNIQUE;

CREATE CONSTRAINT feature_flag_name IF NOT EXISTS
FOR (f:FeatureFlag) REQUIRE f.name IS UNIQUE;

CREATE CONSTRAINT incident_id IF NOT EXISTS
FOR (i:Incident) REQUIRE i.id IS UNIQUE;

// An endpoint is identified by its service and route together, since two
// services may each expose `/health` and they are not the same endpoint.

CREATE CONSTRAINT endpoint_identity IF NOT EXISTS
FOR (e:Endpoint) REQUIRE (e.service, e.route) IS UNIQUE;

// --- Lookups ----------------------------------------------------------

// Tier narrows the candidate set in blast radius queries, which otherwise
// scan every service node.

CREATE INDEX service_tier IF NOT EXISTS
FOR (s:Service) ON (s.tier);

// Incident memory is searched by when and by what broke, never by id,
// because nobody looking for a similar past incident knows its identifier.

CREATE INDEX incident_started_at IF NOT EXISTS
FOR (i:Incident) ON (i.started_at);

CREATE INDEX incident_fault_class IF NOT EXISTS
FOR (i:Incident) ON (i.fault_class);
