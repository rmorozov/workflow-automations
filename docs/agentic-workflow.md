# Agentic workflow

How Claude and Codex sessions and subagents work on this repository: who does what, which
model and effort each role uses, and how several branches run at once without
merge conflicts. Subagent definitions live in [`.claude/agents/`](../.claude/agents/);
the backlog format is in [docs/backlog](backlog/README.md).

## Roles

| Role | Where it runs | Does | Does not |
| --- | --- | --- | --- |
| Owner (human) | Project chat, GitHub | Sets priorities, decides forks that change goals, approves and merges PRs, owns repository settings | Write task files by hand (optional) |
| Coordinator | Project chat session | Triages asks, allocates backlog IDs, keeps the ledger of which thread works on which task, starts threads for the next wave | Edit code |
| Thread lead | One thread session per task (or per lane bundle) | Owns one branch and its PR; runs the pipeline below with subagents; drives CI to green | Touch files outside its task's `touches:` |
| `task-planner` | Subagent | Reads task + code, returns plan and the exact `touches:` | Edit files |
| `implementer` | Subagent | Tests, code, spec/README section, deletes the task file when done | Edit files outside `touches:` |
| `checker` | Subagent | Runs ruff, backlog check, pytest, build; reports failures | Fix or diagnose |
| `reviewer` | Subagent | Adversarial review against acceptance evidence and scope | Edit files |
| `backlog-curator` | Subagent | Files follow-ups with coordinator-issued IDs; proposes waves | Edit code |

## Claude models and effort

Prices are per million input/output tokens for the current models (cached
2026-10-06): Haiku 5.5 $0.10/$0.50, Sonnet 5.5 $2/$10, Opus 5.5 $4/$20,
Fable 5.1 $10/$50.

| Role | Model | Effort | Why |
| --- | --- | --- | --- |
| `checker` | Haiku 5.5 | low | Runs fixed commands and copies failures; no judgment. 20× cheaper than Sonnet and the most frequently run role. |
| `backlog-curator` | Sonnet 5.5 | low | Structured writing from existing findings; low effort keeps it terse. |
| `implementer` | Sonnet 5.5 | medium | Most tasks are local, well-specified changes with tests as the safety net. Medium is the recommended start for agentic coding on Sonnet 5.5. |
| `implementer` on tasks marked `tier: complex` | Opus 5.5 | as the task says | Graph semantics, completeness, schema contracts and algorithmic bounds (AG-003/004/006/007/012, SH-001/003/004/007): wrong results here pass tests that encode the same misunderstanding. |
| `task-planner` | Opus 5.5 | high | One short, read-heavy call whose mistakes multiply: a wrong `touches:` list breaks parallel scheduling, a wrong plan wastes an implementer round. |
| `reviewer` | Opus 5.5 | high (medium for P3) | Catching a defect before merge is cheaper than any later fix. Independence comes from a fresh context that reads only the task, spec and diff and reproduces findings itself; a different model from the implementer is a bonus, not the source of independence. |
| Thread lead | Opus 5.5 | medium | Orchestrates, reads PR events, decides when to escalate. Set as the project's default model; it is not configured in this repository. |
| Coordinator | Opus 5.5 | medium | Same as thread lead; its work is short triage turns. |
| Design arbiter, on demand | Fable 5.1 | xhigh | 2.5× Opus. Settles a specific, reproduced disagreement or a hard design choice the owner asks to stress-test. Never a standing stage, and not triggered merely because rounds elapsed. |

The thread lead may run a subagent at another model or effort than its definition
when the task's tier and actual risk require it; record the resolved model/effort
in the handoff. Claude maps mechanical→Haiku/low, routine→Sonnet/medium,
complex→Opus/high, deep→Fable/xhigh for implementation. Role defaults apply to
planning and review; task tiers do not change the coordinator's model.

### Pipeline per task

1. Thread lead reads the task file and claims it with the coordinator.
2. `task-planner` only when risk calls for it: P1, `tier: complex`/`deep`, or uncertain
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

Task files now contain `tier`, not a provider model or per-task effort. Existing
Haiku/Sonnet/Opus/Fable hints migrated to mechanical/routine/complex/deep respectively.
Effort is set per role profile so a task hint cannot silently disagree with the
profile's fixed setting. Use complex for graph semantics, schema/completeness or
publication risks; deep means a specific design dispute needing arbitration.
Small tasks need one implementation and fresh review, not every role.

| Role | Routine/mechanical default | Complex/deep default |
| --- | --- | --- |
| Coordinator/integration owner | GPT-6.1 Sol / medium | Same; escalate a specific decision to arbiter |
| Planner | GPT-6.1 Sol / high | GPT-6 Astra / high |
| Implementer | Sol / medium; Luna / high for mechanical work | GPT-6 Astra / high |
| Independent reviewer | GPT-6.1 Sol / high | GPT-6 Astra / high |
| Backlog curator | GPT-6 Luna / medium | Same for established findings; owner handles ambiguity |
| Checker summary | GPT-6 Luna / low | Same; shell/CI execute checks |
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
python scripts/agent_profiles.py route --task AG-004 --role reviewer
```

`route` resolves the task tier to the exact profile, model and effort. It does not
spawn agents and always reports `runtime_verified: false`. Dispatch that named
custom profile in a compatible client. Changing the root session or invoking a
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

Static policy/TOML tests pass before a PR, but they cannot prove client support,
account access or profile discovery. No Codex CLI is installed in the setup
execution environment; real profile loading and pilots remain tracked by RW-003.

1. In your actual Codex client, open a fresh trusted repository session. Check the
   installed client version and current custom-agent docs. User/managed settings
   may override or ignore project configuration. Never overwrite personal settings.
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
