# Frisket grid and analytics query-plan checkpoint

Status: diagnostic complete at production base
`1b8cde5c843aedee3fb1702db413a91f77c9231a`; no production code changed.

## Grounding

The authoritative 500k result is
`project-qualification-500k-final.json` with SHA-256
`81f9563d1fc6400e073f5682d780a8b5652cbaf8be11ebb17eff737974115353`.
Its public/runtime workload reported these warm-cache medians:

- selective 0.1% grid filter: 4,107.52 ms (4,581.10 ms maximum);
- category 10% grid filter: 1,339.94 ms;
- amount/date/category sorted pages: 2,635.51--2,787.07 ms;
- broad grouped analytics: 16,854.46 ms;
- filtered 10% analytics: 8,991.14 ms.

After checkpoint and reopen, broad analytics took 20,644.81 ms. The workload
runner source at archived checkout `c0b58475f22ae271d48d82290f722dfdae3822e7`
is byte-identical to runner commit
`c4a4c25903086ef29dcd726a7e7180a866224cf9` for both
`project_workloads.py` and `project_benchmark.py`. This runner commit is not an
ancestor of the production base: `git merge-base --is-ancestor` returned 1 and
the merge base is `33fee60a2b7dba26883e7c05dedae73b51509c3d`.

The bounded diagnostic uses a real `Project`, the same five relevant typed
columns and skewed categories/valid-invalid-missing amount states, and the
public `resolve_sheet_filter_rows` and `evaluate_analytics` entry points. It
ran under networkless `bwrap`, an empty environment, read-only source and
canonical venv, a 1 GiB address-space limit, and a 500 MiB scratch guard. At
10,000 rows it used 6,525,342 scratch bytes and 130,625,536 peak RSS bytes.
The machine-readable result is `results-10k.json` (SHA-256
`24116115713b004347c3c2f0c57f5fd2cfba78c1711bdde52426fff73ef6a67d`).
That run overlapped another diagnostic, so its times are attribution evidence
only, not comparative performance claims.

## Measured cause

Grid filtering compiles a live value as a correlated scalar subquery:

```sql
(SELECT CASE WHEN live.validity='valid' THEN live.value END
 FROM current_cells live
 WHERE live.column_id=? AND live.row_id=r.id)
```

The selective integer predicate is outside that subquery. `EXPLAIN QUERY PLAN`
therefore shows a scan of every visible `rows` entry and one primary-key probe
of `current_cells` per row. Both the exact `COUNT(*)` and the 50-row page run
that population scan. A 0.1% result still performs two full row scans and up to
two live-cell probes per source row. The 10k standalone statements took 21.82
ms for the count and 21.49 ms for the page; the official 500k public call took
4.11 s median.

Grid sorting also scans all visible rows. Its NULL-placement key and scalar
sort key each contain the same correlated subquery, so the amount sort plan
shows two `current_cells` primary-key probes per row and a temporary b-tree for
ordering. The exact count is a separate, cheap row scan. At 10k the page
statement took 15.71 ms; the official 500k sorted-page medians were about 2.79
s. A direct join could remove the duplicate lookup, but it would still sort all
rows and does not solve selective filters without a value access path.

Analytics has larger repeated work. For the broad grouped workload, the main
SQL statement independently expands `prepared` for aggregate metrics, median,
and denominators. Its plan contains three `rows` searches, six joined
`current_cells` searches, two temporary group b-trees and three temporary order
b-trees. `_evaluate` then executes a denominator-only statement with one more
population scan, followed by `_assert_query_finite`, whose statement repeats
the three-scan aggregate/median/denominator graph. That is seven scans of the
same filtered population per public analytics call. The 10k standalone
attribution was 101.68 ms main + 6.15 ms denominator + 77.37 ms finite check.

Filtered analytics repeats the filter too. The 10% plan contains the correlated
category predicate in each of those seven population scans. Its 10k standalone
attribution was 27.18 ms main + 7.77 ms denominator + 19.96 ms finite check.
The repeated scans, JSON extraction, grouping and median ordering explain why
filtering the result to 10% only halves the official 500k latency rather than
approaching one tenth.

## Architecture and proportionality checkpoint

`rows` remains the visible row and stable position authority.
`current_cells` remains the rebuildable authority for the current typed value,
validity and origin. `querysets.py` continues to own the closed filter/sort
semantics. Analytics continues to run inside one read snapshot and to preserve
valid/invalid/missing groups, typed scalar equality, sheet/row/file scope,
stable ordering, denominators before having/pagination, and numeric overflow
checks across all groups, including groups omitted by having, NULL ranking,
limit or offset.

The realistic failures are a stale current value after edit/result
publication, a changed null/invalid ordering, a filter/sort type coercion
change, loss of exact denominator counts on an empty page, or failure to reject
a non-finite metric in a group outside the returned page. These are covered at
the public runtime boundary by existing current-cell/filter tests and
`tests/server/test_project_qa_analytics.py`; the empty-having denominator and
overflow cases are specifically present there.

The smallest candidate is to make the existing `prepared` CTE explicitly
`AS MATERIALIZED` and compare it with the current plan on the same actual
Project workload. Within each statement this should replace three population
scans with one materialization plus reads of the bounded involved-column
projection. It preserves the query vocabulary and result semantics and adds no
schema, generic planner, ORM, cache or second authority. It can be rejected
without production change if temporary storage, long text groups, or elapsed
time regress.

