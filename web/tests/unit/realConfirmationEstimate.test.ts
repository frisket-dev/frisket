import { describe, expect, it } from 'vitest';

import { rowsEstimateFromDetail } from '../../src/api/real';

describe('deterministic confirmation quantities', () => {
  it('surfaces a derive.join fan-out estimate', () => {
    expect(rowsEstimateFromDetail({ estimated_rows: 8, max_output_rows: 5 })).toEqual({
      rows: 8,
    });
  });

  it('surfaces a collection expansion preview count', () => {
    expect(rowsEstimateFromDetail({ preview_count: 250, default_cap: 100 })).toEqual({
      rows: 250,
    });
  });
});
