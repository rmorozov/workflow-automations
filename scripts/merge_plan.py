"""Plan the merge order of open pull requests as batches and trains, using local git only.

Each pull request is given as LABEL=REF (for example 16=origin/claude/project-thread-b2ec4u),
in the owner's priority order. The planner never fetches, pushes or merges; run
`git fetch origin` first. It test-merges in memory with `git merge-tree`:

- Batch 1 holds every pull request that merges cleanly onto the base together with the
  ones before it. Its integration commit is printed so the checks can run on exactly
  the tree that main will have once the whole batch is merged.
- A pull request that conflicts with an earlier one (or must come `--after` one) waits for
  a later batch: together they form a train, and each later car is updated from main
  after the car it waits on has merged.
- A pull request that conflicts with the base alone is blocked until its branch is updated.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field

# Fixed identity and dates make the integration commit id reproducible for the same inputs.
_COMMIT_ENV = {
    "GIT_AUTHOR_NAME": "merge-plan",
    "GIT_AUTHOR_EMAIL": "merge-plan@localhost",
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
    "GIT_COMMITTER_NAME": "merge-plan",
    "GIT_COMMITTER_EMAIL": "merge-plan@localhost",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
}


@dataclass
class Pull:
    label: str
    ref: str
    files: list[str] = field(default_factory=list)
    behind: int = 0


@dataclass
class Batch:
    pulls: list[str]
    # Only batch 1 is computed against the real base; later batches need their cars updated first.
    commit: str | None = None
    deferred: dict[str, str] = field(default_factory=dict)


def git(*args: str, repo: str = ".", check: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, **_COMMIT_ENV}
    result = subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, env=env, check=False
    )
    if check and result.returncode != 0:
        raise ValueError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result


def trial_merge(ours: str, theirs: str, repo: str) -> tuple[str, list[str]]:
    """Return the merged tree and the conflicted paths (empty when the merge is clean)."""
    result = git(
        "merge-tree", "--write-tree", "--name-only", "--no-messages", ours, theirs,
        repo=repo, check=False,
    )  # fmt: skip
    if result.returncode not in (0, 1):
        raise ValueError(f"git merge-tree {ours} {theirs}: {result.stderr.strip()}")
    lines = [line for line in result.stdout.splitlines() if line]
    return lines[0], sorted(set(lines[1:]))


def load_pulls(base: str, specs: list[str], repo: str) -> list[Pull]:
    pulls, seen = [], set()
    for spec in specs:
        label, sep, ref = spec.partition("=")
        if not sep or not label or not ref:
            raise ValueError(f"expected LABEL=REF, got {spec!r}")
        if label in seen:
            raise ValueError(f"duplicate label {label!r}")
        seen.add(label)
        git("rev-parse", "--verify", f"{ref}^{{commit}}", repo=repo)
        files = git("diff", "--name-only", f"{base}...{ref}", repo=repo).stdout.split()
        behind = int(git("rev-list", "--count", f"{ref}..{base}", repo=repo).stdout)
        pulls.append(Pull(label, ref, files, behind))
    return pulls


def plan(
    base: str, pulls: list[Pull], after: dict[str, set[str]], repo: str = "."
) -> tuple[list[Batch], dict[str, str]]:
    """Group pulls into batches in priority order; return the batches and the blocked pulls."""
    labels = {pull.label for pull in pulls}
    for later, earlier in after.items():
        unknown = ({later} | earlier) - labels
        if unknown:
            raise ValueError(f"--after names unknown labels: {', '.join(sorted(unknown))}")
    base_commit = git("rev-parse", f"{base}^{{commit}}", repo=repo).stdout.strip()
    blocked, remaining = {}, []
    for pull in pulls:
        _, conflicts = trial_merge(base_commit, pull.ref, repo)
        if conflicts:
            blocked[pull.label] = f"conflicts with the base in {', '.join(conflicts)}"
        else:
            remaining.append(pull)
    batches: list[Batch] = []
    scheduled: set[str] = set()
    while remaining:
        head, batch, deferred = base_commit, Batch([]), []
        for pull in remaining:
            pending = sorted(after.get(pull.label, set()) - scheduled)
            if pending:
                deferred.append(pull)
                batch.deferred[pull.label] = f"after {', '.join(pending)}"
                continue
            tree, conflicts = trial_merge(head, pull.ref, repo)
            if conflicts:
                deferred.append(pull)
                batch.deferred[pull.label] = (
                    f"conflicts with {', '.join(batch.pulls)} in {', '.join(conflicts)}"
                )
                continue
            message = f"merge-plan: {' '.join(batch.pulls + [pull.label])}"
            head = git(
                "commit-tree", tree, "-p", head, "-p", pull.ref, "-m", message, repo=repo
            ).stdout.strip()
            batch.pulls.append(pull.label)
        if not batch.pulls:
            for pull in deferred:
                blocked[pull.label] = f"waits on blocked or circular {batch.deferred[pull.label]}"
            break
        if not batches:
            batch.commit = head
        batches.append(batch)
        scheduled.update(batch.pulls)
        remaining = deferred
    return batches, blocked


def overlaps(pulls: list[Pull]) -> list[tuple[str, str, list[str]]]:
    """Pairs of pulls that edit the same files, whether or not git merges them cleanly."""
    pairs = []
    for i, first in enumerate(pulls):
        for second in pulls[i + 1 :]:
            shared = sorted(set(first.files) & set(second.files))
            if shared:
                pairs.append((first.label, second.label, shared))
    return pairs


def render(pulls: list[Pull], batches: list[Batch], blocked: dict[str, str]) -> str:
    by_label = {pull.label: pull for pull in pulls}
    lines = []
    for number, batch in enumerate(batches, 1):
        if number == 1:
            lines.append(f"Batch 1: merge now; test commit {batch.commit}")
        else:
            lines.append(f"Batch {number}: after batch {number - 1} merges; update each from main")
        for label in batch.pulls:
            pull = by_label[label]
            why = batches[number - 2].deferred[label] if number > 1 else "clean"
            lines.append(
                f"  {label}  {pull.ref}  {len(pull.files)} files, {pull.behind} behind; {why}"
            )
    for label, why in blocked.items():
        lines.append(f"Blocked: {label}  {by_label[label].ref}; {why}")
    shared = overlaps(pulls)
    if shared:
        lines.append("Shared files (clean merges still need the batch checks):")
        lines += [f"  {a} & {b}: {', '.join(files)}" for a, b, files in shared]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("pulls", nargs="+", metavar="LABEL=REF")
    parser.add_argument("--base", default="origin/main")
    parser.add_argument(
        "--after",
        action="append",
        default=[],
        metavar="LATER:EARLIER",
        help="LATER must merge in a later batch than EARLIER (repeatable)",
    )
    parser.add_argument("--repo", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    after: dict[str, set[str]] = {}
    try:
        for spec in args.after:
            later, sep, earlier = spec.partition(":")
            if not sep or not later or not earlier or later == earlier:
                raise ValueError(f"expected LATER:EARLIER, got {spec!r}")
            after.setdefault(later, set()).add(earlier)
        pulls = load_pulls(args.base, args.pulls, args.repo)
        batches, blocked = plan(args.base, pulls, after, args.repo)
    except ValueError as exc:
        print(f"merge plan: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(
            json.dumps(
                {
                    "base": args.base,
                    "batches": [batch.__dict__ for batch in batches],
                    "blocked": blocked,
                    "overlaps": [
                        {"pulls": [a, b], "files": files} for a, b, files in overlaps(pulls)
                    ],
                },
                indent=2,
            )
        )
    else:
        print(render(pulls, batches, blocked))
    return 0


if __name__ == "__main__":
    sys.exit(main())