The exclusive 10k comparison preserved every returned SQL row. It reduced the
main and finite-check plans from three population scans/six current-cell probes
to one scan/two probes. Broad statement time was effectively neutral overall:
164.93 ms became 160.94 ms (2.4%), with a 15.5% faster main statement offset by
a slower denominator statement and neutral finite check. Filtered statement
time fell from 54.67 ms to 31.33 ms (42.7%); its main statement fell 58.0% and
finite check 41.6%. The result is `results-materialized-10k.json`, SHA-256
`717762f09f76328d6ab44bea57df1f13e93fa9669f55b2d801a86239911c19f8`.
The planner hint is therefore a useful filtered-query improvement but does not
resolve the broad grouped case by itself.

The next bounded comparison consolidates statement-level metadata without a
new abstraction. A normal nonempty page carries the already-selected
denominator fields and a validation CTE's global finite count in the same SQL
statement. Only an empty result after having/offset uses a metadata fallback.
The finite count still traverses every group before having/rank filtering. This
keeps the common path to one statement while preserving empty-page metadata.

That comparison passed. Against the exclusive materialized-only run, the 10k
public-call median fell from 169.25 ms to 73.78 ms for broad grouped analytics
(56.4%) and from 59.79 ms to 14.13 ms for filtered analytics (76.4%). Each
common-path call now has one heavy SQL statement whose plan contains one source
row scan, two current-cell joins and one prepared materialization. Scratch was
unchanged at 6,525,342 bytes and peak RSS was 131,018,752 bytes. The result is
`results-consolidated-10k.json`, SHA-256
`95a12f0c59cf04c0155b2cf0cd6bc8c538c2bcd4c33b7ee807f2ea635823b8e9`.

The empty-page fallback is deliberately retained instead of adding a UNION
sentinel row and a second ordering vocabulary. Existing having-empty behavior
and a new off-end page assertion prove the fallback still returns the full row
count and denominator. The existing integer-overflow test proves the request
continues to fail as a whole. Metric-ranked excluded-NULL counting remains its
existing separate query; that uncommon shape was not part of the measured
qualification workload and is outside this bounded patch.

No grid compiler rewrite or new current-cell value index is proposed from this
lane yet. A direct sort join removes one lookup but leaves the full sort. A
selective value index also needs a query join/reversal and separate treatment
of integer, real, text/date, validity, JSON and collation semantics. Its write
and bundle-size cost belongs in the physical-layout comparison before adding
it to every project.

## Remaining grid filter and sort opportunity

The rowid packing candidate does not add a scalar access path. It preserves a
unique `current_cells(column_id,row_id)` index and an ordered
`current_cells(row_id,column_id)` index; both locate cell identities. In the
probe, the 0.1% integer filter still starts with `SEARCH r USING INDEX
idx_rows_sheet`, then performs one correlated exact-cell lookup for every row
in both count and page statements. The amount sort likewise starts from rows,
performs two exact-cell lookups per row for the repeated NULL and scalar keys,
and ends with `USE TEMP B-TREE FOR ORDER BY`. Changing the table from
`WITHOUT ROWID` improves packing but does not alter those access choices.

A join to one `current_cells` alias per involved column is the realistic small
next experiment. It can remove the sort's duplicate lookup and reuse a value
already joined for both a filter and a sort. The identity indexes support that
join, but cannot seek a decoded value, so selective filters still scan a
column or the sheet and scalar sorts still sort the surviving rows. This is a
constant-factor opportunity, not the asymptotic fix.

An index on raw `value` would not preserve the current contract. Values are
JSON text, while integer ranges use the exact `frisket_signed_int64(value)`
decoder, real ranges accept only JSON integer/real values and cast to REAL,
ordinary equality casts `json_extract(value,'$')` to text, dates pass through
UTC calendar normalization, and text-like sorts use `COLLATE NOCASE`.
Ordinary filters and sorts expose only `valid` cells: absent, `missing`,
`invalid`, explicit SQL NULL and valid JSON null all become a NULL ordering
key, which is placed after non-NULL values in either direction and then tied
by row position and id. `neq` deliberately includes that NULL population.
Group locators additionally distinguish missing from invalid. A raw JSON-text
order or a valid-only index would change these results.

A targeted expression index could help one exact operator/type only after the
query is shaped as an indexable join. It would not cover leading-wildcard
`contains`, JSON list/entity membership, geographic extraction, date-part
operators, runtime operators, complements such as `neq`, or multiple filters
on different columns. General selective filter and ordered-page improvement
therefore needs a maintained typed scalar projection (or equivalent typed
expression paths) carrying column, validity/order class, decoded value and row
identity with the exact collation/coercion contract. That is a separate
write-amplification and migration decision; adding it speculatively would
undo part of the measured packing win. The proportionate next step is to
measure the one-alias join on the retained workload, and pursue typed
projection only for demonstrated filter/sort demand.

## RED and validation plan

The checked-in opt-in probe is the performance RED: the old production code
shows seven analytics population scans and the measured 500k latency. A brittle
wall-clock threshold or test that polices SQL source text would add more risk
than the planner hint, so it should not enter the routine suite.

Before any production edit, rerun the 10k probe exclusively with both ordinary
and `prepared AS MATERIALIZED` SQL. Require byte-for-byte equal statement rows,
fewer population scans in `EXPLAIN QUERY PLAN`, scratch below 500 MiB, RSS below
1 GiB, and improved broad and filtered statement totals. If it passes, change
only `_base_ctes` and run `tests/server/test_project_qa_analytics.py` plus the
current-cell/filter integration tests. The public runtime assertions, rather
than query text, remain the semantic GREEN proof. Then rerun the bounded actual
Project probe to confirm the intended planner change.

The probe is `scripts/storage_query_probe.py` on branch
`research/storage-queries-20261002`. It owns only disposable bundles under its
explicit work root; both diagnostic bundles were removed after the result JSON
was preserved.
