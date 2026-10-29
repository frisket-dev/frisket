import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');

test('optional language profiles preserve root pins and ship their hashed worker dependencies', () => {
  const script = `
import tomllib
from pathlib import Path

root = Path.cwd()
metadata = tomllib.loads((root / "pyproject.toml").read_text())
root_packages = tomllib.loads((root / "uv.lock").read_text())["package"]
for kind, required in [
    ("spacy", {"spacy", "pydantic"}),
    ("translation", {"ctranslate2", "sentencepiece"}),
]:
    profile = f"sidecar/{kind}-runtime"
    requirements = f"sidecar/{kind}-requirements.txt"
    lock = tomllib.loads((root / profile / "uv.lock").read_text())
    packages = [package for package in lock["package"] if package["source"].get("registry")]
    names = {package["name"] for package in packages}
    assert required <= names
    assert not names.intersection({"frisket-data", "frisket-models", "fastapi", "sqlalchemy", "av", "onnx-asr", "faster-whisper", "torch"})
    if kind == "translation":
        assert "spacy" not in names and len(names) < 15
    else:
        assert "ctranslate2" not in names and len(names) < 50
    for package in packages:
        original = next(item for item in root_packages if (item["name"], item["version"], item["source"]) == (package["name"], package["version"], package["source"]))
        assert package.get("sdist") == original.get("sdist")
        assert package.get("wheels") == original.get("wheels")
    text = (root / requirements).read_text()
    entries = {}
    current = None
    for line in text.splitlines():
        if line and not line.startswith((" ", "#")):
            current = line.split("==", 1)[0]
            entries[current] = False
        elif line.strip().startswith("--hash=sha256:"):
            assert len(line.strip().split(":", 1)[1].split()[0]) == 64
            entries[current] = True
    assert set(entries) == names and all(entries.values())
    build = metadata["tool"]["hatch"]["build"]
    assert {f"{profile}/pyproject.toml", f"{profile}/uv.lock", requirements} <= set(build["targets"]["sdist"]["only-include"])
    assert build["targets"]["wheel"]["force-include"][requirements] == f"frisket/data/model-server/{kind}-requirements.txt"
print("root versions and artifact hashes preserved; minimal hashed profiles bundled")
`;
  const python = process.platform === 'win32' ? 'python' : 'python3';
  const result = spawnSync(python, ['-c', script], {
    cwd: ROOT, encoding: 'utf8', env: process.platform === 'win32' ? { ...process.env } : { PATH: process.env.PATH },
  });
  assert.equal(result.status, 0, result.stderr);
});
