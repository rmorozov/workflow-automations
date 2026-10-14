"""The merge planner batches clean pull requests and turns conflicting ones into trains."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("merge_plan", ROOT / "scripts" / "merge_plan.py")
merge_plan = importlib.util.module_from_spec(_spec)
sys.modules["merge_plan"] = merge_plan
_spec.loader.exec_module(merge_plan)

LINES = "".join(f"line {n}\n" for n in range(1, 11))


def git(repo: Path, *args: str) -> str:
    return merge_plan.git(*args, repo=str(repo)).stdout.strip()


def commit(repo: Path, branch: str, files: dict[str, str], start: str = "main") -> None:
    git(repo, "checkout", "-q", "-B", branch, start)
    for name, text in files.items():
        (repo / name).write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", branch)


def edit(line: int, text: str) -> str:
    lines = LINES.splitlines(keepends=True)
    lines[line - 1] = f"{text}\n"
    return "".join(lines)


@pytest.fixture
def repo(tmp_path):
    """main with a.txt, b.txt and c.txt of ten lines each."""
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "commit.gpgsign", "false")
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text(LINES)
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "base")
    return tmp_path


def run_plan(repo: Path, specs: list[str], after: dict | None = None):
    pulls = merge_plan.load_pulls("main", specs, str(repo))
    return pulls, *merge_plan.plan("main", pulls, after or {}, str(repo))


def test_disjoint_pulls_form_one_batch_whose_tree_is_main_after_merging(repo):
    commit(repo, "p1", {"a.txt": edit(1, "p1")})
    commit(repo, "p2", {"a.txt": edit(9, "p2")})  # same file, other lines: clean overlap
    commit(repo, "p3", {"b.txt": edit(1, "p3")})
    pulls, batches, blocked = run_plan(repo, ["1=p1", "2=p2", "3=p3"])
    assert [b.pulls for b in batches] == [["1", "2", "3"]] and not blocked
    git(repo, "checkout", "-q", "main")
    for branch in ("p1", "p2", "p3"):
        git(repo, "merge", "-q", "--no-edit", branch)
    assert git(repo, "rev-parse", f"{batches[0].commit}^{{tree}}") == git(
        repo, "rev-parse", "main^{tree}"
    )
    assert merge_plan.overlaps(pulls) == [("1", "2", ["a.txt"])]


def test_integration_commit_is_reproducible(repo):
    commit(repo, "p1", {"a.txt": edit(1, "p1")})
    commit(repo, "p2", {"b.txt": edit(1, "p2")})
    first = run_plan(repo, ["1=p1", "2=p2"])[1][0].commit
    assert run_plan(repo, ["1=p1", "2=p2"])[1][0].commit == first


def test_conflicting_pull_waits_for_a_later_batch_and_names_the_cause(repo):
    commit(repo, "p1", {"a.txt": edit(1, "p1")})
    commit(repo, "p2", {"b.txt": edit(1, "p2")})
    commit(repo, "p3", {"a.txt": edit(1, "p3")})
    _, batches, blocked = run_plan(repo, ["1=p1", "2=p2", "3=p3"])
    assert [b.pulls for b in batches] == [["1", "2"], ["3"]] and not blocked
    assert batches[0].deferred == {"3": "conflicts with 1, 2 in a.txt"}
    assert batches[1].commit is None  # its car must be updated from main first


def test_after_orders_clean_pulls_into_successive_batches(repo):
    commit(repo, "p1", {"a.txt": edit(1, "p1")})
    commit(repo, "p2", {"b.txt": edit(1, "p2")})
    commit(repo, "p3", {"c.txt": edit(1, "p3")})
    _, batches, _ = run_plan(repo, ["2=p2", "3=p3", "1=p1"], {"2": {"1"}, "3": {"2"}})
    assert [b.pulls for b in batches] == [["1"], ["2"], ["3"]]
    assert batches[0].deferred == {"2": "after 1", "3": "after 2"}


def test_pull_conflicting_with_the_base_is_blocked(repo):
    commit(repo, "stale", {"c.txt": edit(5, "stale")})
    commit(repo, "main", {"c.txt": edit(5, "main moved")})
    commit(repo, "p1", {"a.txt": edit(1, "p1")})
    _, batches, blocked = run_plan(repo, ["1=p1", "9=stale"])
    assert [b.pulls for b in batches] == [["1"]]
    assert blocked == {"9": "conflicts with the base in c.txt"}


def test_circular_after_is_blocked_not_looped(repo):
    commit(repo, "p1", {"a.txt": edit(1, "p1")})
    commit(repo, "p2", {"b.txt": edit(1, "p2")})
    _, batches, blocked = run_plan(repo, ["1=p1", "2=p2"], {"1": {"2"}, "2": {"1"}})
    assert batches == [] and set(blocked) == {"1", "2"}


def test_cli_reports_json_and_rejects_bad_input(repo, capsys):
    commit(repo, "p1", {"a.txt": edit(1, "p1")})
    assert merge_plan.main(["--repo", str(repo), "--base", "main", "--json", "1=p1"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["batches"][0]["pulls"] == ["1"] and report["blocked"] == {}
    for bad in (["p1"], ["1=p1", "1=p1"], ["--after", "1:7", "1=p1"], ["1=missing"]):
        assert merge_plan.main(["--repo", str(repo), "--base", "main", *bad]) == 1
    assert "merge plan:" in capsys.readouterr().err


def test_planning_leaves_branches_and_worktree_untouched(repo):
    commit(repo, "p1", {"a.txt": edit(1, "p1")})
    commit(repo, "p2", {"a.txt": edit(1, "p2")})
    refs = git(repo, "show-ref")
    run_plan(repo, ["1=p1", "2=p2"])
    assert git(repo, "show-ref") == refs and git(repo, "status", "--porcelain") == ""
