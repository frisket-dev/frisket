import json
import tempfile
from pathlib import Path
from time import perf_counter
from frisket.engine.store import Project

with tempfile.TemporaryDirectory(dir='/scratch') as tmp:
    p = Project.create(Path(tmp)/'probe.frisket')
    try:
        sid = p.add_sheet('Documents')
        cid = p.add_column(sid, 'body')
        p.add_rows(sid, [{'body':'original'}], {'body':cid})
        p.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        snap = p.read_snapshot()
        assert snap.row_count(sid) == 1
        def state():
            checkpoint = list(p.db.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone())
            return {'main_bytes':p.db_path.stat().st_size,
                    'wal_bytes':Path(str(p.db_path)+'-wal').stat().st_size,
                    'checkpoint':checkpoint}
        before=state()
        elapsed=[]
        for i in range(200):
            start=perf_counter()
            p.add_rows(sid,[{'body':str(i)+' '+('sample paragraph ' * 4096)}],{'body':cid})
            elapsed.append(perf_counter()-start)
        held=state()
        assert snap.row_count(sid) == 1
        assert p.row_count(sid) == 201
        snap.close()
        released=state()
        p.add_rows(sid,[{'body':'after release'}],{'body':cid})
        reused=state()
        assert reused['wal_bytes'] <= held['wal_bytes']
        assert released['checkpoint'][1] == released['checkpoint'][2]
        p.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        final=state()
        print(json.dumps({'before':before,'reader_held':held,'reader_released':released,'next_write':reused,'after_truncate':final,'writes':200,'write_seconds_max':max(elapsed),'write_seconds_total':sum(elapsed)},indent=2))
    finally:
        p.close()
