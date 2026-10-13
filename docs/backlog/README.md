# Backlog

These are proposals, not promises made by the current specifications. Each task is
one file, `<ID>.md`, so parallel branches never edit the same backlog line.

```bash
python scripts/backlog.py              # every open task
python scripts/backlog.py ready        # tasks whose dependencies are done
python scripts/backlog.py plan --busy AG-018
                                       # waves of ready tasks that touch disjoint files
python scripts/backlog.py check        # validate the files (also run by pytest)
```

## Task file

```markdown
---
id: AG-010
title: Offline analysis without Azure SDK or Kerberos
tool: ado-graph
priority: P2
depends_on: [AG-018]
touches: [src/workflow_automations/ado_graph/client.py, docs/specs/ado-graph.md#purpose-and-boundaries]
model: sonnet
effort: medium
---

# AG-010 Offline analysis without Azure SDK or Kerberos

What is wrong and the concrete follow-up.

## Acceptance evidence

What a test or measurement must show for the task to be done.
```

- `id` matches the file name. Prefixes: `AG` ADO graph, `SH` code shared by the ADO
  tools, `TR` ADO triage, `XT` XLSX translation, `XO` XLSX outline/unfold, `GP`
  Gerrit parents, `RW` repository workflow.
- `depends_on` lists tasks whose result this one needs. A deleted task is done, so a
  dependency on a missing ID is satisfied.
- `touches` lists the files the change will edit: a path, a directory ending in `/`,
  or `path#section` for one section of a shared Markdown file (the anchor of its
  heading). Two tasks may run in parallel only when their `touches` are disjoint;
  different sections of one file count as disjoint.
- `model` and `effort` say which tier should implement it; see
  [agentic workflow](../agentic-workflow.md#models-and-effort).
- Titles avoid `: ` so the front matter stays one `key: value` per line.

## Lifecycle

1. **New finding.** The coordinator allocates the next free ID for its prefix (never
   pick one on a branch: two branches would pick the same number) and the task file
   is added in the PR that found it, or in a small backlog PR.
2. **Claimed.** The coordinator records which thread works on which ID. Nothing in
   the file changes, so claiming causes no merge traffic.
3. **Done.** The PR that meets every acceptance line deletes the file. If it meets
   only some, it rewrites the file to describe what remains.

Priorities: P1 wrong or unsafe results, P2 robustness and scale, P3 convenience.

## ADO graph and triage sharing

The AG items started as findings from the verbatim import of `ado-graph` and its
comparison with `ado-triage`; AG-001, AG-014 and AG-015 are implemented. The SH
items share small tested boundaries between the two tools rather than merging their
CLI modes or their different notions of scope. The highest-value order is
completeness and safe IO → explicit graph semantics → small transport and
normalization helpers → shared snapshot replay → performance and UX. Avoid a
general-purpose ADO framework until both tools need the same contract.
