# XLSX translation specification, v1

Status: implemented initial contract. Entry points: `xlsx-translate`,
`python scripts/xlsx_translate.py`, or `python -m workflow_automations.xlsx_translation`.

## Goal

Two offline modes: extract unique cell texts from an XLSX data sheet, then apply
external translations to a new sheet using IDs. Pandas handles table processing,
deduplication/counting, CSV IO, and the ID join; openpyxl supplies cell metadata and
Excel IO. There are no LLM calls, credentials, or network operations in the tool.

Original input is XLSX. Mapping input is a batch of CSV files, XLSX workbooks, or a
mixture. Each invocation applies one target language. Separate extractions can
target different languages; reusable dictionaries across languages are deferred.

## Input and matching

The selected sheet is a rectangular table beginning at A1. Row 1 contains unique,
nonempty literal string headings. The rectangle ends at the last nonblank cell,
including formulas; styling outside it does not extend the rectangle. Internal
blank rows/columns are retained. Merged cells intersecting it are rejected.

`--columns` selects exact heading names, including headings containing spaces when
shell-quoted. `--column-indices` selects 1-based physical positions. They are
mutually exclusive; absence selects all columns.

Actual string cells containing non-whitespace text are eligible. Whole cells are
translation units. No normalization, trimming, case folding, or sentence splitting
occurs. Text `NA`, `NULL`, `0012`, and formula-like literal strings is preserved.
Numeric, boolean, date/time, blank, whitespace-only, error, and formula cells are
excluded. Array/data-table formulas are unsupported and rejected. Ordinary formula
expressions are copied unchanged and not recalculated.

Default dictionary key is `(physical column, kind, exact text)`. Kind is `header`
or `cell`. `--dedupe-scope global` uses `(kind, exact text)` across selected columns.
Global sharing is explicit because identical words can mean different things in
different columns. Row-dependent translations of identical strings within a column
are outside v1. Headings are translated only with `--translate-headings`.

## ID-based data model

Three distinct objects form the translation chain:

1. **Source dictionary:** `text_id,source_text`.
2. **Translation mapping:** `text_id,translated_text_<language>`.
3. **Cell references:** original Excel row/column coordinates and `text_id`, stored
   in the manifest, not sent to the translator.

IDs have the form `<bundle>_c0002_c000001`, or `<bundle>_global_c000001` for global
mode. Header IDs have suffix `_h000001`. The bundle component is a randomly
generated 16-hex-character namespace (64 bits); this is accidental-mixing detection,
not authentication. Within a scope/kind, assign sequential IDs in first-occurrence
order. IDs are immutable within the bundle and not reusable across extractions.

Example (abbreviated namespace for readability):

| Source dictionary text_id | source_text |
| --- | --- |
| bundle_c0001_c000001 | Открыто |
| bundle_c0001_c000002 | Закрыто |

| Translation mapping text_id | translated_text_en |
| --- | --- |
| bundle_c0001_c000001 | Open |
| bundle_c0001_c000002 | Closed |

Application joins **original cell → ID → target text**. Returned translation
files never need to repeat source text. Mapping row/file order is irrelevant.

## Extraction outputs

`--formats csv xlsx` is the default; either format may be selected alone.

| Output | Contents |
| --- | --- |
| `sources.xlsx` | One source dictionary sheet per selected column, or one `global` sheet |
| `translations.<language>.xlsx` | Corresponding two-column translation templates |
| `sources/c0002.csv` | Per-column source dictionary (global mode uses `global.csv`) |
| `translations/c0002.csv` | Corresponding blank translation template |
| `batches/c0002.part001.csv` | Optional source dictionary chunks |
| `manifest.json` | Schema, settings, dictionary, cell references, source fingerprint, batch inventory |
| `summary.json` | Unique/eligible counts, character totals, per-column counts |
| `prompt.txt` | Languages, column context, and response instructions |
| `agent_prompt.md` | Agent-mode instructions and work-unit checklist; written only when CSV units exist |

Column IDs are based on physical positions, not arbitrary heading text; filenames
and sheet names remain safe. Selected columns with no eligible text get empty
two-column dictionaries/templates. Global mode has one editable dictionary; the
manifest retains references to every original column. CSV and XLSX exports share
the same IDs. The manifest is required regardless of selected export format.

