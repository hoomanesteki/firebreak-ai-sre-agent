"""Tests for firebreak.lab.webhook."""

import json
import socket
import threading

import httpx

from firebreak.lab.webhook import AlertSink, build_server, summarise


def test_alert_sink_record_appends_one_json_line_per_call(tmp_path):
    path = tmp_path / "alerts.jsonl"
    sink = AlertSink(path)

    sink.record({"a": 1})
    sink.record({"b": 2})

    assert sink.count == 2
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    second = json.loads(lines[1])
    assert "received_at" in first
    assert "received_at" in second
    assert first["payload"] == {"a": 1}
    assert second["payload"] == {"b": 2}


def test_alert_sink_calls_listener_with_a_message_naming_the_count(tmp_path):
    messages = []
    sink = AlertSink(tmp_path / "alerts.jsonl", listener=messages.append)

    sink.record({"alerts": [{"status": "firing", "labels": {"alertname": "Foo"}}]})
    sink.record({"alerts": [{"status": "resolved", "labels": {"alertname": "Bar"}}]})

    assert "[1]" in messages[0]
    assert "[2]" in messages[1]


def test_summarise_handles_empty_payload():
    assert summarise({}) == "delivery with no alerts"


def test_summarise_handles_payload_with_no_alerts_list():
    assert summarise({"alerts": "not-a-list"}) == "delivery with no alerts"
    assert summarise({"alerts": []}) == "delivery with no alerts"


def test_summarise_names_both_alertnames_and_statuses_for_two_alerts():
    payload = {
        "alerts": [
            {"status": "firing", "labels": {"alertname": "HighErrorRate"}},
            {"status": "resolved", "labels": {"alertname": "HighLatency"}},
        ]
    }

    result = summarise(payload)

    assert result == "2 alert(s): HighErrorRate[firing], HighLatency[resolved]"


def test_build_server_binds_to_localhost(tmp_path):
    sink = AlertSink(tmp_path / "alerts.jsonl")
    server = build_server(0, sink)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def _read_raw_response(port, request_bytes):
    with socket.create_connection(("127.0.0.1", port), timeout=5.0) as sock:
        sock.sendall(request_bytes)
        sock.settimeout(5.0)
        chunks = []
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        except TimeoutError:
            pass
        return b"".join(chunks)


def test_webhook_server_end_to_end_records_serves_health_and_rejects_bad_requests(tmp_path):
    path = tmp_path / "alerts.jsonl"
    sink = AlertSink(path)
    server = build_server(0, sink)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{port}"
        payload = {
            "receiver": "firebreak",
            "status": "firing",
            "alerts": [
                {
                    "status": "firing",
                    "labels": {"alertname": "HighErrorRate", "severity": "critical"},
                }
            ],
        }

        response = httpx.post(f"{base_url}/", json=payload, timeout=5.0)
        assert response.status_code == 200
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1

        bad_json_response = httpx.post(
            f"{base_url}/",
            content=b"not-json",
            headers={"Content-Type": "text/plain"},
            timeout=5.0,
        )
        assert bad_json_response.status_code == 400
        assert bad_json_response.json()["error"] == "body is not JSON"

        non_object_response = httpx.post(f"{base_url}/", json=["not", "an", "object"], timeout=5.0)
        assert non_object_response.status_code == 400
        assert non_object_response.json()["error"] == "body is not a JSON object"

        raw = _read_raw_response(
            port, b"POST / HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
        )
        head, _, body = raw.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n")[0]
        assert b"400" in status_line
        assert json.loads(body)["error"] == "missing or oversized body"

        health_response = httpx.get(f"{base_url}/", timeout=5.0)
        assert health_response.status_code == 200
        assert health_response.json()["received"] == 1
    finally:
        server.shutdown()
        thread.join(timeout=5.0)
        server.server_close()


def test_alert_sink_records_concurrent_deliveries_without_interleaving(tmp_path):
    """The server threads each delivery and Alertmanager can send several at once.

    Without a lock the appends can interleave and produce a line that is not
    parseable JSON, which would silently corrupt a recording's alert log.
    """
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "alerts.jsonl"
    sink = AlertSink(path)
    payloads = [
        {"alerts": [{"labels": {"alertname": f"alert{i}"}, "status": "firing"}]} for i in range(60)
    ]

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(sink.record, payloads))

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 60
    assert sink.count == 60
    names = {json.loads(line)["payload"]["alerts"][0]["labels"]["alertname"] for line in lines}
    assert names == {f"alert{i}" for i in range(60)}
