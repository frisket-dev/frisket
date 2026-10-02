# Storage-layout research archive — 2026-10-02

This is a research-only harness and report archive. The production candidate was
`b4e5906e97aeaa9a677a827b04a9605fad9052cc`; the archived qualification harness
is `36f92b8f58741fce03ed8d2ba5834e97e9412182`.

Run probes with the candidate source first on `PYTHONPATH`, followed by this
archive's source. These bounded measurements make no claim for 30M rows or
100 GB projects.

Start with [DECISION.md](DECISION.md) for the measured outcome, limitations and
next implementation gate. Detailed analytical results are in [columnar.md](columnar.md).

The four standalone probes were archived unchanged under `scripts/storage_slice/`
(`storage_layout_probe.py`, `storage_size_probe.py`, `storage_query_probe.py`,
`storage_columnar_probe.py`). Original research notes retain their original
worktree paths. The qualification and migration modules remain in the same
package. For module invocation, set `PYTHONPATH` to `<candidate>/src:<archive>`
and use `python -m scripts.storage_slice.<module>`. Run only in an isolated
environment with the documented disk/memory limits; this archive is not part
of the production change or a standing CI benchmark suite.
