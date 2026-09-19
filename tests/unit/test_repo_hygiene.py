"""Tests for the repository hygiene checks (SPEC.md Section 19.4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from check_repo_hygiene import (
    Config,
    HygieneError,
    check_banned_characters,
    check_flagged_words,
    check_readme_stats,
    is_excluded,
    load_config,
    matches_any_glob,
    render_stats_block,
)

CONFIG = load_config()


def write(root: Path, rel_path: str, text: str) -> str:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return rel_path


def test_load_config_has_owner_email_only():
    assert CONFIG.allowed_author_emails == ["esteki.net@gmail.com"]


def test_load_config_rejects_missing_keys(tmp_path: Path):
    path = tmp_path / "partial.yaml"
    path.write_text("allowed_author_emails: []\n", encoding="utf-8")
    with pytest.raises(HygieneError, match="missing keys"):
        load_config(path)


def test_load_config_rejects_missing_file(tmp_path: Path):
    with pytest.raises(HygieneError, match="missing hygiene config"):
        load_config(tmp_path / "absent.yaml")


def test_check_banned_characters_flags_em_dash(tmp_path: Path):
    rel = write(tmp_path, "docs/note.md", "A sentence \u2014 with an em dash.\n")
    findings = check_banned_characters([rel], CONFIG, root=tmp_path)
    assert len(findings) == 1
    assert findings[0].rule == "banned-character"
    assert findings[0].location == "docs/note.md:1"


def test_check_banned_characters_flags_en_dash(tmp_path: Path):
    rel = write(tmp_path, "src/module.py", "RANGE = '1\u20135'\n")
    findings = check_banned_characters([rel], CONFIG, root=tmp_path)
    assert len(findings) == 1
    assert "U+2013" in findings[0].message


def test_check_banned_characters_reports_correct_line_number(tmp_path: Path):
    rel = write(tmp_path, "docs/note.md", "clean\nclean\nbad \u2014 here\n")
    findings = check_banned_characters([rel], CONFIG, root=tmp_path)
    assert findings[0].location.endswith(":3")


def test_check_banned_characters_ignores_vendor(tmp_path: Path):
    rel = write(tmp_path, "vendor/otel-demo/readme.md", "vendored \u2014 text\n")
    assert check_banned_characters([rel], CONFIG, root=tmp_path) == []


def test_check_banned_characters_ignores_bundles(tmp_path: Path):
    rel = write(tmp_path, "bundles/scenario/run/topology.json", '{"a": "\u2014"}\n')
    assert check_banned_characters([rel], CONFIG, root=tmp_path) == []


def test_check_banned_characters_ignores_non_text_extensions(tmp_path: Path):
    rel = write(tmp_path, "docs/diagram.svg", "<text>\u2014</text>\n")
    assert check_banned_characters([rel], CONFIG, root=tmp_path) == []


def test_check_banned_characters_accepts_clean_file(tmp_path: Path):
    rel = write(tmp_path, "docs/note.md", "Ranges use 1 to 5, not a dash.\n")
    assert check_banned_characters([rel], CONFIG, root=tmp_path) == []


def test_check_flagged_words_flags_marketing_word(tmp_path: Path):
    rel = write(tmp_path, "README.md", "This is a seamless experience.\n")
    findings = check_flagged_words([rel], CONFIG, root=tmp_path)
    assert [finding.rule for finding in findings] == ["flagged-word"]


def test_check_flagged_words_is_case_insensitive(tmp_path: Path):
    rel = write(tmp_path, "README.md", "We Leverage the graph.\n")
    assert len(check_flagged_words([rel], CONFIG, root=tmp_path)) == 1


def test_check_flagged_words_matches_whole_words_only(tmp_path: Path):
    rel = write(tmp_path, "README.md", "The realmost value is fine.\n")
    assert check_flagged_words([rel], CONFIG, root=tmp_path) == []


def test_check_flagged_words_ignores_source_files(tmp_path: Path):
    rel = write(tmp_path, "src/firebreak/thing.py", "# we leverage this\n")
    assert check_flagged_words([rel], CONFIG, root=tmp_path) == []


def test_matches_any_glob_handles_recursive_docs_pattern():
    assert matches_any_glob("docs/adr/0001-framework.md", CONFIG.prose_globs)


def test_matches_any_glob_rejects_unrelated_path():
    assert not matches_any_glob("src/firebreak/agent/graph.py", CONFIG.prose_globs)


def test_is_excluded_matches_prefix():
    assert is_excluded("labels/scenario/run.json", CONFIG.excluded_paths)


def test_is_excluded_allows_source():
    assert not is_excluded("src/firebreak/agent/graph.py", CONFIG.excluded_paths)


def test_render_stats_block_builds_table():
    stats = {
        "readme_rows": [
            {"metric": "Top-1 accuracy", "value": "TBD", "source": "reports/eval/fb"},
        ]
    }
    rendered = render_stats_block(stats)
    assert rendered.splitlines()[0] == "| Metric | Value | Source |"
    assert "| Top-1 accuracy | TBD | reports/eval/fb |" in rendered


def test_render_stats_block_rejects_missing_rows():
    with pytest.raises(HygieneError, match="readme_rows"):
        render_stats_block({})


def test_render_stats_block_rejects_incomplete_row():
    with pytest.raises(HygieneError, match="missing"):
        render_stats_block({"readme_rows": [{"metric": "x"}]})


def test_check_readme_stats_skips_when_no_report(tmp_path: Path):
    readme = tmp_path / "README.md"
    readme.write_text("# Firebreak\n", encoding="utf-8")
    assert check_readme_stats(readme, tmp_path / "absent.json") == []


def test_check_readme_stats_flags_stale_block(tmp_path: Path):
    stats_path = tmp_path / "site_stats.json"
    stats_path.write_text(
        json.dumps({"readme_rows": [{"metric": "m", "value": "1", "source": "s"}]}),
        encoding="utf-8",
    )
    readme = tmp_path / "README.md"
    readme.write_text("<!-- stats:start -->\nstale content\n<!-- stats:end -->\n", encoding="utf-8")
    findings = check_readme_stats(readme, stats_path)
    assert findings and findings[0].rule == "readme-stats"


def test_check_readme_stats_accepts_matching_block(tmp_path: Path):
    rows = [{"metric": "m", "value": "1", "source": "s"}]
    stats_path = tmp_path / "site_stats.json"
    stats_path.write_text(json.dumps({"readme_rows": rows}), encoding="utf-8")
    block = render_stats_block({"readme_rows": rows})
    readme = tmp_path / "README.md"
    readme.write_text(f"<!-- stats:start -->\n{block}\n<!-- stats:end -->\n", encoding="utf-8")
    assert check_readme_stats(readme, stats_path) == []


def test_check_readme_stats_flags_missing_markers(tmp_path: Path):
    stats_path = tmp_path / "site_stats.json"
    stats_path.write_text(
        json.dumps({"readme_rows": [{"metric": "m", "value": "1", "source": "s"}]}),
        encoding="utf-8",
    )
    readme = tmp_path / "README.md"
    readme.write_text("# Firebreak\n", encoding="utf-8")
    findings = check_readme_stats(readme, stats_path)
    assert findings and "markers" in findings[0].message


def test_config_is_a_dataclass_instance():
    assert isinstance(CONFIG, Config)
