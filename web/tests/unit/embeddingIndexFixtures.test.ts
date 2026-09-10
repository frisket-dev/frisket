import { describe, expect, it } from 'vitest';
import {
  embeddingIndexFixture,
  embeddingIndexListFixture,
} from '../support/embeddingIndexFixtures';

describe('embedding index fixtures', () => {
  it('supplies the required generated-contract maintenance policy', () => {
    const index = embeddingIndexFixture({
      index_id: 'embidx_test',
      name: 'Test index',
      sheet_id: 7,
      space_id: 'space_test',
      total_items: 2,
      ready_items: 2,
    });
    const response = embeddingIndexListFixture(7, [index]);

    expect(response.indexes[0].maintenance).toEqual({
      mode: 'manual',
      schedule: null,
    });
    expect(response.indexes[0].freshness.maintenance_mode).toBe('manual');
  });
});
