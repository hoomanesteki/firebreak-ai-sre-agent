"""The offline demo runs, reproduces, and says what it is showing.

SPEC.md Section 17 Phase 11 requires `make demo-offline` green, and CLAUDE.md requires it
from this phase. What is tested here is the three properties that make it worth having:
it runs with no model, it detects a replay that does not reproduce, and it does not let a
stub-recorded demo look like a model-recorded one.

The last is the one with consequences. A demo is the artefact somebody judges the project
by, and a demo replaying a deterministic stub while looking like a model investigation
would be the most misleading thing in the repository.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from firebreak.demo.showcase import (
    SHOWCASE_SCENARIOS,
    ShowcaseError,
    build_showcase,
    showcase,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CASSETTES = REPO_ROOT / "recordings" / "cassettes"
CONSOLE_DIR = REPO_ROOT / "reports" / "console"


def _console_digest() -> str:
    """One hash over every committed showcase report, for comparing across demo runs."""
    digest = hashlib.sha256()
    for path in sorted(CONSOLE_DIR.glob("*.json")):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def run_demo(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "demo_offline.py"), *arguments],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env={"PYTHONPATH": str(REPO_ROOT / "src"), "PATH": "/usr/bin:/bin"},
        timeout=600,
    )


class TestTheShowcaseIsTenIncidentsAndSaysWhyEach:
    def test_there_are_ten(self) -> None:
        assert len(SHOWCASE_SCENARIOS) == 10

    def test_every_one_has_a_stated_reason(self) -> None:
        """A showcase whose selection nobody can justify is one somebody will quietly
        replace with easier cases."""
        for name, reason in SHOWCASE_SCENARIOS:
            assert len(reason) > 30, f"{name} has no real reason given"

    def test_it_includes_a_no_fault_incident(self) -> None:
        """Abstaining is the right answer somewhere in the set, or the demo is a brochure
        of wins."""
        assert any("no-fault" in name for name, _ in SHOWCASE_SCENARIOS)

    def test_it_includes_a_distractor_and_a_double_fault(self) -> None:
        names = [name for name, _ in SHOWCASE_SCENARIOS]
        assert any("distractor" in name for name in names)
        assert any("double-fault" in name for name in names)

    def test_every_scenario_is_in_the_library(self) -> None:
        assert len(showcase()) == 10

    def test_a_missing_scenario_is_an_error_not_a_gap(self, tmp_path: Path) -> None:
        """A demo that silently shows nine incidents and still reports success is the
        failure mode this project keeps finding in other people's checks.

        Built from a library holding one real spec rather than an empty directory, because
        `load_library` already refuses an empty one and that would test the wrong guard.
        """
        specs = tmp_path / "specs"
        specs.mkdir()
        kept = SHOWCASE_SCENARIOS[0][0]
        source = REPO_ROOT / "scenarios" / "specs" / f"{kept}.yaml"
        (specs / source.name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
        with pytest.raises(ShowcaseError, match="not in the library"):
            showcase(specs)


class TestItBuildsAnywhere:
    def test_the_bundles_are_rebuilt_from_specs(self, tmp_path: Path) -> None:
        """The recorded library is git-ignored, so a demo keyed to it would run on one
        machine and fail in CI."""
        incidents = build_showcase(tmp_path)
        assert len(incidents) == 10
        for incident in incidents:
            assert (tmp_path / incident.bundle_id / "manifest.json").is_file()

    def test_rebuilding_is_deterministic(self, tmp_path: Path) -> None:
        """The cassette keys hash the windows and evidence ids that come out of the
        bundle, so a bundle that varied would invalidate every cassette silently."""
        from firebreak.lab.bundle import sha256_of

        first = tmp_path / "a"
        second = tmp_path / "b"
        build_showcase(first)
        build_showcase(second)
        for incident in showcase():
            for name in ("metrics.parquet", "traces.parquet", "logs.parquet"):
                assert sha256_of(first / incident.bundle_id / name) == sha256_of(
                    second / incident.bundle_id / name
                ), f"{incident.spec.id} {name} is not reproducible"


class TestTheDemoRuns:
    def test_it_replays_every_showcase_incident(self) -> None:
        result = run_demo()
        assert result.returncode == 0, result.stderr
        assert "10 incident(s) replayed and reproduced their recordings" in result.stdout

    def test_it_needs_no_model_and_no_network(self) -> None:
        """Replay mode serves recorded answers, so the graph makes no model call. The
        assertion is on the exit code with no credentials configured, which is the state
        this repository is in."""
        result = run_demo()
        assert result.returncode == 0

    def test_it_says_which_mode_the_cassettes_came_from(self) -> None:
        """The property with consequences: a demo replaying stub answers must not look
        like a demo replaying a model."""
        result = run_demo()
        assert "cassettes recorded from mode: stub" in result.stdout
        assert "not a model" in result.stdout
        assert "Nothing below is evidence about model quality" in result.stdout

    def test_it_says_why_each_incident_is_in_the_showcase(self) -> None:
        result = run_demo()
        assert "why in the showcase:" in result.stdout

    def test_it_shows_abstentions_as_well_as_answers(self) -> None:
        result = run_demo()
        assert "abstained" in result.stdout

    def test_it_leaves_the_committed_reports_byte_identical(self) -> None:
        """Running the demo must not dirty the tree it ships in.

        The showcase reports are committed, because they are the only thing that makes the
        Console non-empty on a fresh clone. They are also rewritten by every demo run, and
        every `make verify`. When the payload stored a replay's wall clock, that made ten
        tracked files differ after every run, over the one field nobody could use: the
        elapsed time of reading cassettes off this machine's disk.
        """
        before = _console_digest()
        assert run_demo().returncode == 0
        assert _console_digest() == before, (
            "the demo rewrote its own committed reports; some field is not deterministic"
        )

    def test_a_replayed_report_stores_no_wall_clock(self) -> None:
        """Same reason the Evaluation page prints "not measured" rather than a cost of
        zero. A number that describes the replay would read as one describing the system.
        """
        for path in sorted(CONSOLE_DIR.glob("*.json")):
            stored = json.loads(path.read_text(encoding="utf-8"))
            if not stored.get("replayed"):
                continue
            assert stored["wall_clock_seconds"] is None, f"{path.name} timed its replay"


class TestItFailsWhenAReplayDoesNotReproduce:
    def test_a_changed_expectation_is_a_failure(self, tmp_path: Path) -> None:
        """A replay that does not reproduce its recording means something in the harness is
        nondeterministic, and every measurement in every report is then suspect. That is
        worth failing a build over rather than warning about.
        """
        copied = tmp_path / "cassettes"
        copied.mkdir()
        manifest = json.loads((CASSETTES / "manifest.json").read_text())
        # Corrupt one expectation, leaving the cassettes themselves alone.
        first = sorted(manifest["incidents"])[0]
        manifest["incidents"][first]["root_cause_service"] = "a-service-that-was-not-named"
        (copied / "manifest.json").write_text(json.dumps(manifest))
        for name in manifest["incidents"]:
            source = CASSETTES / name
            if source.is_dir():
                target = copied / name
                target.mkdir()
                for cassette in source.glob("*.json"):
                    (target / cassette.name).write_text(cassette.read_text())

        result = run_demo("--cassettes", str(copied))
        assert result.returncode == 1
        assert "DIFFERS" in result.stdout
        assert "nondeterministic" in result.stderr

    def test_a_missing_manifest_says_what_to_run(self, tmp_path: Path) -> None:
        result = run_demo("--cassettes", str(tmp_path / "nothing"))
        assert result.returncode != 0
        assert "make cassettes" in result.stderr or "make cassettes" in result.stdout

    def test_a_missing_cassette_says_to_re_record(self, tmp_path: Path) -> None:
        """The interesting failure: the graph asked a question the recording does not
        contain, which is what happens when the loop changes."""
        copied = tmp_path / "cassettes"
        copied.mkdir()
        manifest = json.loads((CASSETTES / "manifest.json").read_text())
        # Keep the manifest and none of the cassettes, which is exactly the shape of a
        # loop that started asking something new.
        answered = [n for n, e in manifest["incidents"].items() if e["cassettes"]]
        manifest["incidents"] = {answered[0]: manifest["incidents"][answered[0]]}
        (copied / "manifest.json").write_text(json.dumps(manifest))

        result = run_demo("--cassettes", str(copied))
        assert result.returncode == 1
        # The floor engaged, which is what a missing cassette actually looks like from
        # outside: the graph's model failure handling catches it first.
        assert "the deterministic floor engaged" in result.stderr
        assert "Re-record with `make cassettes`" in result.stderr


class TestTheCassettesAreCommitted:
    def test_the_manifest_exists_in_the_repository(self) -> None:
        """CI has no recorded bundles and no credentials, so the cassettes are the only
        way its offline demo step checks anything."""
        assert (CASSETTES / "manifest.json").is_file()

    def test_the_manifest_records_the_seed_and_run_id(self) -> None:
        """Without them a future reader cannot tell which bundles the cassettes were
        recorded against, and a changed seed would look like a changed loop."""
        manifest = json.loads((CASSETTES / "manifest.json").read_text())
        assert manifest["run_id"] == "showcase"
        assert manifest["seed"] == 11

    def test_every_incident_in_the_manifest_is_in_the_showcase(self) -> None:
        manifest = json.loads((CASSETTES / "manifest.json").read_text())
        expected = {incident.bundle_id for incident in showcase()}
        assert set(manifest["incidents"]) == expected
