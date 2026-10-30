import { describe, expect, it } from 'vitest';
import type { SelectorChoice } from '../../src/api/selectorChoices';
import { selectorUpdates } from '../../src/engine-selector/selectorUpdates';

describe('selector authored choices', () => {
  it('keeps an engine and its model together for the action fields', () => {
    const choice = { authored_selection: { kind: 'engine_model', engine: 'cloud', model: 'model-id' } } as SelectorChoice;
    expect(selectorUpdates('engine', 'model', choice)).toEqual({ engine: 'cloud', model: 'model-id' });
    expect(selectorUpdates('engine', undefined, choice)).toBeNull();
  });
});
