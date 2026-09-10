import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { describe, expect, it } from 'vitest';

import { DuplicateJsonKeyError, parseStrictJson } from '../../src/actions/strictJson';

// Shared accept/reject fixture with src/frisket/ops/api_call_request.py's
// strict_json_loads (tested by tests/ops/test_strict_json_fixture.py). The
// two implementations are deliberately independent (mirrors a backend
// security behavior no library provides) with no shared runtime code, so
// this fixture is the parity contract that keeps their accept/reject behavior
// aligned.
const FIXTURE_PATH = fileURLToPath(
  new URL('../../../tests/fixtures/strict_json_accept_reject.json', import.meta.url),
);
type FixtureCase = {
  name: string;
  input: string;
  expect: 'accept' | 'reject';
  reason?: string;
};
const { cases: FIXTURE_CASES } = JSON.parse(readFileSync(FIXTURE_PATH, 'utf8')) as {
  cases: FixtureCase[];
};

describe('parseStrictJson (shared accept/reject fixture)', () => {
  for (const testCase of FIXTURE_CASES) {
    it(`${testCase.expect}s: ${testCase.name}`, () => {
      if (testCase.expect === 'accept') {
        expect(() => parseStrictJson(testCase.input)).not.toThrow();
      } else {
        expect(() => parseStrictJson(testCase.input)).toThrow();
      }
    });
  }
});

describe('parseStrictJson', () => {
  it('accepts repeated key names in different objects', () => {
    expect(parseStrictJson('{"left":{"id":1},"right":{"id":2}}')).toEqual({
      left: { id: 1 },
      right: { id: 2 },
    });
  });

  it('rejects a duplicate key at the root', () => {
    expect(() => parseStrictJson('{"role":"user","role":"admin"}'))
      .toThrow(DuplicateJsonKeyError);
  });

  it('rejects duplicate keys nested inside arrays and objects', () => {
    expect(() => parseStrictJson('{"items":[{"id":1,"id":2}]}'))
      .toThrow(/Duplicate JSON key: "id"/);
  });

  it('compares decoded keys rather than their source spelling', () => {
    expect(() => parseStrictJson('{"name":1,"\\u006eame":2}'))
      .toThrow(/Duplicate JSON key: "name"/);
  });

  it('rejects number literals that overflow to infinity', () => {
    expect(() => parseStrictJson('{"value":1e999}')).toThrow(/finite/);
  });
});
