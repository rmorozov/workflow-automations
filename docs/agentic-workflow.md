# Agentic workflow

How Claude and Codex sessions and subagents work on this repository: who does what, which
model and effort each role uses, and how several branches run at once without
merge conflicts. Subagent definitions live in [`.claude/agents/`](../.claude/agents/);
the backlog format is in [docs/backlog](backlog/README.md).

## Roles

| Role | Where it runs | Does | Does not |
| --- | --- | --- | --- |
| Owner (human) | Project chat, GitHub | Sets priorities, decides forks that change goals, reviews PRs and approves each merge batch, owns repository settings | Write task files by hand (optional) |
| Coordinator | Project chat session | Triages asks, allocates backlog IDs, keeps the ledger of which thread works on which task, starts threads for the next wave | Edit code |
| Thread lead | One thread session per task (or per lane bundle) | Owns one branch and its PR; runs the pipeline below with subagents; drives CI to green | Touch files outside its task's `touches:` |
| `task-planner` | Subagent | Reads task + code, returns plan and the exact `touches:` | Edit files |
| `implementer` | Subagent | Tests, code, spec/README section, deletes the task file when done | Edit files outside `touches:` |
| `checker` | Subagent | Runs ruff, backlog check, pytest, build; reports failures | Fix or diagnose |
| `reviewer` | Subagent | Adversarial review against acceptance evidence and scope | Edit files |
| `backlog-curator` | Subagent | Files follow-ups with coordinator-issued IDs; proposes waves | Edit code |
| `merge-steward` | Subagent of the integration owner | Orders ready PRs into batches and trains, validates each batch tree, bisects a failing batch | Merge, push, comment or edit |

## Claude models and effort

Prices are per million input/output tokens for the current models (cached
2026-10-06): Haiku 5.5 $0.10/$0.50, Sonnet 5.5 $2/$10, Opus 5.5 $4/$20,
Fable 5.1 $10/$50.

| Role | Model | Effort | Why |
| --- | --- | --- | --- |
| `checker` | Haiku 5.5 | low | Runs fixed commands and copies failures; no judgment. 20× cheaper than Sonnet and the most frequently run role. |
| `backlog-curator` | Sonnet 5.5 | low | Structured writing from existing findings; low effort keeps it terse. |
| `merge-steward` | Sonnet 5.5 | medium | `merge_plan.py` and the checks carry the deterministic part; the judgment left is ordering and reading diffs where PRs share files. A wrong plan is caught by the batch checks before anything merges, so Opus buys little. |
| `implementer` | Sonnet 5.5 | medium | Most tasks are local, well-specified changes with tests as the safety net. Medium is the recommended start for agentic coding on Sonnet 5.5. |
| `implementer` on tasks marked `tier: complex` | Opus 5.5 | high | Graph semantics, completeness, schema contracts and algorithmic bounds (AG-003/004/006/007/012, SH-001/003/004/007): wrong results here pass tests that encode the same misunderstanding. |
| `task-planner` | Opus 5.5 | high | One short, read-heavy call whose mistakes multiply: a wrong `touches:` list breaks parallel scheduling, a wrong plan wastes an implementer round. |
| `reviewer` | Opus 5.5 | high (medium for P3) | Catching a defect before merge is cheaper than any later fix. Independence comes from a fresh context that reads only the task, spec and diff and reproduces findings itself; a different model from the implementer is a bonus, not the source of independence. |
| Thread lead | Opus 5.5 | medium | Orchestrates, reads PR events, decides when to escalate. Set as the project's default model; it is not configured in this repository. |
| Coordinator | Opus 5.5 | medium | Same as thread lead; its work is short triage turns. |
| Design arbiter, on demand | Fable 5.1 | xhigh | 2.5× Opus. Settles a specific, reproduced disagreement or a hard design choice the owner asks to stress-test. Never a standing stage, and not triggered merely because rounds elapsed. |

The thread lead may run a subagent at another model or effort than its definition
when the task's tier and actual risk require it; record the resolved model/effort
in the handoff. Claude maps mechanical→Haiku/low, routine→Sonnet/medium,
complex→Opus/high for implementation. Arbitration is a fixed on-demand role,
not a task tier. Role defaults apply to planning and review; task tiers do not change the coordinator's model.

### Pipeline per task

1. Thread lead reads the task file and claims it with the coordinator.
2. `task-planner` only when risk calls for it: P1, `tier: complex`, or uncertain
   `touches:`. Small tasks go straight to one implementer and a fresh review.
   Correct the task file's `touches:` first if the plan differs, then re-plan the
   wave.
