# Gerrit parents specification, v1

Status: implemented initial contract. Entry points: `gerrit-parents`,
`python scripts/gerrit_parents.py`, or `python -m workflow_automations.gerrit_parents`.

## Goal

A sheet lists Gerrit repositories (usually leaf projects). A Markdown outline shows
the Gerrit project tree. The tool finds each repository's closest parent, the
project it inherits from directly, and writes the result as a workbook, an outline
grouped by parent, or both. It is offline and deterministic; it uses openpyxl and
PyYAML (the `outline` extra).

Example sheet:

| repos |
| --- |
| repo1 |
| repo2 |

Example tree:

```markdown
* all-projects
   * some-projects
      * repo1
   * other-projects
      * repo3
      * nested-other-projects
         * repo2
```

`gerrit-parents --input repos.xlsx --tree tree.md` writes:

```markdown
- some-projects
  - repo1
- nested-other-projects
  - repo2
```

## Input

`--input` is an `.xlsx` workbook, opened read-only. `--sheet` names the sheet (default:
the first). Row 1 holds headings; `--column` names the repository column (default:
column A, which needs a heading). Cells are read as `xlsx-outline` reads them: text
trimmed, formulas as the values Excel last saved. Blank cells are skipped.

`--tree` is a UTF-8 Markdown outline in the syntax `xlsx-unfold` reads: nested `-`,
`*`, `+` or numbered list items, `#` headings, or headings with lists beneath them.
Leading front matter is ignored. `--title` treats the first heading as a document
title rather than a project. Markdown backslash escapes are removed (`my\_repo` is
`my_repo`) unless `--raw` is given. Any other non-blank line is an error.

## Matching

- A repository matches a tree item with exactly the same name (case-sensitive). Names
  may be Gerrit paths such as `platform/tools/repo`.
- Only when no item has exactly that name, the last path segment is compared: a bare
  name (`repo`) matches items whose path ends in it (`platform/tools/repo`), and a
  path matches a bare item named after its last segment. Two different full paths
  never match each other. So a full-path entry beats a same-named repository
  elsewhere in the tree, and a bare name prefers a bare item. The summary lists names
  matched this way with the tree item each one matched.
- Its parent is the item directly above it; its path is every ancestor from the root.
- The same name listed twice under the same parent counts once, at its first position;
  it has children if any of its listings has them.
- A name not in the tree is `not found`; a name under two or more different parents,
  or whose last segment matches two or more items, is `ambiguous` (a candidate matched
  by last segment shows its tree name in parentheses after its path); a top-level item
  is `no parent`. None of these is guessed.
- An item that has children of its own still matches; the summary lists such names,
  since a leaf repository was expected.

## Output

`--output` writes a new `.xlsx` workbook: the sheet's columns as text, plus
`Parent`, `Path` (ancestors joined by ` / `) and `Status`. For an ambiguous name,
`Path` lists every candidate path, separated by `; `. Rows keep sheet order, blank
repository rows included. The sheet keeps its name unless `--output-sheet` is given.
A sheet that already has a `Parent`, `Path` or `Status` column is rejected.

`--outline` writes Markdown: each parent once, repositories beneath it, duplicates
removed. Parents follow their own first position in the tree, and repositories
their positions within it. When two parents in
different branches have the same name, both are labelled with their full path.
Unresolved names follow in sections `(top level, no parent)`, `(under more than one
parent)` (with candidate paths) and `(not found in tree)`. Text is Markdown-escaped
unless `--raw` is given. Without `--output` or `--outline` the outline goes to
standard output.

Outputs must be new files: existing ones need `--overwrite`, and neither may be the
input workbook or tree. Inputs are never modified. A summary goes to standard error.

## Exit codes

- `0`: every repository matched a parent.
- `2`: validation error (options, sheet, column, tree syntax, existing output).
- `3`: file could not be read or written.
- `4`: outputs written, but some names were not found, ambiguous or top-level.
