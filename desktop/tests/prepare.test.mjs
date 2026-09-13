import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const DESKTOP = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

test('Windows native assets retain the tool IDs and stage .exe destinations', () => {
  const probe = `
import hashlib
import importlib.util
import json
import sys
import tempfile
import zipfile
from pathlib import Path

spec = importlib.util.spec_from_file_location("prepare", sys.argv[1])
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)
with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    prepare.RESOURCES = root / "resources"
    (prepare.RESOURCES / "bin").mkdir(parents=True)
    archive = root / "tool.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("tool.exe", b"windows executable")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    cache = root / "cache"
    cache.mkdir()
    archive.replace(cache / digest)
    prepare.stage_native("tool", {
        "url": "https://example.invalid/tool.zip",
        "sha256": digest,
        "member": "tool.exe",
        "destination": "tool.exe",
    }, cache)
    assets = prepare.native_assets("win32")
    print(json.dumps({
        "names": sorted(assets),
        "destinations": {name: asset["destination"] for name, asset in assets.items()},
        "hashes": {name: asset["sha256"] for name, asset in assets.items()},
        "staged": (prepare.RESOURCES / "bin" / "tool.exe").read_bytes().decode(),
    }))
`;
  const result = spawnSync('python3', ['-c', probe, path.join(DESKTOP, 'scripts', 'prepare.py')], {
    cwd: DESKTOP, encoding: 'utf8', env: { PATH: process.env.PATH },
  });
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), {
    names: ['deno', 'ffmpeg', 'ffprobe', 'uv'],
    destinations: {
      uv: 'uv.exe', ffmpeg: 'ffmpeg.exe', ffprobe: 'ffprobe.exe', deno: 'deno.exe',
    },
    hashes: {
      uv: 'a047d55651bc3e0ca24595b25ec4cfcb10f9dca9fb56514e661269b37d4fae68',
      ffmpeg: 'fec81ae03971d9dd4be3ebe02e263bd2ec1d789483f931bdba5f5715e65da2e9',
      ffprobe: 'fec81ae03971d9dd4be3ebe02e263bd2ec1d789483f931bdba5f5715e65da2e9',
      deno: '15e5300b0ba3c3695a7621d90160a746ec9e710228cee639afa9d580f6e3cd11',
    },
    staged: 'windows executable',
  });
});
