import { describe, expect, it } from 'vitest';

import type { Row, SheetMeta } from '../../src/api/types';
import { estimateEmbeddingCost } from '../../src/components/embeddings/costEstimate';

const sheet: SheetMeta = {
  id: '7',
  name: 'Stories',
  rowCount: 200,
  columns: [
    { id: '11', name: 'headline', type: 'text', ai_generated: false },
    { id: '12', name: 'body', type: 'text', ai_generated: false },
    { id: '13', name: 'ignored', type: 'text', ai_generated: false },
  ],
  citedColumnIds: [],
  annotatedTextColumnIds: [],
};

function row(index: number, headline: string | null, body: string | null): Row {
  return {
    id: String(index + 1),
    index,
    cells: { '11': headline, '12': body, '13': 'x'.repeat(1_000) },
    provenance: {},
  };
}

describe('embedding cost estimate', () => {
  it('scales sampled selected-column tokens to the first run and 100 rows', () => {
    const estimate = estimateEmbeddingCost({
      rows: [row(0, 'a'.repeat(40), 'b'.repeat(40)), row(1, null, null)],
      sheet,
      sourceColumns: ['headline', 'body'],
      inputUsdPerMillionTokens: 2,
    });

    // The first sampled row is ~20 input tokens; the second is empty. Scaling
    // the 10-token sample average across 200 rows gives 2,000 tokens.
    expect(estimate.estimatedTokens).toBe(2_000);
    expect(estimate.firstRunUsd).toBeCloseTo(0.004);
    expect(estimate.per100RowsUsd).toBeCloseTo(0.002);
    expect(estimate.sampledEmbeddableRows).toBe(1);
  });
});
