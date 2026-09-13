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
  const python = process.platform === 'win32' ? 'python' : 'python3';
  const env = process.platform === 'win32'
    ? { ...process.env }
    : { PATH: process.env.PATH };
  const result = spawnSync(python, ['-c', probe, path.join(DESKTOP, 'scripts', 'prepare.py')], {
    cwd: DESKTOP, encoding: 'utf8', env,
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

for (const copyFallback of [false, true]) {
  test(`bundled snapshots remain portable through parent aliases (${copyFallback ? 'copy fallback' : 'hardlinks'})`, () => {
    const probe = `
import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("prepare", sys.argv[1])
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)
with tempfile.TemporaryDirectory() as temporary:
    root = Path(temporary)
    cache = root / "bundle" / "huggingface"
    repository = cache / "models--fixture--whisper"
    blob = repository / "blobs" / "digest"
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"tiny model payload")
    blob_inode = blob.stat().st_ino
    snapshot = repository / "snapshots" / "revision"
    snapshot.mkdir(parents=True)
    model = snapshot / "model.bin"
    alias = snapshot / "alias.bin"
    for entry in (model, alias):
        entry.symlink_to(Path("../../blobs/digest"), target_is_directory=False)
    config = snapshot / "config.json"
    config.write_text('{"model":"fixture"}')
    # macOS /tmp aliases /private/tmp; reproduce the same parent alias on all
    # platforms so containment compares canonical paths on both sides.
    bundle_alias = root / "bundle-alias"
    bundle_alias.symlink_to(cache.parent, target_is_directory=True)
    aliased_cache = bundle_alias / "huggingface"
    fallback = sys.argv[2] == "true"
    if fallback:
        with patch.object(Path, "hardlink_to", side_effect=OSError("hardlinks unavailable")):
            prepare.materialize_bundled_snapshots(aliased_cache)
    else:
        prepare.materialize_bundled_snapshots(aliased_cache)
    ordinary = all(entry.is_file() and not entry.is_symlink() for entry in (model, alias))
    hardlinked = model.stat().st_ino == blob_inode and alias.stat().st_ino == blob_inode
    blob_removed = not blob.exists()
    # The artifact works after leaving its build location and after a copier
    # treats every entry as a regular file, as Electron Builder does.
    moved = root / "moved"
    cache.parent.replace(moved)
    installed = root / "installed"
    shutil.copytree(moved, installed, symlinks=True)
    shutil.rmtree(moved)
    installed_snapshot = installed / "huggingface" / repository.name / "snapshots" / "revision"
    print(json.dumps({
        "ordinary": ordinary, "hardlinked": hardlinked, "blobRemoved": blob_removed,
        "model": (installed_snapshot / "model.bin").read_text(),
        "alias": (installed_snapshot / "alias.bin").read_text(),
        "config": json.loads((installed_snapshot / "config.json").read_text()),
    }))
`;
    const python = process.platform === 'win32' ? 'python' : 'python3';
    const env = process.platform === 'win32' ? { ...process.env } : { PATH: process.env.PATH };
    const result = spawnSync(python, [
      '-c', probe, path.join(DESKTOP, 'scripts', 'prepare.py'), String(copyFallback),
    ], { cwd: DESKTOP, encoding: 'utf8', env });
    assert.equal(result.status, 0, result.stderr);
    assert.deepEqual(JSON.parse(result.stdout), {
      ordinary: true, hardlinked: !copyFallback, blobRemoved: true,
      model: 'tiny model payload', alias: 'tiny model payload', config: { model: 'fixture' },
    });
  });
}
