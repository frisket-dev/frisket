import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { promises as fs } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

test('prewarm bundles OCR, Whisper, and VAD without downloading Parakeet', async (t) => {
  const root = await fs.mkdtemp(path.join(os.tmpdir(), 'frisket-prewarm-'));
  const resources = path.join(root, 'resources');
  const trace = path.join(root, 'trace');
  t.after(() => fs.rm(root, { recursive: true, force: true }));
  const files = {
    'frisket/__init__.py': '', 'frisket/ai/__init__.py': '', 'frisket/ai/models/__init__.py': '',
    'frisket/engine/__init__.py': '', 'frisket/engine/_workers/__init__.py': '', 'frisket/ops/__init__.py': '',
    'frisket/trace.py': 'import os\nfrom pathlib import Path\ndef record(value):\n Path(os.environ["PREWARM_TRACE"]).open("a").write(value + "\\n")\n',
    'frisket/ai/models/artifact_manifest.py': 'from types import SimpleNamespace\nfrom frisket.trace import record\ndef artifact(label):\n def get():\n  record(label)\n  return SimpleNamespace(hf_snapshot=SimpleNamespace(repo_id=label, revision="rev", files=("model",)))\n return get\nwhisper_base_artifact = artifact("whisper")\nparakeet_model_artifact = artifact("parakeet")\nparakeet_vad_artifact = artifact("vad")\n',
    'frisket/engine/_workers/parakeet_artifacts.py': 'from frisket.trace import record\ndef huggingface_hub_cache(): return "/cache"\ndef _snapshot_payload(**payload): return payload\ndef _resolve_snapshot(payload):\n record(payload["repo_id"])\n return "/cache/" + payload["repo_id"]\n',
    'frisket/ops/ocr_engines_local.py': 'from frisket.trace import record\ndef _rapidocr_model_root_dir():\n record("rapidocr")\n return "/models"\n',
  };
  await Promise.all(Object.entries(files).map(async ([name, contents]) => {
    const file = path.join(resources, name); await fs.mkdir(path.dirname(file), { recursive: true }); await fs.writeFile(file, contents);
  }));
  const python = process.platform === 'win32' ? 'python' : 'python3';
  const env = process.platform === 'win32'
    ? { ...process.env, PREWARM_TRACE: trace }
    : { PATH: process.env.PATH, PREWARM_TRACE: trace };
  const result = spawnSync(python, ['-I', path.resolve('scripts/prewarm-models.py'), resources], {
    cwd: path.resolve('.'), encoding: 'utf8', env,
  });
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), { rapidocr: 'ready', whisper: 'whisper', vad: 'vad' });
  assert.deepEqual((await fs.readFile(trace, 'utf8')).trim().split(/\r?\n/), ['rapidocr', 'whisper', 'whisper', 'vad', 'vad']);
});
