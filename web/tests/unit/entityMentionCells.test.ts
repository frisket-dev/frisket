import { GridCellKind } from '@glideapps/glide-data-grid';
import { describe, expect, it } from 'vitest';

import { buildCell, isExpandableEntityMentionCell } from '../../src/grid/cells';
import { columnDef, row } from '../support/domainFixtures';

const entityColumn = columnDef({
  id: 'entities',
  name: 'entities',
  type: 'json',
  semanticType: 'entity_mentions',
});

function entity(text: string) {
  return { type: 'organization', text };
}

function jsonCell(items: unknown[]) {
  return row({ entities: JSON.stringify(items) });
}

describe('entity mention grid cells', () => {
  it('keeps marked entity mention arrays collapsed to their count by default', () => {
    const cell = buildCell(entityColumn, jsonCell([entity('NYPD'), entity('FBI')]));

    expect(cell).toMatchObject({
      kind: GridCellKind.Bubble,
      data: ['2 entities'],
    });
    expect(cell.cursor).toBe('pointer');
    expect(isExpandableEntityMentionCell(entityColumn, cell)).toBe(true);
  });

  it('expands a marked entity mention array into its text pills in source order', () => {
    const cell = buildCell(
      entityColumn,
      jsonCell([entity('NYPD'), entity('Department of Justice'), entity('FBI')]),
      { expandEntityMentions: true },
    );

    expect(cell).toMatchObject({
      kind: GridCellKind.Bubble,
      data: ['NYPD', 'Department of Justice', 'FBI'],
    });
  });

  it('shows all ten entity pills when expanded', () => {
    const labels = Array.from({ length: 10 }, (_, index) => `Entity ${index + 1}`);
    const cell = buildCell(entityColumn, jsonCell(labels.map(entity)), {
      expandEntityMentions: true,
    });

    expect(cell).toMatchObject({ kind: GridCellKind.Bubble, data: labels });
  });

  it('caps expanded entity pills at fifty and makes the hidden remainder explicit', () => {
    const labels = Array.from({ length: 53 }, (_, index) => `Entity ${index + 1}`);
    const cell = buildCell(entityColumn, jsonCell(labels.map(entity)), {
      expandEntityMentions: true,
    });

    expect(cell).toMatchObject({
      kind: GridCellKind.Bubble,
      data: [...labels.slice(0, 50), '+3 more'],
    });
  });

  it('does not expand an unmarked JSON list, even when expansion is requested', () => {
    const webResults = columnDef({ id: 'results', name: 'web results', type: 'json' });
    const cell = buildCell(
      webResults,
      row({ results: JSON.stringify([entity('NYPD'), entity('FBI')]) }),
      { expandEntityMentions: true },
    );

    expect(cell).toMatchObject({ kind: GridCellKind.Bubble, data: ['2 web results'] });
    expect(cell.cursor).toBeUndefined();
    expect(isExpandableEntityMentionCell(webResults, cell)).toBe(false);
  });

  it('counts only valid mentions and skips malformed entries without crashing', () => {
    const source = jsonCell([
      entity('NYPD'),
      null,
      'FBI',
      { type: 'organization' },
      { text: 42 },
      entity('DOJ'),
    ]);
    const collapsed = buildCell(entityColumn, source);
    const expanded = buildCell(
      entityColumn,
      source,
      { expandEntityMentions: true },
    );

    expect(collapsed).toMatchObject({ kind: GridCellKind.Bubble, data: ['2 entities'] });
    expect(expanded).toMatchObject({ kind: GridCellKind.Bubble, data: ['NYPD', 'DOJ'] });
  });

  it('requires both the JSON storage type and the entity semantic contract', () => {
    const unmarked = columnDef({ id: 'unmarked', name: 'unmarked', type: 'json' });
    const wrongStorage = columnDef({
      id: 'wrong-storage',
      name: 'wrong storage',
      type: 'text',
      semanticType: 'entity_mentions',
    });
    const value = JSON.stringify([entity('NYPD')]);

    const unmarkedCell = buildCell(unmarked, row({ unmarked: value }), { expandEntityMentions: true });
    const wrongStorageCell = buildCell(wrongStorage, row({ 'wrong-storage': value }), {
      expandEntityMentions: true,
    });

    expect(unmarkedCell.cursor).toBeUndefined();
    expect(isExpandableEntityMentionCell(unmarked, unmarkedCell)).toBe(false);
    expect(wrongStorageCell.cursor).toBeUndefined();
    expect(isExpandableEntityMentionCell(wrongStorage, wrongStorageCell)).toBe(false);
  });

  it('does not make synthetic entity-cell states interactive', () => {
    const value = JSON.stringify([entity('NYPD')]);
    const synthetic = [
      buildCell(entityColumn, row({ entities: value }, { cellErrors: { entities: 'failed' } })),
      buildCell(entityColumn, row({ entities: value }, { cellStates: { entities: 'incomplete' } })),
      buildCell(entityColumn, row({ entities: value }, { cellOutcomes: { entities: 'withheld_unverified' } })),
      buildCell(entityColumn, row({ entities: null }), { pending: true }),
    ];

    for (const cell of synthetic) {
      expect(cell.cursor).toBeUndefined();
      expect(isExpandableEntityMentionCell(entityColumn, cell)).toBe(false);
    }
  });
});
