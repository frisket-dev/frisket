import { describe, expect, it } from 'vitest';

import { jsonValuesEqual } from '../../src/actions/canonicalJson';

describe('jsonValuesEqual', () => {
  it('treats undefined object properties as absent JSON properties', () => {
    expect(jsonValuesEqual(
      { name: 'details', properties: undefined },
      { name: 'details' },
    )).toBe(true);
  });
});
