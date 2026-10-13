# Agentic workflow

How Claude sessions and subagents work on this repository: who does what, which
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

## Models and effort

Prices are per million input/output tokens for the current models (cached
2026-10-06): Haiku 5.5 $0.10/$0.50, Sonnet 5.5 $2/$10, Opus 5.5 $4/$20,
Fable 5.1 $10/$50.

| Role | Model | Effort | Why |
| --- | --- | --- | --- |
| `checker` | Haiku 5.5 | low | Runs fixed commands and copies failures; no judgment. 20× cheaper than Sonnet and the most frequently run role. |
| `backlog-curator` | Sonnet 5.5 | low | Structured writing from existing findings; low effort keeps it terse. |
| `implementer` | Sonnet 5.5 | medium | Most tasks are local, well-specified changes with tests as the safety net. Medium is the recommended start for agentic coding on Sonnet 5.5. |
| `implementer` on tasks marked `model: opus` | Opus 5.5 | as the task says | Graph semantics, completeness, schema contracts and algorithmic bounds (AG-003/004/006/007/012, SH-001/003/004/007): wrong results here pass tests that encode the same misunderstanding. |
| `task-planner` | Opus 5.5 | high | One short, read-heavy call whose mistakes multiply: a wrong `touches:` list breaks parallel scheduling, a wrong plan wastes an implementer round. |
| `reviewer` | Opus 5.5 | high (medium for P3) | Catching a defect before merge is cheaper than any later fix. Independence comes from a fresh context that reads only the task, spec and diff and reproduces findings itself; a different model from the implementer is a bonus, not the source of independence. |
| Thread lead | Opus 5.5 | medium | Orchestrates, reads PR events, decides when to escalate. Set as the project's default model; it is not configured in this repository. |
| Coordinator | Opus 5.5 | medium | Same as thread lead; its work is short triage turns. |
| Design arbiter, on demand | Fable 5.1 | xhigh | 2.5× Opus. Settles a specific, reproduced disagreement or a hard design choice the owner asks to stress-test. Never a standing stage, and not triggered merely because rounds elapsed. |

The thread lead may run a subagent at another model or effort than its definition
when the task file's `model`/`effort` says so; that field is the owner-visible
record of the choice.

### Pipeline per task

1. Thread lead reads the task file and claims it with the coordinator.
2. `task-planner` only when risk calls for it: P1, `model: opus`, or uncertain
   `touches:`. Small tasks go straight to one implementer and a fresh review.
   Correct the task file's `touches:` first if the plan differs, then re-plan the
   wave.
3. `implementer` at the task's model and effort.
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
