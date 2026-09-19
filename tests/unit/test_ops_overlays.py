"""Tests for the operations overlays in ops/.

These guard the two mistakes that are silent rather than loud: a collector
pipeline that drops an upstream entry because the collector replaces arrays
instead of appending to them, and a published port that reaches beyond
localhost. Both are checked against the pinned demo's own files, so moving
the submodule pin fails here rather than at recording time.
"""

import json
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
OPS = REPO_ROOT / "ops"
VENDOR_COLLECTOR = REPO_ROOT / "vendor" / "otel-demo" / "src" / "otel-collector"

CONNECTOR_NAME = "service_graph"


class _TagTolerantLoader(yaml.SafeLoader):
    """Parses Compose files that use merge tags such as !override."""


def _keep_value(loader: yaml.Loader, node: yaml.Node) -> object:
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    return loader.construct_scalar(node)


_TagTolerantLoader.add_multi_constructor(
    "!", lambda loader, suffix, node: _keep_value(loader, node)
)


def load_yaml(path: Path, tolerant: bool = False) -> dict:
    loader = _TagTolerantLoader if tolerant else yaml.SafeLoader
    with path.open(encoding="utf-8") as handle:
        return yaml.load(handle, Loader=loader)


@pytest.fixture(scope="module")
def extras() -> dict:
    return load_yaml(OPS / "otelcol-config-extras.yml")


@pytest.fixture(scope="module")
def upstream_full() -> dict:
    return load_yaml(VENDOR_COLLECTOR / "otelcol-config-full.yml")


@pytest.fixture(scope="module")
def upstream_observability() -> dict:
    return load_yaml(VENDOR_COLLECTOR / "otelcol-config-observability.yml")


def test_collector_overlay_declares_the_service_graph_connector(extras):
    assert CONNECTOR_NAME in extras["connectors"]


def test_collector_overlay_keeps_every_upstream_metrics_receiver(extras, upstream_full):
    upstream = set(upstream_full["service"]["pipelines"]["metrics"]["receivers"])
    ours = set(extras["service"]["pipelines"]["metrics"]["receivers"])
    assert upstream - ours == set(), "overlay drops upstream metrics receivers"


def test_collector_overlay_adds_only_the_connector_to_metrics(extras, upstream_full):
    upstream = set(upstream_full["service"]["pipelines"]["metrics"]["receivers"])
    ours = set(extras["service"]["pipelines"]["metrics"]["receivers"])
    assert ours - upstream == {CONNECTOR_NAME}


def test_collector_overlay_keeps_every_upstream_traces_exporter(extras, upstream_observability):
    upstream = set(upstream_observability["service"]["pipelines"]["traces"]["exporters"])
    ours = set(extras["service"]["pipelines"]["traces"]["exporters"])
    assert upstream - ours == set(), "overlay drops upstream traces exporters"


def test_collector_overlay_adds_only_the_connector_to_traces(extras, upstream_observability):
    upstream = set(upstream_observability["service"]["pipelines"]["traces"]["exporters"])
    ours = set(extras["service"]["pipelines"]["traces"]["exporters"])
    assert ours - upstream == {CONNECTOR_NAME}


def test_collector_overlay_keeps_kafka_metrics_the_queue_family_needs(extras):
    assert "kafkametrics" in extras["service"]["pipelines"]["metrics"]["receivers"]


def test_collector_overlay_does_not_touch_the_logs_pipeline(extras):
    assert "logs" not in extras["service"]["pipelines"]


def test_collector_overlay_store_ttl_exceeds_the_longest_injected_delay(extras):
    ttl = extras["connectors"][CONNECTOR_NAME]["store"]["ttl"]
    assert ttl.endswith("s")
    assert int(ttl.removesuffix("s")) >= 10


@pytest.fixture(scope="module")
def compose_overlay() -> dict:
    return load_yaml(OPS / "compose.live.yml", tolerant=True)


def published_ports(service: dict) -> list[str]:
    return [str(entry) for entry in service.get("ports", [])]


def test_compose_overlay_binds_every_published_port_to_localhost(compose_overlay):
    offenders = []
    for name, service in compose_overlay["services"].items():
        for entry in published_ports(service):
            if entry.count(":") < 2 or not entry.startswith("127.0.0.1:"):
                offenders.append(f"{name}: {entry}")
    assert offenders == [], f"ports reachable beyond localhost: {offenders}"


def test_compose_overlay_publishes_the_backends_the_tool_layer_queries(compose_overlay):
    services = compose_overlay["services"]
    assert any("16686" in p for p in published_ports(services["jaeger"]))
    assert any("9200" in p for p in published_ports(services["opensearch"]))
    assert any("8016" in p for p in published_ports(services["flagd"]))


def test_compose_overlay_adds_alertmanager_with_a_pinned_image(compose_overlay):
    image = compose_overlay["services"]["alertmanager"]["image"]
    assert image.startswith("quay.io/prometheus/alertmanager:v")
    assert not image.endswith(":latest")


def test_compose_overlay_disables_browser_load_users(compose_overlay):
    environment = compose_overlay["services"]["load-generator"]["environment"]
    assert "LOCUST_BROWSER_TRAFFIC_ENABLED=false" in environment


@pytest.fixture(scope="module")
def alert_rules() -> dict:
    return load_yaml(OPS / "alert_rules.yml")


def test_alert_rules_define_the_alert_scenarios_expect(alert_rules):
    names = {rule["alert"] for group in alert_rules["groups"] for rule in group["rules"]}
    assert "checkout_error_rate_high" in names


def test_every_alert_rule_has_an_expression_a_duration_and_a_summary(alert_rules):
    for group in alert_rules["groups"]:
        for rule in group["rules"]:
            assert rule["expr"].strip(), rule["alert"]
            assert rule["for"], rule["alert"]
            assert rule["annotations"]["summary"], rule["alert"]
            assert rule["labels"]["severity"] in {"critical", "warning"}, rule["alert"]


def test_no_alert_rule_names_a_feature_flag(alert_rules):
    """An alert that named the injected flag would hand the agent the answer."""
    flag_file = REPO_ROOT / "vendor" / "otel-demo" / "src" / "flagd" / "demo.flagd.json"
    flags = json.loads(flag_file.read_text(encoding="utf-8"))["flags"]
    text = (OPS / "alert_rules.yml").read_text(encoding="utf-8")
    named = [flag for flag in flags if flag in text]
    assert named == [], f"alert rules name injected flags: {named}"


def test_alertmanager_sends_resolved_notifications():
    config = load_yaml(OPS / "alertmanager.yml")
    webhook = config["receivers"][0]["webhook_configs"][0]
    assert webhook["send_resolved"] is True
    assert config["route"]["receiver"] == config["receivers"][0]["name"]
