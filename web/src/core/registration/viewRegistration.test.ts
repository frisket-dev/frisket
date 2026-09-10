// Proves the WORK_VIEW_DESCRIPTORS glob conversion:
//   1. Discovery is REAL — WORK_VIEW_DESCRIPTORS reflects the *.view.ts files
//      under core/selectors/views/ on disk, read via fs, independent of any
//      hardcoded list here.
//   2. Every registered view has a title (except 'grid', which is never
//      promoted/labelled — see views/types.ts) AND an availability rule.

import { readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { WORK_VIEW_DESCRIPTORS, WORK_VIEW_KINDS, WORK_VIEW_TITLES } from '../selectors/views/registry';
import type { WorkViewKind } from '../selectors/workView';

const viewsDir = join(dirname(fileURLToPath(import.meta.url)), '..', 'selectors', 'views');

// 'answers' is the sixth glob-registered work-view kind
// (core/selectors/views/answers.view.ts) — added here alongside the
// original five so this file's OWN completeness pin stays exact.
const EXPECTED_KINDS: ReadonlyArray<WorkViewKind> = [
  'grid',
  'document',
  'map',
  'gallery',
  'graph',
  'answers',
];

describe('WORK_VIEW_DESCRIPTORS — glob self-registration', () => {
  it('registers exactly one file per *.view.ts under core/selectors/views/', () => {
    const files = readdirSync(viewsDir).filter((name) => name.endsWith('.view.ts'));
    expect(files.length).toBeGreaterThan(0);
    expect(Object.keys(WORK_VIEW_DESCRIPTORS)).toHaveLength(files.length);
    expect(WORK_VIEW_KINDS).toHaveLength(files.length);
  });

  it('registers exactly the six WorkViewKind union members', () => {
    expect(Object.keys(WORK_VIEW_DESCRIPTORS).sort()).toEqual([...EXPECTED_KINDS].sort());
    expect([...WORK_VIEW_KINDS].sort()).toEqual([...EXPECTED_KINDS].sort());
  });

  it("every descriptor's kind equals its table key (no misfiled view file)", () => {
    for (const [key, descriptor] of Object.entries(WORK_VIEW_DESCRIPTORS)) {
      expect(descriptor.kind).toBe(key);
    }
  });

  it('every registered view has an availability rule (computeEntry)', () => {
    for (const descriptor of Object.values(WORK_VIEW_DESCRIPTORS)) {
      expect(typeof descriptor.computeEntry).toBe('function');
    }
  });

  it('every kind except grid has a title; grid has none (never promoted/labelled)', () => {
    for (const kind of EXPECTED_KINDS) {
      if (kind === 'grid') {
        expect(WORK_VIEW_DESCRIPTORS.grid.title).toBeUndefined();
        expect(WORK_VIEW_TITLES.grid).toBeUndefined();
      } else {
        expect(typeof WORK_VIEW_DESCRIPTORS[kind].title).toBe('string');
        expect(WORK_VIEW_DESCRIPTORS[kind].title!.length).toBeGreaterThan(0);
        expect(WORK_VIEW_TITLES[kind]).toBe(WORK_VIEW_DESCRIPTORS[kind].title);
      }
    }
  });
});