3. `implementer` using the provider's resolved profile for the task tier.
4. `checker`. Failures go back to the implementer.
5. `reviewer`. Blocking findings go back to the implementer. When a finding stays
   unresolved because the implementer cannot get the semantics right, rerun it one
   tier up (Sonnet → Opus); bring in the Fable arbiter only for a reproduced
   disagreement about what is correct.
6. Thread lead opens the draft PR, drives CI green, and asks the owner to review.

These tiers are starting defaults to pilot, not a measured optimum. As an
illustration of scale only (assumed token counts, list prices, no cache
discounts): planner 60k/8k tokens on Opus ≈ $0.40, implementer 300k/40k on Sonnet
≈ $1.00, two checker runs ≈ $0.01, reviewer 150k/10k on Opus ≈ $0.80. Start with
at most two editing threads plus one read-only reviewer, and compare a few
completed tasks on wall time, retries, usage and escaped defects before adding
threads or raising default tiers.

## Parallel work without merge conflicts

A merge conflict is two branches changing the same lines. Each rule below removes
one source of shared lines.

| Shared resource | Conflict it caused | Rule |
| --- | --- | --- |
| `docs/backlog.md` | Every PR edited the same table | One file per task; done means deleted; IDs only from the coordinator. |
| `ado_graph.py` (1,458 lines) | Nearly every AG task edits one module | AG-018 splits it into `cli`, `client`, `export`, `analyze`, `report` first, as a pure move, alone. |
| Specifications and README | Several tasks append to the same section | Tasks name the section they edit (`path#anchor`); edit only that section. |
| `pyproject.toml`, `ci.yml`, `AGENTS.md`, `CONTRIBUTING.md` | Adjacent one-line edits | Only tasks that list them may edit them; `plan` never schedules two together. |
| Task claims | Two threads on one task | The coordinator keeps the ledger until a claim is explicitly released; pass in-progress IDs to `plan --busy`. |
| Shared working directory | Two writers in one checkout | Each editing thread works in its own branch and checkout; integration and merge decisions stay with one owner. |
| Long-lived branches | Drift from `main` | One task (or one lane bundle) per branch, branched from current `main`; merge `main` in before asking for review; merge small PRs quickly. |

`python scripts/backlog.py plan --busy <IDs>` groups ready tasks into waves whose
`touches:` are disjoint. Wave 1 can start now; tasks that conflict with a busy one
are listed separately as waiting until its PR merges. Tasks in the same tool module land in successive waves:
the module, not the scheduler, is the limit.

### Recommended order

1. AG-018 alone among ADO tasks; tasks in other tools (`plan` wave 1) run beside it.
2. AG-010 and AG-004 in parallel (client/CLI boundary vs analysis semantics).
3. Lanes in parallel, each one branch at a time: report IO (AG-002 → AG-005 →
   AG-016 → SH-006), analysis (AG-007, AG-006, AG-013), client (AG-003, AG-008,
   AG-009, AG-011, AG-017).
4. Shared boundaries (SH-002, SH-005, then SH-001/003/004) one at a time, since
   they all create `ado_common/`; then AG-012 and TR-009 on the shared snapshot.

### Merging: batches and trains

Every merge moves `main`, and a moved `main` makes every other open PR's green CI
evidence about a tree that no longer exists. Merging one PR at a time and
re-running CI on each of the rest costs one CI round and one owner touch per PR.
The merge steward instead tests the combined result once and only serialises
PRs that really collide.

- A **batch** is a set of ready PRs that git merges cleanly together. The steward
  checks the batch's integration commit, which has exactly the tree `main` will
  have after the whole batch merges with the modelled method, so the batch merges
  back to back without re-running each PR's CI.
- A **train** is a chain of PRs that cannot share a batch: a later car conflicts
  with an earlier one, or is stacked on it (`--after`). Each car lands in the next
  batch after the one it waits on, once its thread lead has merged `main` into its
  branch, resolved the conflict and got CI green again.
- A PR that conflicts with `main` alone is blocked until its branch is updated.

`main` is squash-merged, so `merge_plan.py` models squash by default: each PR lands
as one single-parent commit. A PR stacked on another then conflicts or duplicates
in the plan exactly as on GitHub, and becomes a train car without needing
`--after`. Use `--method merge` only if the batch will be merged with merge
commits; rebase merges are not modelled.

```bash
git fetch origin
python scripts/merge_plan.py --base origin/main \
    16=origin/<branch> 20=origin/<branch> 21=origin/<branch> \
    --after 21:16       # 21 is stacked on 16
```

Who does what:

