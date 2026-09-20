"""Tests for the repository hygiene checks (SPEC.md Section 19.4)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import check_repo_hygiene
from check_repo_hygiene import (
    Config,
    HygieneError,
    check_banned_characters,
    check_commit_authors,
    check_commit_messages,
    check_flagged_words,
    check_readme_stats,
    collect_findings,
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


def test_load_config_allows_only_the_owner_addresses():
    assert CONFIG.allowed_author_emails == [
        "esteki.net@gmail.com",
        "67445158+hoomanesteki@users.noreply.github.com",
    ]


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


# --- commit checks -------------------------------------------------------
#
# These gate authorship and AI attribution on every commit, so they are
# tested against a fake git log rather than left to run untested in CI.


def fake_log(monkeypatch, output: str) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run_git(args, cwd=None):
        calls.append(args)
        return output

    monkeypatch.setattr(check_repo_hygiene, "run_git", fake_run_git)
    return calls


def test_check_commit_authors_accepts_the_owner(monkeypatch):
    fake_log(monkeypatch, "abc123 esteki.net@gmail.com esteki.net@gmail.com\n")

    assert check_commit_authors(CONFIG, "main") == []


def test_check_commit_authors_rejects_an_unknown_author(monkeypatch):
    fake_log(monkeypatch, "abc123 someone@example.com esteki.net@gmail.com\n")

    findings = check_commit_authors(CONFIG, "main")

    assert [finding.rule for finding in findings] == ["commit-author"]
    assert "author someone@example.com" in findings[0].message


def test_check_commit_authors_rejects_an_unknown_committer(monkeypatch):
    fake_log(monkeypatch, "abc123 esteki.net@gmail.com bot@example.com\n")

    findings = check_commit_authors(CONFIG, "main")

    assert "committer bot@example.com" in findings[0].message


def test_check_commit_authors_is_case_insensitive(monkeypatch):
    fake_log(monkeypatch, "abc123 Esteki.Net@Gmail.com ESTEKI.NET@GMAIL.COM\n")

    assert check_commit_authors(CONFIG, "main") == []


def test_check_commit_authors_reports_every_bad_commit(monkeypatch):
    fake_log(
        monkeypatch,
        "aaa111 a@example.com a@example.com\nbbb222 esteki.net@gmail.com esteki.net@gmail.com\n",
    )

    findings = check_commit_authors(CONFIG, "main")

    assert len(findings) == 2
    assert all("aaa111" in finding.location for finding in findings)


def test_check_commit_authors_ignores_malformed_lines(monkeypatch):
    fake_log(monkeypatch, "not-a-real-line\n")

    assert check_commit_authors(CONFIG, "main") == []


def test_check_commit_authors_returns_nothing_when_git_gives_no_output(monkeypatch):
    fake_log(monkeypatch, "")

    assert check_commit_authors(CONFIG, "main") == []


def test_check_commit_messages_rejects_an_ai_coauthor_trailer(monkeypatch):
    log = "abc123\x00feat(agent): add node\n\nCo-Authored-By: Claude <x@y.z>\n\x00\n"
    fake_log(monkeypatch, log)

    findings = check_commit_messages(CONFIG, "main")

    assert [finding.rule for finding in findings] == ["ai-attribution"]


def test_check_commit_messages_rejects_the_anthropic_noreply_address(monkeypatch):
    log = "abc123\x00chore: thing\n\nnoreply@anthropic.com\n\x00\n"
    fake_log(monkeypatch, log)

    assert check_commit_messages(CONFIG, "main")


def test_check_commit_messages_rejects_a_generated_with_line(monkeypatch):
    log = "abc123\x00docs: readme\n\nGenerated with a tool\n\x00\n"
    fake_log(monkeypatch, log)

    assert check_commit_messages(CONFIG, "main")


def test_check_commit_messages_accepts_a_clean_message(monkeypatch):
    log = "abc123\x00feat(gates): re-run cited queries\n\nWhy it matters.\n\x00\n"
    fake_log(monkeypatch, log)

    assert check_commit_messages(CONFIG, "main") == []


def test_check_commit_messages_returns_nothing_when_git_gives_no_output(monkeypatch):
    fake_log(monkeypatch, "")

    assert check_commit_messages(CONFIG, "main") == []


def test_collect_findings_skips_git_checks_when_asked(monkeypatch):
    calls = fake_log(monkeypatch, "abc123 someone@example.com someone@example.com\n")

    findings = collect_findings([], CONFIG, "main", skip_git=True)

    assert findings == []
    assert calls == []


# --- entry point ---------------------------------------------------------
#
# main() is what CI runs. An exit code that is always zero would make the
# whole check decorative, so the codes are asserted directly.


def test_main_returns_zero_on_a_clean_file(tmp_path, capsys):
    rel = write(tmp_path, "docs/clean.md", "Ranges use 1 to 5.\n")

    assert check_repo_hygiene.main([rel, "--skip-git", "--root", str(tmp_path)]) == 0
    assert "clean" in capsys.readouterr().out


def test_main_returns_one_when_a_rule_fails(tmp_path, capsys):
    rel = write(tmp_path, "docs/bad.md", "A sentence \u2014 with a dash.\n")

    assert check_repo_hygiene.main([rel, "--skip-git", "--root", str(tmp_path)]) == 1
    assert "banned-character" in capsys.readouterr().err


def test_main_returns_two_when_the_config_cannot_be_loaded(monkeypatch, capsys):
    def raise_missing(path=None):
        raise HygieneError("missing hygiene config: nowhere.yaml")

    monkeypatch.setattr(check_repo_hygiene, "load_config", raise_missing)

    assert check_repo_hygiene.main(["--skip-git"]) == 2
    assert "hygiene config error" in capsys.readouterr().err


def test_main_falls_back_to_tracked_files_when_none_are_given(monkeypatch):
    monkeypatch.setattr(check_repo_hygiene, "list_tracked_files", lambda cwd=None: [])
    captured = {}

    def fake_collect(files, config, base_ref, skip_git, root=None):
        captured["files"] = files
        return []

    monkeypatch.setattr(check_repo_hygiene, "collect_findings", fake_collect)

    assert check_repo_hygiene.main(["--skip-git"]) == 0
    assert captured["files"] == []


def test_run_git_returns_empty_string_on_failure(tmp_path):
    assert check_repo_hygiene.run_git(["rev-parse", "--not-a-flag"], cwd=tmp_path) == ""


def test_list_tracked_files_returns_paths_in_this_repository():
    tracked = check_repo_hygiene.list_tracked_files()

    assert "SPEC.md" in tracked
    assert all(not path.startswith("/") for path in tracked)


def test_check_banned_characters_skips_a_file_that_is_not_utf8(tmp_path):
    path = tmp_path / "docs" / "binary.md"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe not valid utf-8")

    assert check_banned_characters(["docs/binary.md"], CONFIG, root=tmp_path) == []


def test_check_banned_characters_skips_a_path_that_is_not_a_file(tmp_path):
    (tmp_path / "docs").mkdir()

    assert check_banned_characters(["docs"], CONFIG, root=tmp_path) == []


def test_matches_any_glob_handles_a_bare_recursive_prefix():
    """site/templates/** has no extension part, so fnmatch alone misses it."""
    assert matches_any_glob("site/templates/base.html", CONFIG.prose_globs)
    assert matches_any_glob("site/templates/nested/page.html", CONFIG.prose_globs)


def test_load_config_rejects_a_config_that_is_not_a_mapping(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text("- a\n- b\n", encoding="utf-8")

    with pytest.raises(HygieneError, match="must be a mapping"):
        load_config(path)


def test_render_stats_block_rejects_a_row_that_is_not_a_mapping():
    with pytest.raises(HygieneError, match="each readme_rows entry must be a mapping"):
        render_stats_block({"readme_rows": ["not-a-mapping"]})


def test_collect_findings_runs_the_git_checks_when_not_skipped(monkeypatch):
    fake_log(monkeypatch, "abc123 stranger@example.com stranger@example.com\n")

    findings = collect_findings([], CONFIG, "main", skip_git=False)

    assert any(finding.rule == "commit-author" for finding in findings)


def test_check_flagged_words_skips_a_file_that_is_not_utf8(tmp_path):
    path = tmp_path / "README.md"
    path.write_bytes(b"\xff\xfe seamless")

    assert check_flagged_words(["README.md"], CONFIG, root=tmp_path) == []


def test_check_flagged_words_skips_a_path_that_is_not_a_file(tmp_path):
    (tmp_path / "docs").mkdir()

    assert check_flagged_words(["docs"], CONFIG, root=tmp_path) == []


def test_check_banned_characters_skips_a_tracked_file_that_is_gone(tmp_path):
    """git ls-files can name a file that a rebase or checkout removed."""
    assert check_banned_characters(["docs/deleted.md"], CONFIG, root=tmp_path) == []


def test_check_flagged_words_skips_a_prose_file_that_is_gone(tmp_path):
    assert check_flagged_words(["README.md"], CONFIG, root=tmp_path) == []
