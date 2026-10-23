# Workflow automations

Local helpers for engineering management routines. Tools process local files unless
their specification says otherwise; the translation helper does not contact an LLM or
upload workbook data. `ado-triage` reads (never writes) an on-premise Azure DevOps server.

| Tool | Purpose | Specification |
| --- | --- | --- |
| `xlsx-translate` | Deduplicate workbook text and apply translations by ID | [XLSX translation](docs/specs/xlsx-translation.md) |
| `xlsx-outline` | Fold repeated left-to-right sheet values into a Markdown outline | [XLSX outline](docs/specs/xlsx-outline.md) |
| `xlsx-unfold` | Turn an edited outline back into rows, or merge it into the original | [XLSX outline](docs/specs/xlsx-outline.md#unfold-outline-to-table) |
| `gerrit-parents` | Match repositories listed in a sheet to their closest parent in a Gerrit tree outline | [Gerrit parents](docs/specs/gerrit-parents.md) |
| `team-roster` | Classify job titles, map people to ADO AreaPaths and count headcount per area | [Team roster](docs/specs/team-roster.md) |
| `team-capacity` | Turn the roster and a working calendar into net capacity hours per AreaPath for `ado-triage` | [Team capacity](docs/specs/team-capacity.md) |
| `team-capacity-calibrate` | Measure the bug-fix share of logged work from a saved ADO snapshot (exploratory) | [Team capacity](docs/specs/team-capacity.md#calibration) |
| `ado-graph` | Export ADO work-item graphs and analyze saved Parquet data | [ADO graph/export](docs/specs/ado-graph.md) |
| `ado-triage` | Rank ADO change-request scope into a triage agenda and team summary | [ADO scope triage](docs/specs/ado-triage.md) |

## Install

Python 3.11 or newer:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[translation]'
```

Install only the extras needed for the tools you use, or every tool's dependencies
at once with `python -m pip install -e '.[all]'`. `all` includes Kerberos support for
`ado-triage`, so on Linux install system Kerberos headers (such as `libkrb5-dev`)
first. On Windows, activate with `.venv\Scripts\Activate.ps1` in PowerShell instead.

## Translate a workbook

```bash
xlsx-translate extract \
  --input source.xlsx --sheet Data \
  --source-language ru --target-language en \
  --output-dir work/translation-bundle \
  --columns Status Description \
  --batch-max-rows 100 --batch-max-bytes 12000
```

The bundle contains:

- `sources.xlsx` and `sources/*.csv`: per-column `text_id,source_text` dictionaries.
- `translations.en.xlsx` and `translations/*.csv`: blank `text_id,translated_text_en` templates.
- `batches/*.csv`: optional size-limited source dictionaries to send to your LLM.
- `prompt.txt`: response instructions and column context.
- `agent_prompt.md`: step-by-step instructions for an LLM agent (see below).
- `manifest.json`: the authoritative dictionary, cell references, and source fingerprint.
- `summary.json`: deduplication and per-column counts.

For several languages from one extraction, pass `--target-language en de`. Sources,
batches and IDs are shared; templates and prompts are written per language
(`translations/de/*.csv`, `translations.de.xlsx`, `prompt.de.txt`,
`agent_prompt.de.md`), and each `apply` names its language with
`--target-language de`.

Translate source dictionaries or batches externally. Return **only two columns**:

```csv
text_id,translated_text_en
<exported-id>,Open
```

Keep IDs unchanged. The angle-bracket placeholder above is not a real ID. Leave an
unresolved translation blank; to explicitly retain a source value, put that value
in the translation field. Source and translation files are separate so the LLM
does not have to repeat the original text in its response.

```bash
xlsx-translate apply \
  --input source.xlsx \
  --manifest work/translation-bundle/manifest.json \
  --mappings work/returned/status.csv work/returned/description.csv \
  --output work/translated.xlsx --output-sheet Data_en
```

Mapping input can be one or more CSV files, XLSX workbooks, or both. Every supplied
XLSX sheet must use the two-column translation schema. The default requires all
translations; `--missing keep` keeps unresolved cells and exits with code 4.
Unknown IDs and conflicting translations always fail. Reordered replies are fine.
Blank lines and a surrounding code fence in a CSV reply are ignored; any other CSV
problem names the file, line and likely cause (for example an unquoted comma).

The original workbook is never modified. The output is a byte-for-byte copy of it
plus the translated sheet, written at the XML level, so other sheets keep charts
and drawings. Workbooks the XML writer cannot handle safely fall back to openpyxl;
`--writer openpyxl` always uses openpyxl and `--writer xml` fails instead of
falling back. The translated copy leaves out drawings, comments and tables, and
formula references are not rewritten. Prefer ordinary data tables. When the
workbook and manifest are unchanged since extraction, apply skips re-reading the
source cells. On a 50,000-row, 10-column sheet this takes about 3 seconds instead
of about 21. Formula expressions are copied unchanged and not evaluated. CSV fields
are literal data to this tool; use the XLSX templates for spreadsheet editing of
text that could be interpreted as formulas.

### Agent mode

To let an LLM agent that can read and write files (for example Claude Code) translate
the whole bundle, point it at `agent_prompt.md`. The prompt lists every work unit
(batch files, or per-column source CSVs without batching) with its row count and the
reply path to write, the reply rules and column context, and the final `apply`
command with every reply path filled in. The agent reads one unit at a time, writes
`replies/<unit>.csv`, checks it, and skips units that already have a complete reply,
so a small context window and an interrupted run both work. Use `--batch-max-rows`
to keep each unit small. The prompt is written only when CSV units exist. With
several target languages there is one prompt per language, such as
`agent_prompt.de.md`, writing to `replies/de/`.

Use `--formats csv` or `--formats xlsx` to select extraction formats. Deduplication
is per column by default; `--dedupe-scope global` shares a dictionary across columns
when context permits. Headings are unchanged unless `--translate-headings` is set.
Each extraction needs a new output directory; retain its manifest for application.

The equivalent Python invocation is `python scripts/xlsx_translate.py ...`, after
installation. `python -m workflow_automations.xlsx_translation ...` also works.

## Outline a workbook

```bash
python -m pip install -e '.[outline]'
xlsx-outline --input plan.xlsx --sheet Data --output work/plan.md
```

Each row's leftmost values become parents and only what changes nests beneath them,
so `Platform | Core | Scheduler` and `Platform | Core | Memory` become one `Platform`
item with one `Core` item holding both tasks. `--levels N` keeps only the first N
selected columns in the hierarchy and lists the rest as `Heading: value` details.
`--columns` or `--column-indices` select and reorder columns. `--heading-levels N`
renders the top N levels as `#` headings (with an optional `--title`), and
`--label-levels` prefixes items with their column heading. Only adjacent rows fold
by default, keeping sheet order; `--group` also merges repeats further down. For
sheets that leave a repeated parent blank, `--fill-down` reuses the value above
within the same parent. Without `--output` the outline goes to standard output; an
existing output file requires `--overwrite`. Formulas show the values Excel last
saved.

To edit the data as a mind map and bring the edits back, use `xlsx-unfold`. Each
path from the root to a leaf becomes a row again, so renaming or moving a parent
updates every row under it. It has two modes:

```bash
xlsx-outline --input plan.xlsx --levels 3 --row-ids --front-matter --output work/plan.md
# edit work/plan.md: rename, move, add or delete items

# 1. Unfold into a new workbook (text values, outline columns only)
xlsx-unfold --input work/plan.md --output work/plan-table.xlsx
#    add --retype to write numbers and dates as typed cells
# 2. Merge into a copy of the original, keeping other columns, types and formatting
xlsx-unfold --input work/plan.md --into plan.xlsx --output work/plan-merged.xlsx
```

`--row-ids` tags items with their sheet rows in hidden `<!-- rows: N -->` comments;
merging needs them, and the new-workbook mode uses them to restore duplicate rows.
`--front-matter` is optional: it records the column names and settings, plus a
fingerprint that stops a merge into a sheet changed since. If your editor drops or
mangles front matter, leave it out and pass `--columns`, `--details` (and any other
outline options you used) to `xlsx-unfold` instead. Neither mode modifies its inputs.
`--retype`, in either mode, writes text that is exactly how `xlsx-outline` prints a
number or date (`42`, `0.1`, `2024-01-02`) as a typed cell; IDs with leading zeros,
other spellings and Text-formatted cells stay text.

## Find Gerrit parents

```bash
python -m pip install -e '.[outline]'
gerrit-parents --input repos.xlsx --column repos --tree gerrit-tree.md \
  --output work/repos-parents.xlsx --outline work/parents.md
```

The tree is a Markdown outline of Gerrit projects (nested bullets or headings, the
same syntax `xlsx-unfold` reads). Each repository in the column is looked up by its
exact name and its parent is the item directly above it. `--output` writes a new
workbook with the sheet's columns plus `Parent`, `Path` and `Status`; `--outline`
writes each parent once with its repositories beneath it, in tree order. With
neither, the outline goes to standard output. Names missing from the tree, listed
under more than one parent, or at the top level are reported (and listed at the
end of the outline) instead of guessed; the exit code is then 4.

## Count people per AreaPath

```bash
python -m pip install -e '.[team-roster]'
team-roster staff.xlsx work/roster.xlsx --mapping area-mapping.xlsx
```

The input is a staff export with a `corporatetitle` column (and `email`,
`department_name`, `fullpath`, `approved`). The output copies it, adds
`classified_title` (for example `Senior Developer` becomes `developer`) and
`area_path`, and adds a `dev_per_area` sheet counting approved people per AreaPath
and title class. AreaPaths come from the `person_to_area`, `department_to_area` and
`fullpath_to_area` sheets, a person's own row first; sheets missing from the input
are taken from `--mapping` or created empty for you to fill in. `--dry-run` previews
the classification without writing; an existing output needs `--force` (or a `y` at
the prompt). The input is never modified.

## Plan capacity per AreaPath

```bash
python -m pip install -e '.[team-capacity]'
team-capacity --template capacity-config.xlsx
team-capacity --roster work/roster.xlsx --config capacity-config.xlsx \
  --start-month 2026-11 --months 3 --output work/capacity.xlsx
```

`--template` writes a starter config: the agreed defaults (8-hour days, two-week
iterations from Monday to the next Friday, daily scrum and code review, iteration
rituals, 7 sick days and 30 calendar days of paid leave a year, a 50% bug-fix quota and
12% focus loss) and the UAE private-sector calendar for 2026 and 2027, with holidays
and Ramadan's shorter days. Hijri dates are projections; check them against each
year's official announcement. Add booked leave to `absences`, and team- and phase-specific
rates to `rates`. The output's `dev_per_area_capacity` sheet is what
`ado-triage --capacity-file` reads; `capacity_by_role` and `capacity_by_person` show
where every hour went, and `problems` lists people who could not be counted (the run
then exits 3 unless `--allow-problems`).

To keep the calendar or other sheets outside the config, pass them per run:
`--calendar FILE` (an `.xlsx` or `.csv`, repeated for one file per year) replaces the
`calendar` sheet, `--sheet NAME=FILE` replaces any config sheet (for example
`--sheet absences=leave.csv`), and `--set KEY=VALUE` overrides a setting such as
`--set bug_fix_quota=0.4`. `--template-sheets config-sheets/` writes every template
sheet as `config-sheets/<sheet>.csv` (the names `--sheet` takes) to start from. The config workbook itself is never changed.

`team-capacity-calibrate --snapshot work/triage.json --roster work/roster.xlsx --config
capacity-config.xlsx --output work/calibration.xlsx` compares past iterations' completed
work with the model and suggests `rates` rows.

## Triage ADO scope

```bash
python -m pip install -e '.[ado-triage,ado-kerberos]'
ado-triage export \
  --url https://ado.example.com/tfs/Collection/Project \
  --query-id 00000000-0000-0000-0000-000000000000 \
  --capacity-file capacity.xlsx \
  --previous-file work/triage-2026-09-24.xlsx \
  --output work/triage-2026-10-01.xlsx
```

Authentication uses your Kerberos ticket. On Linux, install system Kerberos headers
(such as `libkrb5-dev`) before the `ado-kerberos` extra. Open the report at
**1. Executive Summary** (teams by RAG load), then **2. Triage Agenda** (items ranked
by risk-flag weight, with recommended actions). Pass last week's report as
`--previous-file` to see new, persisting and resolved risks. `--profile exec` writes
only the leadership sheets plus Master Data, so it still works as `--previous-file`; `--as-of` makes date-based flags reproducible. Add
`--team-dir work/teams` to also write one workbook per team, such as
`work/teams/triage-2026-10-01.Proj_Dept_Team A.xlsx`, so each team lead gets only their
own items. Existing output requires
`--overwrite`.

For a recurring run, `ado-triage export --stale-days 30 --profile exec --write-options
work/triage.ini` saves every option to a `key = value` file, and `ado-triage export
--options work/triage.ini --output work/triage-2026-10-08.xlsx` reuses it (options on the
command line win). `--write-config site.json` writes the `--config` JSON with every
field name, hour band, flag weight and RAG band at its default, ready to edit.

Add `--save-snapshot work/triage-2026-10-01.json` to also keep the fetched ADO data.
Later, `ado-triage export --from-snapshot work/triage-2026-10-01.json --as-of 2026-10-01
--output work/rerun.xlsx` rebuilds the same report offline, with no Kerberos ticket or
ADO access. The query ID and URL come from the snapshot, so omit `--query-id`.
Capacity, previous-report and team options work as usual.

## Development

```bash
python -m pip install -e '.[translation,outline,team-roster,team-capacity,ado-triage,dev]'
ruff check .
ruff format --check .
pytest
python -m build
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the shared development workflow and
[the backlog](docs/backlog/README.md) for proposed extensions. The Apache-2.0 license
applies to repository code.

## Export and analyze an ADO graph

`ado-graph` (its help still says `ado_tool`) reads ADO in export mode; analysis
reads local Parquet files. The `ado-graph-analyze` extra is enough for `analyze`;
`export` needs the `ado-graph` extra, which adds the ADO SDK and Kerberos.

```bash
# Linux: install libkrb5-dev first if the Kerberos dependencies need building.
python -m pip install -e '.[ado-graph]'            # or '.[ado-graph-analyze]' to analyze only
ado-graph export --ado-url https://ado.example/DefaultCollection \
  --project Example --query-id QUERY-GUID --output-dir work/ado-graph
ado-graph analyze --nodes-file work/ado-graph/nodes.parquet \
  --edges-file work/ado-graph/edges.parquet \
  --steps orphans,scc,longest_chain --output-file work/ado-graph/report.xlsx
```

By default, type and project filters stop traversal at filtered items, which are
classified once via WIQL and not fetched. Pass `--filtered-relations follow` to also
fetch filtered items, follow their links (including into other projects) and write
them as bridge nodes (`In_Scope=False`); analysis then replaces each bridge with an
`Indirect` edge so a Feature → Task → Feature chain stays connected. A bridge with many
links can multiply edges; `--max-indirect-edges` caps that (exiting before writing),
and `analyze --bridges drop` skips contraction. Export exits 4 when ADO did not return
some linked work items (outputs are written but incomplete) and 5, writing nothing,
when items from the saved query itself are missing. Outputs are published atomically;
replacing existing ones needs `--overwrite`, and analyze never writes over its inputs.
The default `--steps all` includes unbounded cycle enumeration and exact centrality;
choose steps deliberately on large graphs. Read the [specification](docs/specs/ado-graph.md) and
[prioritized findings](docs/backlog/README.md#ado-graph-and-triage-sharing) before using the results.
Add `--save-snapshot run.json` to an export to keep what ADO returned, and
`ado-graph replay --snapshot run.json --output-dir DIR` rebuilds the same export files
offline, with only the analyze extra.
Report sheets carry no organization-specific fields unless asked: add one with
`analyze --meta-field Team=Custom.Team`, or `--meta-preset legacy` for the former
`Roadmap_State` column.
Each mode also takes `--options FILE` (a `key = value` file of its options, such as
`steps = orphans,scc` or `meta-field = [Team=Custom.Team]`); `--write-options FILE`
writes every option with its current value as a starting point. The command line wins.
Excel holds at most 32,767 characters in a cell. By default a longer text cell keeps its
start, with a marker linking to a `Long_Text` sheet that holds the full text split across
cells; `--long-text trim` only shortens it, `--long-text keep` refuses (export) or moves the
sheet to CSV (analyze), and `--max-cell-chars N` shortens cells further for readability.
The equivalent script invocation is `python scripts/ado_graph.py ...` after installation.

## Agentic development

The repository includes Claude and Codex role definitions, a per-task backlog,
provider-neutral tiers, conflict-aware planning and a merge planner that batches
ready PRs and turns conflicting ones into trains. See
[agentic workflow](docs/agentic-workflow.md#codex-setup).

```bash
python scripts/backlog.py plan --busy AG-018
python scripts/agent_profiles.py check
python scripts/agent_profiles.py route --task AG-004 --role reviewer
python scripts/merge_plan.py --base origin/main 16=origin/<branch> 20=origin/<branch>
```

Codex runtime activation depends on your client; profile parsing alone does not
verify model routing. RW-003 tracks real-client probes and the initial pilots.