1. The integration owner (the coordinator, or a steward thread it starts for a
   wave, which counts against the thread cap) gives the `merge-steward` subagent
   the open thread PRs with their CI and review state.
2. The steward keeps those that are non-draft, green on their head and free of
   unresolved review threads, orders them (PRs that unblock waiting tasks first,
   then P1, then older) and runs `merge_plan.py`. It runs `scripts/check.sh` on
   batch 1's test commit in a disposable worktree with its own fresh virtual
   environment and an editable install of that worktree, so the tests import the
   batch's `src/` and not another checkout's. It bisects a failing batch by
   re-planning half of it at a time, and checks pairs across the halves when
   neither half fails alone. It returns the plan with the base and head SHAs; it
   never merges.
3. The owner approves the batch as one decision: its PRs, their order and the
   check result. Nothing merges without that approval.
4. The integration owner fetches and confirms that `origin/main` is still the
   planned base and each PR head is still the planned SHA; anything moved means
   re-plan. It then squash-merges the batch in the planned order, pinning each
   merge to its planned head SHA (GitHub refuses the merge if the head moved), and
   confirms that `git rev-parse origin/main^{tree}` equals the test commit's tree.
   A mismatch means something else merged meanwhile: stop and re-plan. If CI on `main` then fails on a combination the
   local check did not cover (another Python or pandas version), revert through a
   PR rather than pushing to `main`.
5. For each train car, the coordinator asks the owning thread lead to merge `main`
   into its branch and resolve the conflict; the integration owner updates a
   branch itself only when that thread has ended, with a merge commit, never a
   rebase. The steward re-plans once those cars are green.

Shared files that merge cleanly are listed, not serialised: the batch checks are
the evidence that they still work together, and the steward's reading of both
diffs is the warning. A GitHub merge queue would do steps 2 to 4 on the server,
but it needs branch protection, which only the owner can change (RW-001).

### What comes next

Splitting the backlog and `ado_graph.py` moves the bottleneck rather than removing
it:

- `ado_triage.py` (1,291 lines) and `xlsx_translation.py` (1,130 lines) serialize
  their own backlogs the same way. Split them when two tasks for the same tool are
  wanted at once, not before.
- `ado_common/` becomes the new shared hotspot once SH tasks start; keep it to one
  open branch at a time until its modules settle.
- With more branches in flight, owner review becomes the limit. Small PRs, the
  reviewer subagent's findings in the PR, and branch protection (RW-001) keep each
  review short.

## Codex setup

