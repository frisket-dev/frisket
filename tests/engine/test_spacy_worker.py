"""Exercise the real framed worker with a fake spaCy import and no core dependencies."""

from pathlib import Path
import subprocess
import sys

from frisket.engine._workers.framing import FrameCodec


def test_worker_import_closure_and_entity_protocol():
    root = Path(__file__).resolve().parents[2] / "src"
    script = r"""
import contextlib, io, sys, types
sys.path.insert(0, sys.argv[1])
class DenyCore:
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'pydantic', 'duckdb', 'httpx', 'fastapi', 'sqlalchemy', 'numpy'}:
            raise AssertionError('unexpected core dependency: '+fullname)
sys.meta_path.insert(0,DenyCore())
from frisket.engine._workers.spacy_worker import framed_main, CODEC
loads=[]
def load(path):
    loads.append(path)
    ent=types.SimpleNamespace(text='Ada', label_='PERSON', start_char=0, end_char=3)
    return lambda text: types.SimpleNamespace(ents=[ent])
sys.modules['spacy']=types.SimpleNamespace(load=load)
source=io.BytesIO()
for frame in [{'type':'init','model_path':'/owned/model'}, {'type':'extract','text':'Ada','labels':['person']}, {'type':'extract','text':'Ada','labels':['location']}, {'type':'close'}]:
    CODEC.write_frame(source,frame)
source.seek(0)
sink=io.BytesIO()
sys.stdin=types.SimpleNamespace(buffer=source)
original=sys.stdout
sys.stdout=types.SimpleNamespace(buffer=sink)
try:
    assert framed_main()==0
finally:
    sys.stdout=original
assert loads==['/owned/model']
sys.stdout.buffer.write(sink.getvalue())
"""
    result = subprocess.run(
        # subprocess-boundary: prove cold worker imports without core dependencies loaded by pytest.
        [sys.executable, "-I", "-c", script, str(root)], capture_output=True, check=True
    )
    import io

    frames = io.BytesIO(result.stdout)
    codec = FrameCodec(4 * 1024 * 1024, 4 * 1024 * 1024, ValueError)
    assert codec.read_frame(frames) == {"type": "ready"}
    assert codec.read_frame(frames) == {
        "type": "result",
        "entities": [
            {"text": "Ada", "type": "person", "start": 0, "end": 3, "score": None}
        ],
    }
    assert codec.read_frame(frames) == {"type": "result", "entities": []}
    assert codec.read_frame(frames) is None
