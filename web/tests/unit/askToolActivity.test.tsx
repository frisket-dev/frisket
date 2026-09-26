import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { AskToolActivity } from '../../src/components/project-ask/AskToolActivity';
import type { AskEvent } from '../../src/api/projectQA';

function render(payload: AskEvent['payload']) {
  return renderToStaticMarkup(<AskToolActivity event={{ thread_id: 't', turn_id: 'r', seq: 1, created_at: '', kind: 'tool_completed', payload }} />);
}

describe('Ask activity coverage', () => {
  it('shows search terms without exposing structured query JSON', () => {
    expect(render({ tool: 'search_cells', query: 'awarded contracts' })).toContain('awarded contracts');
    expect(render({ tool: 'query_rows', query: { filter: { '2': { eq: 'awarded' } } } })).not.toContain('Query:');
  });

  it('shows analytics quality, paging and denominator facts in the existing disclosure', () => {
    const html = render({ tool: 'analytics', row_count: 1000000, has_more: true, excluded_null_groups: 3,
      quality: { '2': { present: 80, missing: 15, invalid: 5 } },
      denominators: { total: { value: 0, reason: 'zero_denominator', mixed_sign: true } },
    });
    expect(html).toContain('1,000,000 rows considered');
    expect(html).toContain('80 valid values, 15 missing, 5 invalid');
    expect(html).toContain('More groups are available');
    expect(html).toContain('3 groups had no value');
    expect(html).toContain('before group filtering or pagination');
    expect(html).toContain('denominator is zero');
  });

  it('distinguishes incomplete semantic coverage from a full semantic search', () => {
    expect(render({ tool: 'search_cells', hits: 2, coverage: { semantic: false, complete: false, reason: 'embedding_budget' } })).toContain('keyword results are shown');
    expect(render({ tool: 'search_cells', hits: 2, coverage: { semantic: true, complete: true } })).not.toContain('keyword results are shown');
  });
});
