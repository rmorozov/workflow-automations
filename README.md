# Workflow automations

Local helpers for engineering management routines. Tools process local files unless
their specification says otherwise; the translation helper does not contact an LLM or
upload workbook data. `ado-triage` reads (never writes) an on-premise Azure DevOps server.

| Tool | Purpose | Specification |
| --- | --- | --- |
| `xlsx-translate` | Deduplicate workbook text and apply translations by ID | [XLSX translation](docs/specs/xlsx-translation.md) |
| `xlsx-outline` | Fold repeated left-to-right sheet values into a Markdown outline | [XLSX outline](docs/specs/xlsx-outline.md) |
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

The original workbook is never modified. The output retains original sheets and
adds the translated copy. Full fidelity for charts, drawings, unsupported Excel
extensions, and formula reference rewriting is outside v1. Prefer ordinary data
tables. Formula expressions are copied unchanged and not evaluated. CSV fields
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
to keep each unit small. The prompt is written only when CSV units exist.

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
only the leadership sheets plus Master Data, so it still works as `--previous-file`; `--as-of` makes date-based flags reproducible. Existing
output requires `--overwrite`.

## Development

```bash
python -m pip install -e '.[translation,outline,ado-triage,dev]'
ruff check .
ruff format --check .
pytest
python -m build
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the shared development workflow and
[the backlog](docs/backlog.md) for proposed extensions. The Apache-2.0 license
applies to repository code.
