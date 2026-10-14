"""Static configuration coverage; no model calls or claimed runtime activation."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def run(*args, root=ROOT):
    return subprocess.run(
        [sys.executable, str(root / "scripts/agent_profiles.py"), *args],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )


def test_policy_and_backlog_are_valid():
    result = run("check")
    assert result.returncode == 0, result.stderr
    assert "static only" in result.stdout


def test_routine_and_complex_routes_and_fixed_coordinator(tmp_path):
    root = copy_setup(tmp_path)
    for id_, tier in [("ZZ-001", "routine"), ("ZZ-002", "complex")]:
        (root / "docs/backlog" / f"{id_}.md").write_text(
            f"---\nid: {id_}\ntitle: Synthetic routing fixture\ntool: demo\n"
            f"priority: P2\ndepends_on: []\ntouches: [demo.py]\ntier: {tier}\n---\n"
        )
    routine = json.loads(run("route", "--task", "ZZ-001", root=root).stdout)
    assert routine["model"] == "gpt-6.1-sol"
    assert routine["effort"] == "medium"
    complex_ = json.loads(run("route", "--task", "ZZ-002", "--role", "reviewer", root=root).stdout)
    assert complex_["model"] == "gpt-6-astra"
    assert complex_["effort"] == "high"
    assert complex_["runtime_verified"] is False
    owner = json.loads(run("route", "--task", "ZZ-002", "--role", "coordinator", root=root).stdout)
    assert owner["model"] == "gpt-6.1-sol"


def test_unknown_task_and_missing_task_fail():
    assert run("route", "--task", "ZZ-999").returncode == 1
    assert run("route").returncode == 2


def test_all_task_files_use_neutral_tiers():
    for path in (ROOT / "docs/backlog").glob("*.md"):
        if path.name == "README.md":
            continue
        front = path.read_text().split("---")[1]
        assert "tier:" in front
        assert "model:" not in front
        assert "effort:" not in front


def copy_setup(tmp_path):
    for name in (".codex", "scripts", "docs"):
        shutil.copytree(ROOT / name, tmp_path / name)
    return tmp_path


def test_unknown_route_profile_is_rejected(tmp_path):
    root = copy_setup(tmp_path)
    path = root / ".codex/routing.toml"
    path.write_text(path.read_text().replace('reviewer = "reviewer"', 'reviewer = "missing"'))
    result = run("check", root=root)
    assert result.returncode == 1
    assert "unavailable profile" in result.stderr


def test_unsupported_permission_override_is_rejected(tmp_path):
    root = copy_setup(tmp_path)
    path = root / ".codex/agents/reviewer.toml"
    path.write_text('sandbox_mode = "read-only"\n' + path.read_text())
    result = run("check", root=root)
    assert result.returncode == 1
    assert "unsupported profile fields" in result.stderr


def test_integer_is_not_accepted_as_disabled_boolean(tmp_path):
    root = copy_setup(tmp_path)
    path = root / ".codex/agents/reviewer.toml"
    path.write_text(path.read_text().replace("apps = false", "apps = 0"))
    result = run("check", root=root)
    assert result.returncode == 1
    assert "capabilities must be disabled" in result.stderr


def test_root_cap_must_be_integer(tmp_path):
    root = copy_setup(tmp_path)
    path = root / ".codex/config.toml"
    path.write_text(
        path.read_text().replace("threads_per_session = 3", "threads_per_session = 3.0")
    )
    result = run("check", root=root)
    assert result.returncode == 1
    assert "root defaults/thread cap" in result.stderr
