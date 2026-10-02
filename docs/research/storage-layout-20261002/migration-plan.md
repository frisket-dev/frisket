# 500k rowid migration qualification: resource plan

**Execution update:** the 500k migration and separately admitted main/search
compaction have now passed. This document preserves the resource planning
sequence; its provisional instructions below are historical. Actual results are
in `DECISION.md` and `results/project-migration{-finished,}-500k.json`.

This is a plan for a future, root-authorized proof. It does not authorize an
implementation, a 500k run, or `VACUUM`. The old 500k qualification bundle was
deleted, so the migration source must be recreated; reconstructing old tables
inside a retained candidate bundle would not exercise the old schema digest,
old physical allocation, or open-time migration path.

## Proportionate proof shape

The candidate only changes the physical representation of `cells` and
`current_cells`. The pending `bundle_open` work rebuilds those two relations in
one transaction, preserves their value/provenance columns and named indexes,
then stamps the schema digest. The existing Project/runtime/search boundaries
and the actual-Project qualification harness remain the proof surface.

The realistic failure modes are an insufficient-disk migration, loss or change
of a value/provenance row, a missing direct-read/search index after reopening,
or a false space claim from an unmeasured compaction. The smallest useful proof
is one authentic old Project, one in-place migration, and one separately
created fresh candidate. It needs no migration benchmark framework, no copied
9 GB source bundle, and no synthetic reconstruction of an old layout.

## Measured inputs and planning arithmetic

The old 500k qualification recorded these closed files:

| Input | Bytes | GiB |
| --- | ---: | ---: |
| Old `project.db` | 6,330,556,416 | 5.90 |
| Old `project.search.db` | 3,022,413,824 | 2.82 |
| Old bundle databases | 9,352,970,240 | 8.71 |

The 10k cell-only representation probe measured 123,916,288 B for old
`WITHOUT ROWID` cells/current cells and 96,550,912 B for the rowid variant: a
27,365,376 B (22.08%) reduction. Applying that ratio mechanically to the 500k
main database gives a **planning proxy** of 4,932,531,512 B for a candidate
main file and 7,954,945,336 B including the unchanged old-size search sidecar.
That is not a 500k size prediction: the 10k probe omitted most project objects,
and the physical-layout audit explicitly rejects linear extrapolation.

For admission, use a pessimistic bound instead: let both the replacement main
allocation and its WAL be as large as the old 6,330,556,416 B main file. The
in-place migration workspace is then:

```
unchanged search sidecar + old main + replacement allocation + WAL
= 3,022,413,824 + 3 * 6,330,556,416
= 22,014,083,072 B
```

With the existing 15 GiB host reserve (16,106,127,360 B), this requires at
least 38,120,210,432 B free immediately before migration. The plan preflight
measured 51,284,152,320 B free, leaving 13,163,941,888 B of unmodelled
headroom. Rounded `df -h` output must never be used for this decision. This is
a provisional no-vacuum capacity pass, not authorization: root must grant a
separate serialized disk slot after an exact preflight and after all other
large artifacts are removed.

Before a 500k migration, use the physical lane's existing small in-place
migration to record its actual peak `project.db + project.db-wal +
project.db-shm` divided by its starting main-file size. Apply that measured
factor to the old 500k main file, with the 3x-main envelope above as a floor.
The approved run must stop before either its main/WAL workspace exceeds that
frozen cap or free space reaches the existing 15 GiB reserve. This is a small
measurement/reporting requirement, not a new benchmark framework or a change
to the ordinary qualification runner's 18 GiB scratch ceiling.

Do **not** run explicit `VACUUM` in that slot. A conservative envelope with a
post-migration main file as large as old plus replacement allocation and a
second full rewrite is:

```
15 GiB reserve + old search sidecar + 4 * old main
= 44,450,766,848 B
```

At the plan preflight it leaves 6,833,385,472 B before SQLite temporary
files or reporting output. That is not enough to attach vacuum to the ordinary
migration proof. Record pre/post migration files and freelist facts, then
defer vacuum/reclaim evidence to a separately approved slot with a cap derived
from the same small migration and an exact fresh-space check.

## Serialized sequence

1. With no retained large bundle present, use the exact reviewed **old-layout**
   composition and the existing actual-Project runner to create one 500k
   bundle with `--retain-success`. Save its JSON report outside the bundle.
   This is the authentic old Project; it includes the old schema digest and
   real mixed-length corpus. Its ordinary runner limits remain unchanged:
   15 GiB free reserve, 18 GiB scratch ceiling, and no run above 200 rows until
   root admits this slot.

