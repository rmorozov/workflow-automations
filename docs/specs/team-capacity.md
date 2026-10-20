# Team capacity specification, v1

Status: implemented. Entry points: `team-capacity` and `team-capacity-calibrate`,
`python scripts/team_capacity.py` and `python scripts/team_capacity_calibrate.py`, or
`python -m workflow_automations.team_capacity` and `…team_capacity_calibrate`.
Install the `team-capacity` extra (pandas, openpyxl). Design notes: [Team capacity
model design](https://claude.ai/code/artifact/ff3e10f9-39d4-4a50-935e-c54f795d2ea5).

## Goal

[`ado-triage`](ado-triage.md) compares each AreaPath's remaining change-request work
with a `dev_per_area_capacity` sheet of hours per month. That sheet used to be
filled in by hand from [`team-roster`](team-roster.md)'s headcount. `team-capacity`
computes it from person rows and a working calendar. Like a payslip, working days
are the gross; holidays and booked leave are unpaid days; rituals are fixed
deductions; expected illness, unbooked leave, bug fixing and interrupts are
percentages of what is left. The net is the time that can buy planned work. The tool
is local and deterministic.

### Limits

The output estimates developer-effort availability per AreaPath. It is a capacity
triage worksheet under validation, not a delivery commitment: it does not show that
testing, architecture, integration or acceptance capacity exists, or that work is
ready and unblocked. The defaults (rituals, 7 sick days, 50% bug-fix quota, 12% focus
loss) are starting hypotheses, not measured constants, and the bug-fix quota and
focus loss dominate the result. Net hours compare with change-request estimates
only if those estimates exclude what the model deducts (see [What change-request
estimates contain](#what-change-request-estimates-contain)). Open work on these
limits: the measurement contract (CP-008), availability versus productivity
(CP-009), calibration evidence (CP-010, CP-011), leave balances (CP-012) and
sensitivity (CP-013), plus demand quality in triage (CP-014), in `docs/backlog/`.

```bash
team-capacity --template CONFIG.xlsx [--overwrite]
team-capacity --roster ROSTER.xlsx --config CONFIG.xlsx --start-month YYYY-MM \
  [--months 3] --output OUTPUT.xlsx [--overwrite] [--allow-problems] [-v]
```

## Roster

The roster sheet is the first sheet with `email` and `classified_title` columns,
normally `team-roster`'s main sheet. With an `approved` column, only approved rows
count (`TRUE`, `1`, or the text `TRUE`, `True`, `true`, `1`). Other columns, all
optional:

| Column | Meaning | Default |
| --- | --- | --- |
| `area_path` | AreaPath the person's hours belong to | required; blank is a problem |
| `country` | Calendar to use | `area_countries`, then `default_country` |
| `allocation` | Share of the person's time in this AreaPath, 0 to 1 | the role group's |
| `start_date`, `end_date` | Joiners and leavers count only inside these dates | always employed |
| `hours_per_day` | Part-time day length; calendar hours scale with it | the country's |
| `vacation_days`, `sick_days` | The person's annual budgets | the country's |
| `ramp_up_share`, `ramp_up_months` | A joiner's reduced speed after `start_date` | settings |

A person split across AreaPaths has one row per AreaPath; their allocations should
add up to at most 1. Emails are compared lower-cased. AreaPaths are compared
case-insensitively, and the first spelling in the roster is used in the output,
because `ado-triage` matches them case-sensitively.

## Config workbook

`--template` writes every sheet with the agreed defaults and the UAE reference
calendar. Sheet and column names are case-insensitive; empty rows are ignored.

| Sheet | Columns | Notes |
| --- | --- | --- |
| `settings` | `key`, `value` | see below; a missing key keeps its default |
| `countries` | `country`, `workdays`, `hours_per_day`, `vacation_days`, `vacation_unit`, `sick_days` | blank cells take the settings value |
| `area_countries` | `area_path`, `country` | optional; the deepest matching AreaPath wins |
| `calendar` | `country`, `date`, `hours`, optional `name`, `status` | exception days: 0 a holiday, 6 or 7 a short day, 8 a working Saturday |
| `absences` | `email`, `from`, `to`, `type`, optional `hours` | `type` is `vacation`, `sick` or `other`; `hours` per day for part days |
| `role_groups` | `title_class`, `group`, `allocation`, `bug_fix`, `triage_pool` | required; one row per title class |
| `rituals` | `name`, `per`, `hours`, `groups` | `per` is `day` or `iteration`; `groups` a comma list or `all` |
| `rates` | `area_path`, `from`, `to`, optional `phase`, `bug_fix_quota`, `focus_loss` | optional |
| `iterations` | `name`, `start`, `end`, optional `phase` | optional; with rows it replaces the cadence |

Settings and their defaults:

| Key | Default | Meaning |
| --- | --- | --- |
| `iteration_start` | none | a Monday; required without an `iterations` sheet |
| `iteration_weeks` | 2 | iteration length in whole weeks |
| `absences_from` | the window start | absences are recorded from this date (see [Leave budgets](#leave-budgets)); it may not be after the window start |
| `default_country` | none | country of people without one |
| `workdays` | `Mon-Fri` | default week (`Sun-Thu` and `Mon,Tue,Thu` also work) |
| `hours_per_day` | 8 | default working day |
| `vacation_days` | 30 | paid leave per year |
| `vacation_unit` | `calendar` | `calendar` or `working` days |
| `sick_days` | 7 | working days of sick leave expected per year, not the legal ceiling |
| `bug_fix_quota` | 0.5 | for groups with `bug_fix` = yes |
| `focus_loss` | 0.12 | interrupts and context switching, every group |
| `ramp_up_share` | 0.5 | a joiner's share of full speed |
| `ramp_up_months` | 3 | months after `start_date` at that share |

The template's role groups: `developer` (developer, lead developer, research
developer) and `team lead` (allocation 0.5) are in the triage pool and pay the
bug-fix quota; `test` (testing engineer, sdet), `architect` and `analyst` (analyst,
security analyst) are reported but not in the pool; `management` (manager, head) has
allocation 0. Its rituals: daily scrum 0.5 h and code review 1 h per day; pre-planning,
demo and retro 1 h and 1 on 1 0.5 h per iteration, each for the groups listed in the
sheet.

## Calendar

Work is computed per person and day, then summed into months and iterations, because
weekly totals cannot be split between months correctly. A day's scheduled hours are
the country's `calendar` row for that date, else `hours_per_day` on a workday and 0
otherwise; a person's own `hours_per_day` scales them (a 4-hour part-timer works 3
hours on a 6-hour day). Booked absences remove their hours exactly: a whole day
without `hours`, else `hours` per day; together they never exceed the day.

An iteration is `iteration_weeks` whole weeks named by its start Monday, on a fixed
cadence from `iteration_start` (both directions). It ends on its last Friday but owns
the weekend after it, so a working Saturday falls in it. Holidays shorten it in
hours and never move its end. An `iterations` sheet replaces the cadence: each row
starts on a Monday, rows may not overlap, and days between rows are in no iteration.

## Leave budgets

Two annual budgets, in days, cover leave that is not booked yet: vacation (the
entitlement people spend during the year) and sick leave (the days people are
expected to take). For each person and calendar year:

1. The budget is prorated to the days the person is employed and to the part of the
   year whose absences are recorded, from `absences_from` (else the window start)
   to year end. Leave before that date counts as already taken.
2. The days of that type booked in the same part of the year are subtracted.
   Calendar-day vacation counts every calendar day of a booking, then converts the
   rest at the country's working-day ratio (30 × 5/7 ≈ 21.4 working days).
3. What is left is spread as an expected share over the person's unbooked working
   time from the window start to year end.

So a booking later in the year lowers the share instead of counting twice. If the
absences sheet holds the whole year's leave, set `absences_from` to January 1 to use
it. Booked leave over the full-year budget is a problem; the share is then 0, and the
booked days still count exactly.

## Net hours

For person p on an available day d of iteration i, with group g:

H = (h − r − R / A) × (1 − s − v) × (1 − b − φ) × a × u

| Symbol | Meaning |
| --- | --- |
| h | scheduled hours minus booked absences |
| r, R | g's per-day and per-iteration ritual hours |
| A | the person's available days in iteration i (a day is available when h > 0) |
| s, v | the sick and vacation shares of [Leave budgets](#leave-budgets); they add because one day is not off twice |
| b, φ | the bug-fix quota (0 for groups without `bug_fix`) and focus loss, from `rates` |
| a, u | allocation and ramp-up (`ramp_up_share` before `start_date` + `ramp_up_months`, else 1) |

Rituals never exceed the day's hours, and a person away for a whole iteration pays
none of its rituals. Bug fixing and focus loss are shares of the same time after
rituals and leave. Every hour component (gross, absence, ritual, illness, vacation,
bug fix, focus, net) is multiplied by a × u, so allocation 0.5 halves each one, and
gross minus the others equals net.

Worked example, one developer, 10 working days, 4% sick share, no leave left: 80
gross, 18.5 ritual, 2.46 illness, 29.52 bug fix, 7.08 focus and 22.44 net hours.

### Rates

A `rates` row sets `bug_fix_quota` and `focus_loss` for its AreaPath and every
AreaPath below it, from `from` to `to` (blank is open). For a day, the matching
rows are ordered by depth (deepest first), then by start (latest first); each rate
takes the first non-blank value, else the setting. Two rows with the same AreaPath
and start and overlapping dates are an error, and so is a bug-fix quota and focus
loss that add up to more than 1 on any day of a bug-fixing group, inherited values
included. `phase` is a label, for example
`stabilization`, and does not change the arithmetic.

## Output

One workbook, written to a temporary sibling and renamed into place:

| Sheet | Contents |
| --- | --- |
| `dev_per_area_capacity` | `AreaPath`, then one `YYYY-MM` column per window month: net hours of the triage-pool groups. Every AreaPath of a triage-pool person is listed, at 0 when nobody is available. This is the contract `ado-triage --capacity-file` reads. |
| `capacity_by_role` | `area_path`, `group`, `period_type` (`month` or `iteration`), `period`, `partial`, `people`, `fte`, then the eight hour components |
| `capacity_by_person` | the same per person, with `email`, `classified_title`, `country` |
| `calendar_weeks` | working days and hours per country and week, to check against official weekly norms |
| `problems` | `email`, `area_path`, `problem`, `detail` |

Iteration rows hold only days inside the window; `partial` marks an iteration whose
start or last Friday is outside the window. `people` counts those with hours in the
period; FTE is allocation × ramp-up averaged over the country's working days in the
period, counting only days the person is employed. Hours are full precision, shown with two decimals.

Problems are approved people with no email, no AreaPath or a title class without a
role group (they are not counted), allocations over 1 for one email, leave over the
full-year budget, a budget larger than the days left, a country year inside the
window with no calendar rows, and no triage-pool people at all (`ado-triage` cannot
read an empty capacity sheet). A ritual with blank `hours` or `groups` is an input
error. With any problem the output is still written and the
exit code is 3, unless `--allow-problems`.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Output or template written |
| 1 | Missing or unreadable input, invalid config or roster value, existing output without `--overwrite`, or an output that is an input; nothing written. Input errors are listed together, each with its sheet and row. |
| 2 | Invalid command line |
| 3 | Output written, but the `problems` sheet is not empty |

## UAE reference calendar

The template's `calendar` sheet holds UAE private-sector exception days for 2026 and
2027, with a `status` per row:

- 2026, announced: New Year (Jan 1), Eid al-Fitr (Mar 19–21), Arafat Day and Eid
  al-Adha (May 26–29), Hijri New Year (Jun 15), Prophet's Birthday (Aug 28, moved to a
  Friday). National Day (Dec 2–3) is expected and may be moved.
- 2027, projected: New Year (Jan 1), Eid al-Fitr (Mar 9–11), Arafat Day and Eid al-Adha
  (May 15–18), Hijri New Year (Jun 6), Prophet's Birthday (Aug 14), National Day
  (Dec 2–3). Hijri dates depend on the moon sighting.
- Ramadan working days are 6 hours, two hours shorter (MOHRE): Feb 19 to Mar 18, 2026,
  and, projected, Feb 8 to Mar 8, 2027.

The week is Monday to Friday, 8 hours. Update the rows when the official dates are
announced.

## Calibration

`team-capacity-calibrate` checks the rate that dominates the forecast against logged
work: the bug-fix quota and focus loss take 62% of the base, and ±10 points of bug-fix
quota move net hours by about ±26%, while the whole calendar moves them by a few
percent. It measures only the bug share of logged work. Focus loss cannot be
identified from logged hours and is taken from the config. Its suggestions are
exploratory: they rest on a snapshot's cumulative `CompletedWork` and today's roster
(see the limits below, CP-010 and CP-011).

```bash
team-capacity-calibrate --snapshot SNAPSHOT.json --roster ROSTER.xlsx --config CONFIG.xlsx \
  --output CALIBRATION.xlsx [--bug-types Bug] [--overwrite]
```

The snapshot is a saved [ADO snapshot](ado-snapshot.md) of past work (from
`ado-triage --save-snapshot` or `ado-graph export --save-snapshot`); nothing contacts
ADO. The config needs an `iterations` sheet: an item belongs to the iteration whose
`name` equals its `System.IterationPath` or that path's last segment. The model runs
over every configured iteration from the first to the last one an item is in, so an
iteration nobody logged work in is still reported.

Completed work is `Microsoft.VSTS.Scheduling.CompletedWork` of each snapshot item,
counted for the triage pool only: its AreaPath maps to the deepest AreaPath of a
triage-pool person, and its `System.AssignedTo` is a triage-pool person. An item is
bug work when its type is in `--bug-types` or its parent's type is (a task under a
bug). Work outside a configured iteration, outside the pool's AreaPaths, or assigned
to someone else is not counted; its hours per reason go to `problems`. The assignee's
email is the `uniqueName` or `mailAddress` that contains `@` (on-premise servers
often give `DOMAIN\user` as `uniqueName`), or the address in `Name <email>` text.

The `calibration` sheet has one row per AreaPath and iteration: predicted base,
bug-fix and net hours; completed bug, other and total hours; the configured and
observed bug share; the suggested quota; and `logged_to_predicted`, completed other
hours ÷ predicted net hours. That ratio relates logged effort to predicted capacity;
it is not estimate accuracy, which would need each item's estimate and actual effort,
and a low value can mean under-logging, blocked work, no ready work or duties the
model omits. The observed share is bug hours ÷ logged hours. Assuming nobody logs
focus loss, logged time is the base less focus loss, and the suggested quota
is the observed share × (1 − focus loss). Focus loss here is the iteration's predicted
focus hours ÷ predicted base, so a rate changing inside the iteration counts by day.
An iteration without logged work has
status `no logged work`, blank ratios and a `problems` row; it is never counted as
zero.

`suggested_rates` has one `rates` row per AreaPath and run of consecutive iterations
with the same `phase`; an iteration without usable data ends a run, so no row spans
dates without evidence. Each row has its quota weighted by logged hours, `focus_loss` blank (so it
inherits) and a `note`. It loads as a valid `rates` sheet, but its dates are the past
iterations': to forecast with it, change `from` and `to` to the coming phases.
Nothing changes the config automatically.

Limits: `CompletedWork` is cumulative per item, so an item worked over several
iterations counts in the one it is in now; parents are looked up one level up.

### What change-request estimates contain

The model deducts code review, rituals, bug fixing and interrupts from capacity, so
change-request estimates (`RemainingWork`, and the completed work compared with net
hours) must count development work only. If a team's estimates already include
code review, testing or bug fixing, those hours are deducted twice and the team looks
overloaded; agree the rule with the teams before relying on calibration, and set that
team's rates accordingly.

## Verification

Tests in `tests/team_capacity/` cover holidays and absences removing exactly their
days, short days keeping their rituals, a working Saturday, part-day and part-time
hours, the New Year week counted in December, 30 calendar days of leave becoming 21.4
working days over the year, booked sick days reducing only the sick budget, a later
booking lowering the share without double counting, over-booking, `absences_from`,
joiners and leavers, iterations crossing a month, the `iterations` sheet,
`area_countries`, every validation failure naming its row, the worked example,
rituals for an absent person and spread over available days, allocation, unmapped
title classes, unapproved people, `rates` subtrees and depth, the bug-fix flag,
ramp-up, over-allocation, the output sheets with per-role sums equal to per-person
sums, `ado-triage`'s own `load_capacity` reading the output, overwrite and
input-protection rules, problem exit codes, and the template's UAE calendar.
`tests/team_capacity_calibrate/` covers bug shares and logged-to-predicted ratios for two teams over
three iterations, tasks under bugs, iterations without logged work, uncounted work,
phase-split suggestions that load as a valid `rates` sheet, and the CLI.
