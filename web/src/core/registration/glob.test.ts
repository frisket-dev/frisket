// Pure unit pin for collectEagerRegistrations — the collision-uniqueness assert
// ("duplicate type = build-time error"). Exercised here against SYNTHETIC module
// maps shaped like import.meta.glob's eager output (`{ path -> { default } }`),
// independent of any real glob-registered inventory, so this file proves the
// MECHANISM in isolation.
//
// The mechanism's actual wiring — commandRegistry.ts and views/registry.ts
// calling this against a REAL `import.meta.glob(...)` result — is proven against
// the built module in commandRegistration.test.ts and viewRegistration.test.ts.

import { describe, expect, it } from 'vitest';
import { collectEagerRegistrations } from './glob';

interface Fixture {
  key: string;
  label: string;
}

function moduleMap(entries: ReadonlyArray<[path: string, value: Fixture]>) {
  return Object.fromEntries(entries.map(([path, value]) => [path, { default: value }]));
}

describe('collectEagerRegistrations', () => {
  it('collects every module keyed by keyOf(default)', () => {
    const modules = moduleMap([
      ['./a.fixture.ts', { key: 'alpha', label: 'A' }],
      ['./b.fixture.ts', { key: 'beta', label: 'B' }],
    ]);

    const result = collectEagerRegistrations(modules, (v) => v.key, 'fixture');

    expect(Object.keys(result).sort()).toEqual(['alpha', 'beta']);
    expect(result.alpha).toEqual({ key: 'alpha', label: 'A' });
    expect(result.beta).toEqual({ key: 'beta', label: 'B' });
  });

  it('returns an empty map for an empty glob result', () => {
    expect(collectEagerRegistrations(moduleMap([]), (v: Fixture) => v.key, 'fixture')).toEqual({});
  });

  it('throws — a build-time error, not a silent overwrite — when two files claim the same key', () => {
    const modules = moduleMap([
      ['./first-party/a.fixture.ts', { key: 'dup', label: 'first' }],
      ['./first-party/b.fixture.ts', { key: 'dup', label: 'second' }],
    ]);

    expect(() => collectEagerRegistrations(modules, (v) => v.key, 'fixture')).toThrow(
      /duplicate fixture registration for 'dup'/,
    );
  });

  it('the thrown message names both colliding file paths and the kind label', () => {
    const modules = moduleMap([
      ['./first-party/openSheet.command.ts', { key: 'openSheet', label: 'x' }],
      ['./first-party/openSheetAgain.command.ts', { key: 'openSheet', label: 'y' }],
    ]);

    try {
      collectEagerRegistrations(modules, (v) => v.key, 'first-party command');
      expect.unreachable('expected collectEagerRegistrations to throw');
    } catch (err) {
      const message = (err as Error).message;
      expect(message).toContain('first-party command');
      expect(message).toContain('./first-party/openSheet.command.ts');
      expect(message).toContain('./first-party/openSheetAgain.command.ts');
    }
  });

  it('does not throw for non-colliding keys that share a common prefix', () => {
    const modules = moduleMap([
      ['./view.grid.ts', { key: 'view.grid', label: 'grid' }],
      ['./view.graph.ts', { key: 'view.graph', label: 'graph' }],
    ]);
    expect(() => collectEagerRegistrations(modules, (v) => v.key, 'fixture')).not.toThrow();
  });
});

// ---- Real import.meta.glob, not a synthetic module map --------------------
//
// The tests above construct a `{ path -> { default } }` object by hand to
// pin collectEagerRegistrations in isolation. These two prove the ACTUAL
// discovery mechanism: a real `import.meta.glob(..., { eager: true })` call
// against on-disk fixture files, fed through the same collector
// core/commands/registry.ts and core/selectors/views/registry.ts use.
// __fixtures__/ok/ has two distinct-key files; __fixtures__/dup/ has two
// files that deliberately claim the same key.
describe('collectEagerRegistrations against a REAL import.meta.glob result', () => {
  it('discovers every fixture file with no collision', () => {
    const modules = import.meta.glob('./__fixtures__/ok/*.fixture.ts', { eager: true }) as Record<
      string,
      { default: Fixture }
    >;
    expect(Object.keys(modules)).toHaveLength(2); // proves the glob itself found both files
    const result = collectEagerRegistrations(modules, (v) => v.key, 'ok-fixture');
    expect(Object.keys(result).sort()).toEqual(['one', 'two']);
  });

  it('throws when two REAL globbed files claim the same key', () => {
    const modules = import.meta.glob('./__fixtures__/dup/*.fixture.ts', { eager: true }) as Record<
      string,
      { default: Fixture }
    >;
    expect(Object.keys(modules)).toHaveLength(2); // the glob found both colliding files
    expect(() => collectEagerRegistrations(modules, (v) => v.key, 'dup-fixture')).toThrow(
      /duplicate dup-fixture registration for 'dupFixture'/,
    );
  });
});
