import { describe, expect, it } from 'vitest';

import type { ColumnTypeInfo } from '../../src/api/open';
import {
  applyColumnTypeRegistry,
  facetBehaviorFor,
  presentationFor,
} from '../../src/grid/typeRegistry';

const collection = (name: string, facet: Record<string, unknown>): ColumnTypeInfo => ({
  name,
  core: name === 'json',
  presentation: { renderer: 'json', facet },
  hasValidator: true,
  hasParser: false,
  description: name,
});

describe('collection facet behavior', () => {
  it('preserves the real server collection hint when applying the column-type registry', () => {
    applyColumnTypeRegistry([
      collection('json', { kind: 'collection', operator: 'list_contains_any' }),
    ]);

    expect(facetBehaviorFor('json')).toEqual({
      kind: 'collection',
      preferred: false,
      oneClick: false,
      operator: 'list_contains_any',
    });
  });

  it('treats an active server entry as authoritative over the local default', () => {
    applyColumnTypeRegistry([
      {
        ...collection('json', { kind: 'collection', operator: 'list_contains_any' }),
        presentation: { renderer: 'text' },
      },
    ]);

    expect(presentationFor('json')).toEqual({ renderer: 'text' });
    expect(facetBehaviorFor('json')).toBeNull();
  });

  it('fails closed for malformed or unsupported collection hints', () => {
    applyColumnTypeRegistry([
      collection('missing_operator', { kind: 'collection' }),
      collection('wrong_operator', { kind: 'collection', operator: 'eq' }),
      collection('extra_key', { kind: 'collection', operator: 'list_contains_any', preferred: true }),
    ]);

    expect(facetBehaviorFor('missing_operator')).toBeNull();
    expect(facetBehaviorFor('wrong_operator')).toBeNull();
    expect(facetBehaviorFor('extra_key')).toBeNull();
  });
});
