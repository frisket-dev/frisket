import { readdirSync, readFileSync } from 'node:fs';
import { dirname, extname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const srcRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');

function productionModules(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) return productionModules(path);
    if (!['.ts', '.tsx'].includes(extname(entry.name)) || entry.name.includes('.test.')) {
      return [];
    }
    return [path];
  });
}

describe('STRUCT-A1 previewView production-read fence', () => {
  it('routes every raw previewView store selector through previewViewForSheet', () => {
    const offenders: string[] = [];
    const rawSelection =
      /const\s+([A-Za-z_$][\w$]*)\s*=\s*useSelector\(\s*[A-Za-z_$][\w$.]*\.store\s*,\s*\(\s*([A-Za-z_$][\w$]*)\s*\)\s*=>\s*\2\.previewView\s*\)/g;

    for (const path of productionModules(srcRoot)) {
      const source = readFileSync(path, 'utf8');
      for (const match of source.matchAll(rawSelection)) {
        const selectedName = match[1];
        const routed = new RegExp(`previewViewForSheet\\(\\s*${selectedName}\\s*,`).test(source);
        if (!routed) {
          offenders.push(`${relative(srcRoot, path)}: ${match[0]}`);
        }
      }
    }

    expect(offenders).toEqual([]);
  });
});
