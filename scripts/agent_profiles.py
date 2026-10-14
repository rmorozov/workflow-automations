"""Validate repository Codex policy, or resolve a backlog task to a concrete profile.

Static checks do not prove runtime discovery, permissions or model availability.
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path

from backlog import TIERS, load_tasks

ROOT = Path(__file__).resolve().parents[1]
EFFORTS = {"low", "medium", "high", "xhigh"}
TASK_ROLES = {"implementer", "planner", "reviewer"}
FIXED_ROLES = {"coordinator", "curator", "checker", "arbiter", "steward"}
FIELDS = {"name", "description", "developer_instructions", "model", "model_reasoning_effort"}
FEATURES = {"apps", "plugins", "memories", "request_permissions_tool"}


def validate_config(config: dict) -> None:
    """Check the documented subset's shape/types, not a snapshot of policy values."""
    if set(config) != {"model", "model_reasoning_effort", "agents"}:
        raise ValueError("project config: missing or unsupported fields")
    agents = config["agents"]
    if not isinstance(agents, dict) or set(agents) != {
        "enabled",
        "max_concurrent_threads_per_session",
        "default_subagent_model",
        "default_subagent_reasoning_effort",
    }:
        raise ValueError("project config: missing or unsupported agents fields")
    for settings, model, effort in (
        (config, "model", "model_reasoning_effort"),
        (agents, "default_subagent_model", "default_subagent_reasoning_effort"),
    ):
        if not isinstance(settings[model], str) or not settings[model].strip():
            raise ValueError("project config: models must be nonempty strings")
        if not isinstance(settings[effort], str) or settings[effort] not in EFFORTS:
            raise ValueError("project config: unsupported effort")
    if type(agents["enabled"]) is not bool:
        raise ValueError("project config: enabled must be a boolean")
    cap = agents["max_concurrent_threads_per_session"]
    if type(cap) is not int or cap < 1:
        raise ValueError("project config: thread cap must be a positive integer")


def policy(root: Path = ROOT) -> tuple[dict, dict]:
    profiles = {}
    for path in sorted((root / ".codex/agents").glob("*.toml")):
        profile = tomllib.loads(path.read_text(encoding="utf-8"))
        if set(profile) != FIELDS | {"features"}:
            raise ValueError(f"{path.name}: missing or unsupported profile fields")
        if any(not isinstance(profile[k], str) or not profile[k].strip() for k in FIELDS):
            raise ValueError(f"{path.name}: profile fields must be nonempty strings")
        name = profile["name"]
        if name != path.stem or name in profiles:
            raise ValueError(f"{path.name}: name must match its unique file stem")
        if profile["model_reasoning_effort"] not in EFFORTS:
            raise ValueError(f"{path.name}: unsupported effort")
        features = profile["features"]
        if (
            not isinstance(features, dict)
            or set(features) != FEATURES
            or any(value is not False for value in features.values())
        ):
            raise ValueError(f"{path.name}: external capabilities must be disabled")
        profiles[name] = profile
    routing = tomllib.loads((root / ".codex/routing.toml").read_text(encoding="utf-8"))
    if set(routing) != set(TIERS) | {"fixed"}:
        raise ValueError("routing must cover every tier and fixed role")
    for tier, roles in routing.items():
        if not isinstance(roles, dict) or set(roles) != (
            FIXED_ROLES if tier == "fixed" else TASK_ROLES
        ):
            raise ValueError(f"{tier}: incomplete or unsupported roles")
        if any(not isinstance(name, str) or name not in profiles for name in roles.values()):
            raise ValueError(f"{tier}: route names an unavailable profile")
    referenced = {name for roles in routing.values() for name in roles.values()}
    if set(profiles) != referenced:
        raise ValueError("unrouted profiles are not allowed")
    config = tomllib.loads((root / ".codex/config.toml").read_text(encoding="utf-8"))
    validate_config(config)
    return profiles, routing


def resolve(root: Path, task_id: str | None, role: str) -> dict:
    profiles, routing = policy(root)
    tier = None
    if task_id is not None:
        tasks = load_tasks(root / "docs/backlog")
        if task_id not in tasks:
            raise ValueError(f"Unknown open task: {task_id}")
        tier = tasks[task_id].tier
    if role in TASK_ROLES and tier is None:
        raise ValueError("task-dependent roles require a task")
    name = routing["fixed"][role] if role in FIXED_ROLES else routing[tier][role]
    profile = profiles[name]
    return {
        "task": task_id,
        "tier": tier,
        "role": role,
        "profile": name,
        "file": f".codex/agents/{name}.toml",
        "model": profile["model"],
        "effort": profile["model_reasoning_effort"],
        "runtime_verified": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["check", "route"])
    parser.add_argument("--task")
    parser.add_argument("--role", choices=sorted(TASK_ROLES | FIXED_ROLES), default="implementer")
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            profiles, _ = policy()
            load_tasks(ROOT / "docs/backlog")
            print(f"{len(profiles)} Codex profiles and all task tiers valid (static only)")
        else:
            if not args.task and args.role in TASK_ROLES:
                parser.error("route requires --task for task-dependent roles")
            print(json.dumps(resolve(ROOT, args.task, args.role), indent=2))
    except (ValueError, OSError) as exc:
        print(f"agent profiles: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