2. Before any write, let physical and columnar consumers perform their
   read-only accounting against that retained bundle. The report supplies the
   closed database sizes and `dbstat` facts. No copy, reflink, export, or second
   500k bundle is created.

3. Transfer ownership of this disposable research artifact to the migration
   proof. Remove its read-only modes only after the read-only consumers finish,
   verify it still has the expected old schema digest, and run the reviewed
   candidate's open-time migration **in place**. Capture migration duration,
   database/WAL sizes, `dbstat`, integrity, public read/search results,
   edit/history behavior, and reopen results. Do not call `VACUUM`.

4. After all migrated-bundle evidence is written to small reports and the
   migration proof is accepted, remove that one migrated artifact. Only then
   create the separate fresh 500k candidate qualification with the existing
   runner. This supplies the fresh-candidate comparison without coexisting
   with the old or migrated 9 GB bundle.

5. Compare old report, in-place migrated report, and fresh-candidate report.
   Treat the fresh candidate's closed files as the candidate footprint; do not
   claim that the in-place file shrank without the separately gated vacuum
   experiment.

At every phase only one full Project bundle exists. The runner's scratch,
in-place replacement tables, WAL, and any future vacuum are the actual disk
risks; this sequence removes the avoidable risk of two concurrent 9 GB copies.

## Bounded 10k migration preflight

Before any 500k slot, root may place one disposable copy of the existing old
10k Project fixture beneath an admitted work directory. Run the small probe
inside the same network-isolated `bwrap` shape as qualification: clear the
environment, mount only the exact candidate source, qualification worktree,
canonical read-only venv and required runtime libraries, then mount the owned
work and results directories. Do not bind `/home` or `/etc`. Put the reviewed
candidate source first on `PYTHONPATH` and the qualification worktree second,
so `Project(bundle)` executes the candidate's ordinary open-time migration
while the probe supplies only measurement:

```sh
PYTHONPATH=<candidate-worktree>/src:<qualification-worktree> \
  /venv/bin/python -m scripts.storage_slice.project_migration_probe \
  --bundle /work/old-10k.frisket --work-dir /work \
  --candidate-sha <reviewed-candidate-sha> \
  --expected-old-schema-digest <reviewed-old-schema-digest> \
  --output /results/project-migration-10k.json
```

The probe streams ordered row counts and SHA-256 digests of every copied
`cells` and `current_cells` field before and after open, then records open and
checkpoint phases, file/WAL facts, `dbstat`, integrity/foreign-key checks, and
a small public value/history read. It reuses the qualification sampler and its
existing 15 GiB reserve and 18 GiB scratch ceiling. Progress-handler
cancellation applies to the post-open integrity and checkpoint statements;
the constructor-owned migration is observation-only and is checked immediately
on return. It neither copies a bundle nor runs vacuum. This small proof sets no
500k resource cap; the larger migration remains independently gated.

The composed candidate at
`b4e5906e97aeaa9a677a827b04a9605fad9052cc` completed this exact disposable
10k proof. It moved the schema digest from
`frisket.schema.v1:348c367f3a24a414ba6f1e612e40ea15` to
`frisket.schema.v1:f078f2bc57411d372468936618f2f884`, with identical streamed
field digests/counts and green foreign-key/integrity checks. The starting main
file was 126,287,872 B. During migration, `project.db + project.db-wal +
project.db-shm` peaked at 440,840,160 B, a 3.490756x small-fixture factor;
the whole bundle peaked at 500,724,054 B (2.689108x its 186,204,540 B start).
After checkpoint, `project.db` was 220,307,456 B with 29,808 freelist pages
(122,093,568 B), while `project.search.db` remained 59,850,752 B with no
freelist pages. These are an input to a later 500k gate, not a 500k peak or
compaction claim; the migrated copy is retained only for a separately
authorized vacuum measurement.

Applying the measured 3.490756x main-workspace factor to the old 500k main
file gives 22,098,427,792 B; adding the unchanged old-size FTS sidecar gives a
25,120,841,616 B migration-bundle planning peak. The probe's default total
work-directory cap remains 18 GiB. For a root-approved **migration-only** 500k
run, `--max-scratch-gib 28` sets a 28 GiB total-directory cap and a 26 GiB
(27,917,287,424 B) soft stop, leaving the projected peak below the soft stop.
This override is not for ordinary qualification and requires peer resource
review plus an exact fresh-space check.

The cap applies to total bytes under `--work-dir`, including the already
present old bundle. At start the probe records that directory size and uses
`min(requested cap, initial work bytes + free bytes - 15 GiB reserve)`; this
avoids counting the input bundle twice when deciding how much additional WAL
and replacement-table growth is safe. The constructor-owned migration remains
observation-only, while the sampler and external timeout enforce the measured
headroom around it.