CSV is UTF-8 with BOM. `--delimiter` defaults to comma and accepts one non-quote,
non-newline, non-NUL character. CSV quotes delimiters, quotes, and embedded newlines.
Literal NA-like strings are never parsed as missing values. Excel dictionary and
translation fields are explicitly written as text, including leading `=`. CSV
quoting cannot control Excel's own formula interpretation when a CSV is opened in
Excel; XLSX templates are recommended for that editing workflow.

## LLM batches

Optional `--batch-max-rows` and `--batch-max-bytes` are positive limits. Both apply
when provided. Split within each dictionary without changing IDs or splitting cell
texts. Each ID occurs in exactly one batch. Byte limits cover actual serialized
source CSV bytes, including header, BOM and quoting. If one record exceeds the
limit, extraction fails without publishing the bundle and identifies that ID.

These are input-size limits, not exact token counts; leave room for instructions
and the generated reply. The prompt asks for every ID once and only the target
two-column schema, proper CSV quoting, no prose/code fences, and preservation of
placeholders/markup. It tells the model to treat source text as data. Placeholder
correctness and semantic translation quality remain human review responsibilities.

## Agent mode

`agent_prompt.md` drives an LLM agent with file access and a small context window.
Work units are the batches when batching is enabled, otherwise the per-column (or
global) source CSVs; an XLSX-only extraction without batches has no units and no
agent prompt. The prompt states the absolute bundle directory and uses paths
relative to it. It contains the same reply rules and column context as `prompt.txt`,
the delimiter, and a checklist of each unit, its row count and its reply path
`replies/<unit file name>`. Steps: process units in order, reading only the current
unit; write a two-column reply with one row per source row; verify row count and
IDs; skip a unit whose reply already has the expected row count, so runs resume.
Finally run `apply` with the original workbook's absolute path, the bundle manifest,
every reply path listed explicitly, and output `translated.<language>.xlsx` in the
bundle. `apply` validation is unchanged and remains the authority on completeness.

## Mapping validation and application

`apply` requires the original workbook, manifest, and explicit mapping file paths.
Directory/glob expansion and configuration files are deferred; the script never
automatically loads source dictionary files as translation replies.

CSV and every sheet of an input XLSX mapping must contain exactly the two headers
`text_id,translated_text_<manifest target language>` in that order. Extra metadata
or source-text columns/sheets are rejected. Target language tags are constrained to
letters followed by letters/digits/hyphens/underscores, up to 35 characters.

CSV replies may contain blank or whitespace-only lines and one Markdown code fence
line (such as a line of three backticks followed by `csv`) at the start and end; these
carry no IDs and are skipped. Any other CSV problem fails with the file, line number
and likely cause: a different header (with hints for spaces around the delimiter or a
different delimiter), or a row without exactly two fields (usually an unquoted
delimiter in a translation).

Validation before output:

- Supported manifest schema, valid dictionary IDs, references and occurrence counts.
- Selected original sheet matches a semantic fingerprint; reference cells match
  their authoritative source dictionary values.
- Every supplied ID exists in the manifest. Unknown or foreign-bundle IDs fail.
- XLSX mapping fields are literal strings, not numbers, formulas, or errors.
- Nonempty translations fit Excel's UTF-16 cell limit and contain no invalid
  control characters. Overlong values are rejected, never truncated.
- Identical duplicate resolved replies are accepted and reported; different replies
  for one ID fail regardless of file order. Blank/whitespace-only replies are
  unresolved and do not override a resolved reply.

To intentionally retain text, supply the exact original text as its translation.
Intentionally deleting a cell's content is outside v1. No separate action/status
column is required.

Default `--missing error` requires every ID. Missing values generate a failure
report and no workbook. `--missing keep` retains original unresolved cells, writes
a partial report/workbook, and returns exit code 4. Unknown/conflicting/invalid
records always fail. Corrections to a resolved value should replace its reply,
rather than supplying both conflicting versions. Incremental completion uses the
same manifest and accumulated reply files, without regenerating extraction IDs.

