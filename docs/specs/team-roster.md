# Team roster specification, v1

Status: implemented; imported from the owner's title-classification script with its
behaviour kept (differences are listed under [Changes from the
script](#changes-from-the-script)). Entry points: `team-roster`,
`python scripts/team_roster.py`, or `python -m workflow_automations.team_roster`.
Install the `team-roster` extra (pandas, openpyxl).

## Goal

Capacity planning needs to know how many people of which kind work in each ADO
AreaPath. HR exports list people with free-text corporate titles and organizational
units, not AreaPaths. The tool normalizes each title to a small set of classes,
maps each person to an AreaPath, and counts approved people per AreaPath and class.
That headcount is the input from which the `dev_per_area_capacity` sheet read by
[`ado-triage`](ado-triage.md) is prepared. The tool is local and deterministic.

```bash
team-roster INPUT.xlsx OUTPUT.xlsx [--mapping MAPPING.xlsx] [--dry-run] [--force] [-v] [-q]
```

## Input

`INPUT.xlsx` is read in full and never modified. The **main sheet** is the first
sheet with a `corporatetitle` column; without one the run fails. Its other columns
used by the tool, all optional, are `email`, `department_name`, `fullpath` and
`approved`. Column names are exact and case-sensitive.

Three **mapping sheets** give AreaPaths:

| Sheet | Key column | Matched against main-sheet column |
| --- | --- | --- |
| `person_to_area` | `email` | `email` |
| `department_to_area` | `department_name` | `department_name` |
| `fullpath_to_area` | `full_path` | `fullpath` |

Each also has an `area_path` column. A mapping sheet in the input is used as is; one
missing from the input is taken from `--mapping MAPPING.xlsx` (whose other sheets
are ignored), and otherwise created empty with its two columns so it can be filled
in for the next run. A missing key or `area_path` column is added blank.

## Title classification

`classified_title` is added to the main sheet. If the sheet already has that column
it is kept unchanged, so manual corrections survive a rerun. A title is lower-cased
and trimmed, then the first rule whose pattern occurs in it gives the class:

| Pattern | Class |
| --- | --- |
| `lead developer` | lead developer |
| `research developer` | research developer |
| `testing engineer`, `test engineer` | testing engineer |
| `engineer in test` | sdet |
| `security analyst` | security analyst |
| `system analyst` | analyst |
| `head of` | head |
| `team lead` | team lead |
| `architect` | architect |
| `manager` | manager |
| `developer` | developer |

A title matching no rule keeps its original text (so it shows up as its own column
in the pivot); a blank or non-text title is `Unknown`. Seniority is dropped:
`Junior`, `Middle` and `Senior Developer` are all `developer`.

## AreaPath mapping

`area_path` on the main sheet is recomputed on every run. For each row the mappings
are tried in priority order person, department, fullpath; the first whose key equals
the row's value wins. Keys and values are compared as text, trimmed and
lower-cased; blank values never match. Mapping rows with a blank key or AreaPath
are ignored, and a repeated key keeps its last row. A mapping whose main-sheet
column is absent is skipped with a warning; with none present, `area_path` is left
as it is (and the pivot needs an existing `area_path` column). The log reports matches per mapping and rows with
no match; rows left without an AreaPath or with a blank class are logged as
warnings.

## Headcount pivot

`dev_per_area` has one row per AreaPath and one column per class, with the count of
non-blank `email` values. Only approved rows count when the main sheet has an
`approved` column: `TRUE`, `1` (as a boolean, number or the text `TRUE`, `True`,
`true`, `1`); without the column every row counts. Rows without an AreaPath are not
counted, and a person without an email is not counted although their AreaPath
appears. With no countable rows the sheet is empty. An existing `dev_per_area` sheet
is replaced.

## Output

`OUTPUT.xlsx` contains, in order: the main sheet, `dev_per_area`, the three mapping
sheets in priority order, then every other input sheet as read. Values are written,
not formulas: text that starts with `=` stays text. Column widths fit their content
up to 50 characters. The file is written to a temporary sibling and renamed into
place, so a failed run leaves an existing output unchanged; missing parent
directories are created.

An existing output is replaced only with `--force` or after answering `y` at the
prompt; when the standard input is not a terminal the run fails instead of
prompting. The output may not be the input or mapping file. `--dry-run` reads the
input, logs its sheet, columns, missing mapping columns, five sample
classifications and the approved count, and writes nothing.

Logging goes to standard error: `-v` adds debug messages, `-q` keeps warnings and
errors only (`-v` wins when both are given).

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Output written, dry run done, or overwrite declined at the prompt |
| 1 | Missing or unreadable input or mapping file, no `corporatetitle` sheet, refused overwrite, or write failure; nothing written |

## Changes from the script

- A file whose mapping sheet was created empty got column widths of NaN, which
  openpyxl and Excel cannot read back; empty columns now fit their heading.
- With pandas 3 the script failed while saving any sheet with a blank cell (its width
  calculation called `len` on NaN), leaving a partial file; widths now ignore blanks
  and the output is published atomically.
- A blank `email`, `department_name` or `fullpath` matched a mapping key spelled
  `nan`; blanks now never match.
- The dry run counted approved rows with `approved == True` only; it now uses the
  pivot's rule.
- Overwriting no longer blocks on a prompt without a terminal, and the output may not
  replace an input.
- Text cells openpyxl cannot store (control characters) are cleaned rather than
  failing the write.

## Verification

Tests in `tests/team_roster/` cover every classification rule and its priority,
blank and non-text titles, the person → department → fullpath priority with
case- and space-insensitive keys, input mapping sheets winning over `--mapping`,
created empty sheets, kept `classified_title`, the approved rule, pivot counting
(unapproved, unmapped and email-less people), the blank-versus-`nan` key, sheet
order, readable widths, formula-like text, overwrite protection (forced,
non-interactive and prompted), refusing to replace inputs, missing inputs, dry run,
and that inputs are not modified. The output was compared with the original script's
on synthetic data under pandas 2.2.
