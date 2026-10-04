"""Release approval requires measurements, not the existence of phase reports."""

import json

from check_release_readiness import check_reports


def write(root, config="fb-v1", **overrides):
    payload = {
        "configuration": config,
        "split": "test_id",
        "generated_at": "2026-10-03",
        "commit": "frozen",
        "quotable_as_a_result": True,
        "data_source": "recorded bundles",
        "trials_per_task": 3,
        "recorded_scenarios": 2,
        "total_scenarios": 2,
        "evaluated_scenarios": 2,
        "model_modes": ["api"],
        "model_ids": ["test-model"],
        "model_calls": 6,
        "fallback_trials": 0,
    }
    payload.update(overrides)
    directory = root / config / "test_id"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "run.json").write_text(json.dumps(payload))


def test_missing_reports_block_release(tmp_path):
    assert check_reports(tmp_path, ("fb-v1",), {"test_id": 2}, "frozen")


def test_complete_measured_report_passes(tmp_path):
    write(tmp_path)
    assert check_reports(tmp_path, ("fb-v1",), {"test_id": 2}, "frozen") == []


def test_stub_partial_old_or_short_runs_block_release(tmp_path):
    for changes in (
        {"model_modes": ["stub"]},
        {"evaluated_scenarios": 1},
        {"commit": "old"},
        {"trials_per_task": 1},
        {"fallback_trials": 1},
    ):
        write(tmp_path, **changes)
        assert check_reports(tmp_path, ("fb-v1",), {"test_id": 2}, "frozen")


def test_command_reports_missing_recordings_and_returns_failure(tmp_path, monkeypatch, capsys):
    import sys

    import check_release_readiness as readiness

    monkeypatch.setattr(readiness, "ROOT", tmp_path)
    monkeypatch.setattr(readiness, "split_coverage", lambda _: (0, 2))
    monkeypatch.setattr(sys, "argv", ["release-check", "--commit", "frozen"])
    assert readiness.main() == 1
    assert "0/2 scenarios recorded" in capsys.readouterr().out


def test_unreadable_report_blocks_release(tmp_path):
    write(tmp_path)
    (tmp_path / "fb-v1" / "test_id" / "broken.json").write_text("{")
    assert check_reports(tmp_path, ("fb-v1",), {"test_id": 2}, "frozen")