Project configuration is in `.codex/config.toml`; custom roles are standalone
`.codex/agents/<name>.toml` files. Repository routing policy lives in
`.codex/routing.toml`, consumed by our helper, not by Codex itself.
Checked against [official subagent documentation](https://learn.chatgpt.com/docs/agent-configuration/subagents)
on 2026-10-14. Profiles specify both model and effort. Custom profile values take
precedence over inherited/default spawn settings in clients that load them.
Permissions, provider, MCP connections and service tier inherit from the parent;
these files deliberately do not claim to override sandbox/approval settings.
Read-only roles have explicit no-edit instructions; that is not a security sandbox.

### Task tiers and resolved roles

Task files now contain `tier`, not a provider model or per-task effort.
Initial Haiku/Sonnet/Opus hints mapped to mechanical/routine/complex; task content
takes precedence over that initial mapping. AG-018 is mechanical because it moves
existing code without behavior changes; its P1 priority still warrants planning
and independent review.
Effort is set per role profile so a task hint cannot silently disagree with the
profile's fixed setting. Use complex for graph semantics, schema/completeness or
publication risks. Arbitration remains on demand for a reproduced disagreement,
independent of the task tier.
Small tasks need one implementation and fresh review, not every role.

This changes effort policy: it does not preserve the old per-task effort hints.
Before this setup, 13 Sonnet tasks used low and 8 Opus tasks used medium. Role
defaults replace those choices. AG-018 now uses mechanical routing; the other
12 Sonnet/low and 8 Opus/medium tasks still start at higher Claude effort by this
policy. Even mechanical maps to different effort settings
in Claude and Codex. Higher effort is not a demonstrated cost or quality gain.
Content review keeps validation, counters, report layout, CSV/config adapters and
small helper extraction routine; completeness, graph metrics, auth lifetime,
cross-bundle identity, workbook range integrity and parser equivalence remain
complex. Revisit these defaults using pilot usage and retries rather than assuming
the old model label or token price establishes the best tier.

| Role | Routine/mechanical default | Complex default |
| --- | --- | --- |
| Coordinator/integration owner | GPT-6.1 Sol / medium | Same; escalate a specific decision to arbiter |
| Planner | GPT-6.1 Sol / high | GPT-6 Astra / high |
| Implementer | Sol / medium; Luna / high for mechanical work | GPT-6 Astra / high |
| Independent reviewer | GPT-6.1 Sol / high | GPT-6 Astra / high |
| Backlog curator | GPT-6 Luna / medium | Same for established findings; owner handles ambiguity |
| Checker summary | GPT-6 Luna / low | Same; shell/CI execute checks |
| Merge steward | GPT-6.1 Sol / medium | Same; the batch checks are the safety net |
| Design arbiter (on demand) | GPT-6 Astra / xhigh | Only for a concrete unresolved disagreement |

The root default is Sol/medium, and the session cap is three spawned threads
(excluding root), with at most two editing workers by instruction. Threads for
planning/curation consume this cap too; close idle agents instead of opening more.
A P1 routine task may explicitly select a complex planner/reviewer after recording
why. Do not silently fall back if a requested model is unavailable.

```bash
python scripts/backlog.py check
python scripts/backlog.py plan --busy AG-018
python scripts/agent_profiles.py check
python scripts/agent_profiles.py route --role coordinator
python scripts/agent_profiles.py route --role steward
python scripts/agent_profiles.py route --task AG-004 --role reviewer
```

`route` resolves the task tier to the exact profile, model and effort. It does not
spawn agents and always reports `runtime_verified: false`. Fixed roles can be
resolved without `--task`; task-dependent roles require an open task ID.
Dispatch that named custom profile in a compatible client. Changing the root session or invoking a
built-in worker is not equivalent to loading the profile. The checker commands
are consolidated in `scripts/check.sh`; run it in a disposable validation worktree,
as pytest/build can generate caches or metadata. No per-task scheduler daemon,
model API calls or credentials are added.

### Work ownership and handoffs

One editing worker per isolated worktree/branch. Coordinator owns task IDs, claims,
integration, remote comments, pushes and merges under the user's authorization.
Workers return diffs and evidence, not remote messages. Reserve tests and spec
sections along with code before dispatch; re-plan when reservations change.
Review from fresh context: original task, specification, base/head SHAs and actual
diff, followed by independent reproductions. Do not feed the implementer's
explanation as the reviewer's initial conclusion.

A handoff records task ID/tier, role/profile, expected model/effort, observed
client telemetry (or unverified), base/head SHAs, worktree, allowed paths/sections,
acceptance evidence, constraints and the next decision. An agent's self-reported
model identity is not evidence of actual routing. No edits/pushes/merges by planner,
reviewer or arbiter; temporary reproductions are allowed. Curator edits only its
assigned task files; checker uses the validation worktree and returns exact logs.

### Activation and pilot checklist

Static checks cover the documented key structure, types and routing completeness,
not a duplicate snapshot of model names, effort choices or the thread cap. Those
values live in TOML. Changes to routing/caps also need matching workflow guidance.
Static policy/TOML tests pass before a PR, but they cannot prove client support,
account access or profile discovery. No Codex CLI is installed in the setup
execution environment; real profile loading and pilots remain tracked by RW-003.

1. In your actual Codex client, open a fresh trusted repository session. Check the
   installed client version and current custom-agent docs. Confirm every config and
   profile key is recognised; record unsupported/ignored settings. User/managed
   settings may override or ignore project configuration. Never overwrite personal settings.
2. Ask: “Spawn the custom task_planner agent to read this repository and return
   a path-only plan without editing files.” Observe the named profile and runtime
   model/effort in client telemetry/event logs. Repeat for each profile without
   editing/publishing; no claim based solely on a model's answer or valid TOML.
3. For no-edit roles, compare git status before/after and review the tool transcript.
   For worker/curator probes, allow only a synthetic file in a temporary worktree.
   Checker uses a disposable checkout. All retain the root permissions.
4. If the client cannot select custom profiles but exposes explicit model/effort
   spawn controls, pass the resolved instructions/model/effort with fresh context
   and record `manual adapter`; this does not verify native TOML discovery. If only
   inherited full-context forks are available, document the actual inherited model
   and do not claim cheaper role routing. In this Work session, explicit model
   overrides require a fresh-context spawn; full-context forks inherit the parent.
5. Pilot one owner-chosen small task on routine routing and one graph-semantics
   task on complex routing (after its dependencies). Record runtime settings,
   elapsed time, usage if exposed, review retries, tests and escaped defects in
   the PRs. Require human review before adopting stronger defaults or more writers.

Setup creates profiles, not active background workers. These are starting defaults,
not measured savings. Keep provider model/pricing claims out of task files.
