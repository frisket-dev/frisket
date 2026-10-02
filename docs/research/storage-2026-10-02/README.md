# Storage decision evidence

[`DECISION.md`](DECISION.md) is the final decision and result summary for
issue #239. The other files are bounded evidence behind that decision:

- `project-qualification-300k-final.json` and
  `project-qualification-500k-final.json` are the final actual-`Project`
  qualification reports.
- `project-qualification-300k.json` is the earlier diagnostic baseline that
  exposed broad edit undo/redo work. It used a narrower workload and is not a
  second final result.
- `aggregate-membership-measurement.json` records the bounded compact-receipt
  measurement.
- `batch_probe/` contains the clustering probe, raw results, and interpretation.
- `wal_probe.py` and `wal-results.json` reproduce the bounded pinned-reader WAL
  observation.
- `overhead.md` is a dated audit at `aa5201b2fc2abe26a03f1547721ca715ba7baaf9`.
  It preserves the provenance for measuring the three index candidates,
  including the two exact duplicates and the separately measured source-run
  prefix. `DECISION.md` contains the final implemented three-index result; the
  audit is supporting evidence, not the final recommendation.

The final qualifications ran from research commit
`c4a4c25903086ef29dcd726a7e7180a866224cf9`, which contains reviewed production
candidate `8e77c701039d314a8863f2230e3c7a2bf0aa6c69` as an ancestor. Main later
squashed that candidate as `1b8cde5c843aedee3fb1702db413a91f77c9231a`;
the candidate and landed production commits have identical trees. The diagnostic
300k baseline ran from `bfef160a55e5ee977ba5f0fca97294b6c94c41c3`.

The final runner commands were:

```sh
PYTHONPATH=src:. timeout --signal=INT --kill-after=30s 45m \
  python -m scripts.storage_slice.project_benchmark \
  --rows 300000 --composition-sha 8e77c701039d314a8863f2230e3c7a2bf0aa6c69 \
  --work-dir /path/to/owned-scratch --output /path/to/report.json

PYTHONPATH=src:. timeout --signal=INT --kill-after=30s 90m \
  python -m scripts.storage_slice.project_benchmark \
  --rows 500000 --composition-sha 8e77c701039d314a8863f2230e3c7a2bf0aa6c69 \
  --work-dir /path/to/owned-scratch --output /path/to/report.json
```

Both ran in fresh network-disabled processes with the repository's locked Python
environment. The WAL probe requires a writable `/scratch`; the clustering probe
accepts explicit `--rows` and `--project` arguments. Typical direct invocations
are:

```sh
PYTHONPATH=src:. python docs/research/storage-2026-10-02/wal_probe.py
PYTHONPATH=src:. python docs/research/storage-2026-10-02/batch_probe/probe.py \
  --rows 10000 --project /path/to/disposable.frisket
```

These probes create disposable databases and are research commands rather than
standing CI. No generated database bundle, log, virtual environment, or private
coordination note is archived here.
