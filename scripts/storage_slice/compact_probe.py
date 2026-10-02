import json
import shutil
import time
from pathlib import Path
from frisket.engine.store import Project
from scripts.storage_slice.benchmark import PhaseSampler
from scripts.storage_slice.project_migration_probe import _raw_digests
from scripts.storage_slice.project_benchmark import _dbstat_accounting, _file_sizes

bundle = Path('/work/old-10k.frisket')
started = time.perf_counter()
before = _raw_digests(bundle)
report = {'before_files': _file_sizes(bundle), 'free_before': shutil.disk_usage('/work').free}
sampler = PhaseSampler('/work', hard_limit_bytes=1024**3)
sampler.start()
project = Project(bundle)
project.db.set_progress_handler(lambda: int(sampler.hard_limit.is_set()), 10000)
sampler.on_limit = project.db.interrupt
try:
    sampler.set_phase('compact')
    report['compact_result'] = project.compact()
    sampler.sample()
    report['after_compact_files'] = _file_sizes(bundle)
    report['checkpoint'] = list(project.db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone())
    assert project.db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
finally:
    project.close()
    sampler.stop()
assert not sampler.hard_limit.is_set(), sampler.limit_reason
assert _raw_digests(bundle) == before
report.update({'status': 'completed', 'seconds': time.perf_counter()-started,
               'after_closed_files': _file_sizes(bundle), 'peaks': sampler.peaks,
               'main_accounting': _dbstat_accounting(bundle/'project.db'),
               'free_after': shutil.disk_usage('/work').free})
Path('/results/compact-10k.json').write_text(json.dumps(report, indent=2)+'\n')
print(json.dumps({key: report[key] for key in ('status','seconds','compact_result')}))