The semantic fingerprint is SHA-256 of a canonical JSON representation of the
entire selected table with types, dimensions, positions, headings and values.
It distinguishes text, numeric values, booleans, dates/times, blanks, errors and
formula expressions. Integral numeric floats and integers are equivalent as Excel
numbers. Formatting changes or changes outside the source table are permitted.
Changed values, headings, row order or bounds require a new extraction. The hash
detects accidental changes, not hostile manifest edits.

Extraction also stores `fast_check`, a SHA-256 of the exact workbook bytes and the
exact manifest content. When both are unchanged at apply time, the fingerprint and
per-cell reference checks are skipped because they would repeat extraction's own
reading; any byte change in either file, or a manifest without `fast_check`, takes
the full semantic check. The report's `source_check` is `unchanged` or `cells`.

## Workbook and report output

The output is the original workbook plus a copy of the selected worksheet in which
only referenced cells hold resolved target strings. Reference IDs map to
translations; no text-based join with returned replies occurs.

By default the copy is made at the XML level (report `writer`: `xml`). Every member
of the original XLSX package is copied unchanged, so original sheets keep full
fidelity, including charts and drawings. The copied sheet XML has translated cells
rewritten as inline strings with their style kept. Parts a second sheet cannot share
(drawings, comments, tables, pictures, controls, OLE objects, printer settings) are
dropped from the copy, and tab selection and code names are cleared. External
hyperlinks are kept. If the copy would still reference other sheet parts, or a
translated cell cannot be located (for example a producer that does not write the
cell reference as the first attribute), the tool falls back to openpyxl (`writer`:
`openpyxl`), which loads, copies and saves the whole workbook. New sheet name defaults to `<original>_<language>`;
`--output-sheet` sets it explicitly. Invalid or case-insensitively colliding names,
or translated heading collisions, fail before workbook publication.

Original rows, columns, repeated occurrences, ordinary formulas and nontext cell
types remain logically intact. The openpyxl fallback retains supported formatting
on a best-effort basis. Drawings, charts and tables in the translated copy, and
formula-reference rewriting, are outside the contract. Formula expressions may
continue referencing original sheets, and no cached recalculated values are promised.

`<output-stem>.report.json` records bundle/language, resolved IDs, missing IDs/cells,
translated cell count, redundant replies, complete/partial/failed status, the source
check and the writer used.
Same-text translations count as resolved/translated. Validation errors other than
missing translations print diagnostics but do not promise a report.

Extraction refuses an existing destination, even empty; a complete bundle is staged
and renamed into place. Apply never overwrites any input path. Existing output or
report requires `--overwrite`. A temporary XLSX is reopened before atomic replacement;
failed writes preserve the existing workbook. Report publication precedes workbook
publication; a report alone does not indicate successful output, and multiple
files are not one transaction. Inputs are never modified.

## CLI and exit codes

See [README](../../README.md) for a complete extraction/application example.
`xlsx-translate extract --help` and `xlsx-translate apply --help` list all options.

| Exit | Meaning |
| --- | --- |
| 0 | Complete extraction or translation |
| 2 | Configuration, source layout, manifest, or mapping validation failure |
| 3 | File IO failure |
| 4 | Partial translation produced with `--missing keep` |

## Verification and future work

Tests use generated, synthetic real workbooks to cover repeated values, per-column
versus global scope, XLSX/CSV/mixed replies, reply reordering, NA/leading-zero text,
embedded CSV punctuation/newlines, blanks/numbers/dates/booleans/formulas, literal
formula-like text, strict/partial completeness, foreign IDs, conflicts, batch
coverage/limits, headings, changed source, manifest validation, safe overwrite, an
agent-mode round trip that follows the prompt's checklist and apply command, CSV
reply diagnostics, XML and openpyxl writers producing identical cells, Excel-style
shared-string and prefixed-namespace packages, and the unchanged-source shortcut.

V1 reads the selected worksheet and manifest into memory, streaming the sheet once
in openpyxl's read-only mode. No fixed memory ceiling is promised. Measured on a
synthetic 50,000-row, 10-column sheet with two translated columns: apply took 21 s
before this design, 2.8 s with an unchanged source and about 10 s when the source
must be re-verified; extraction about 7 s.

Deferred: JSON configuration, reusable translation memories, one bundle with
multiple languages, pending-only batch regeneration, row-context keys, token-aware
batching, source CSV tables, placeholder validators, full Excel fidelity and API
translation. See [backlog](../backlog.md).
