# Retained actual-Project qualification bundle

**Execution update:** root admitted and completed the old-layout 500k run,
then the migration/reclaim proof. The fresh candidate run is now executing
serially with the unchanged ordinary qualification limits. `PLAN.md` records
process ownership; `DECISION.md` records results. The initial admission notes
below explain the original gate.

The existing reviewed `scripts.storage_slice.project_benchmark` runner remains
the only qualification harness. It streams the actual Project schema, performs
the full persisted readback, search, typed edit/undo/redo, history, checkpoint,
and reopen workloads. This preparation adds no benchmark framework and no
production storage behavior.

## Admission and resource plan

The final 500,000-row run is not authorized by this plan. Run it only after
root grants one serialized slot, production changes have received their review,
and the reviewed composition SHA is known. Do not run any tier above 200 rows
before that grant.

The prior 500k bundle used about 9.4 GB. This host currently reports about
39 GB free, which would leave about 30 GB using that observed footprint, but
the runner's existing 15 GiB host reserve remains the qualification gate.
Recheck `df -h /home/dev` immediately before scheduling and keep the unchanged
18 GiB scratch ceiling. The repository's 25 GB free-space warning applies to
dependency hydration, not this already-hydrated runner. A larger host and the
30-million-row/100-GB qualification remain deferred.

Use the existing runner's 15 GiB reserve, 6/8 GiB RSS limits, 4 GiB available
memory floor, and timeout handling unchanged. There is one run only; no repeat
is needed for the later physical and columnar analyses.

## Final invocation and handoff

Run from the reviewed production composition with the canonical read-only
virtual environment bound at `/venv`, source first on `PYTHONPATH`, cleared
environment, and no network. Give the runner a disposable work directory on
the admitted filesystem and use `--retain-success`:

```sh
reviewed_worktree=<reviewed-worktree>
git_common_dir=$(git -C "${reviewed_worktree}" rev-parse --path-format=absolute --git-common-dir)
sandbox_root=$(mktemp -d /tmp/frisket-qualification-sandbox.XXXXXX)
mkdir -p "${sandbox_root}"/{bin,dev,lib,lib64,proc,results,run,src,tmp,usr,venv,work}
mkdir -p "${sandbox_root}$(dirname "${git_common_dir}")"

bwrap --die-with-parent --unshare-net --clearenv --bind "${sandbox_root}" / \\
  --ro-bind /usr /usr --ro-bind /lib /lib --ro-bind /lib64 /lib \\
  --ro-bind /bin /bin \\
  --ro-bind /home/dev/Development/frisket/frisket/.venv /venv \\
  --ro-bind "${reviewed_worktree}" /src \\
  --ro-bind "${git_common_dir}" "${git_common_dir}" \\
  --bind <admitted-disposable-work-dir> /work \\
  --bind <research-results> /results \\
  --tmpfs /tmp --proc /proc --dev /dev --remount-ro / \\
  --chdir /src \\
  --setenv PATH /venv/bin:/usr/bin:/bin --setenv PYTHONPATH /src/src:/src \\
  --setenv HOME /tmp \\
  timeout --signal=INT --kill-after=30s 45m /venv/bin/python \\
  -m scripts.storage_slice.project_benchmark --rows 500000 \\
  --composition-sha <reviewed-production-composition-sha> \\
  --work-dir /work --output /results/project-qualification-500k.json \\
  --retain-success
```

The lane removes its empty `sandbox_root` after the process exits. The admitted
work and results directories are distinct writable mounts; the source,
canonical environment, and the exact Git metadata needed for the preflight are
read-only. Do not bind `/home` or `/etc`: either could expose unrelated local
credentials or configuration to the runner.

On completion, the JSON report and command summary name the retained bundle
and its owner: `storage-layout columnar and physical measurement lanes`. The
bundle is closed, its files are mode `0444`, and its directories are mode
`0555`. Columnar and physical work may only open it read-only and must write
their derived results elsewhere. The report's checkpoint section contains
per-object `dbstat` pages and bytes for both `project.db` and
`project.search.db`; those figures are the starting physical-accounting facts.

Default runs remove their owned scratch. An interrupted or resource-stopped
run removes its scratch even when `--retain-success` was requested, and is not
a handoff artifact.
