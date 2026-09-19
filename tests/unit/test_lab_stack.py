"""Tests for firebreak.lab.stack."""

import json
from pathlib import Path

import pytest
import yaml

from firebreak.lab.stack import (
    ALERTMANAGER_TARGET,
    GENERATED_HEADER,
    RULE_FILE_IN_CONTAINER,
    StackConfigError,
    build_prometheus_config,
    render_flag_store,
    render_prometheus_config,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
VENDORED_PROMETHEUS_CONFIG = (
    REPO_ROOT / "vendor" / "otel-demo" / "src" / "prometheus" / "prometheus-config.yaml"
)


def test_build_prometheus_config_adds_rule_files_and_alerting_and_keeps_input_keys():
    vendored = {
        "global": {"scrape_interval": "60s"},
        "otlp": {"keep_identifying_resource_attributes": True},
        "storage": {"tsdb": {"out_of_order_time_window": "30m"}},
    }

    rendered = build_prometheus_config(vendored)

    assert rendered["rule_files"] == [RULE_FILE_IN_CONTAINER]
    assert rendered["alerting"] == {
        "alertmanagers": [{"static_configs": [{"targets": [ALERTMANAGER_TARGET]}]}]
    }
    assert rendered["global"] == vendored["global"]
    assert rendered["otlp"] == vendored["otlp"]
    assert rendered["storage"] == vendored["storage"]


def test_build_prometheus_config_raises_when_global_section_missing():
    with pytest.raises(StackConfigError, match="global"):
        build_prometheus_config({"otlp": {}})


def test_build_prometheus_config_raises_when_input_is_not_a_dict():
    with pytest.raises(StackConfigError, match="global"):
        build_prometheus_config(["not", "a", "dict"])


def test_render_prometheus_config_raises_when_source_file_does_not_exist(tmp_path):
    source = tmp_path / "missing.yaml"
    destination = tmp_path / "out.yaml"

    with pytest.raises(StackConfigError, match="not found"):
        render_prometheus_config(source, destination)


def test_render_prometheus_config_creates_missing_parent_directories(tmp_path):
    destination = tmp_path / "a" / "b" / "c" / "prometheus.yml"
    assert not destination.parent.exists()

    result = render_prometheus_config(VENDORED_PROMETHEUS_CONFIG, destination)

    assert result == destination
    assert destination.parent.is_dir()
    assert destination.is_file()


def test_render_prometheus_config_on_real_vendored_file_has_expected_keys_and_header(tmp_path):
    destination = tmp_path / "generated" / "prometheus.yml"

    render_prometheus_config(VENDORED_PROMETHEUS_CONFIG, destination)

    text = destination.read_text(encoding="utf-8")
    assert text.startswith(GENERATED_HEADER)
    parsed = yaml.safe_load(text)
    assert parsed["rule_files"] == [RULE_FILE_IN_CONTAINER]
    assert "alerting" in parsed
    assert "otlp" in parsed
    assert "storage" in parsed


def test_render_flag_store_copies_the_pinned_file(tmp_path: Path):
    source = REPO_ROOT / "vendor" / "otel-demo" / "src" / "flagd" / "demo.flagd.json"
    destination = render_flag_store(source, tmp_path / "flagd")

    assert destination.name == "demo.flagd.json"
    copied = json.loads(destination.read_text(encoding="utf-8"))
    assert copied["flags"]["paymentFailure"]["defaultVariant"] == "off"


def test_render_flag_store_creates_the_directory(tmp_path: Path):
    source = REPO_ROOT / "vendor" / "otel-demo" / "src" / "flagd" / "demo.flagd.json"
    destination = render_flag_store(source, tmp_path / "deep" / "flagd")

    assert destination.is_file()


def test_render_flag_store_overwrites_a_modified_store(tmp_path: Path):
    source = REPO_ROOT / "vendor" / "otel-demo" / "src" / "flagd" / "demo.flagd.json"
    target_dir = tmp_path / "flagd"
    first = render_flag_store(source, target_dir)
    first.write_text('{"flags": {}}', encoding="utf-8")

    render_flag_store(source, target_dir)

    assert "paymentFailure" in json.loads(first.read_text(encoding="utf-8"))["flags"]


def test_render_flag_store_keeps_an_existing_store_when_asked(tmp_path: Path):
    """A stack restart must not silently clear a fault that is still applied."""
    source = REPO_ROOT / "vendor" / "otel-demo" / "src" / "flagd" / "demo.flagd.json"
    target_dir = tmp_path / "flagd"
    existing = render_flag_store(source, target_dir)
    existing.write_text('{"flags": {"kept": {}}}', encoding="utf-8")

    render_flag_store(source, target_dir, overwrite=False)

    assert json.loads(existing.read_text(encoding="utf-8"))["flags"] == {"kept": {}}


def test_render_flag_store_rejects_a_missing_source(tmp_path: Path):
    with pytest.raises(StackConfigError, match="vendored flag file not found"):
        render_flag_store(tmp_path / "absent.json", tmp_path / "flagd")
