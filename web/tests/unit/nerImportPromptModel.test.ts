import { describe, expect, it } from 'vitest';
import type { ActionTemplate, ColumnDef } from '../../src/api/open';


import {
  describeColumns,
  nerImportOffer,
} from '../../src/components/nerImportPromptModel';

function column(name: string, over: Partial<ColumnDef> = {}): ColumnDef {
  return { id: name, name, type: 'text', ...over } as ColumnDef;
}

function nerTemplate(over: Partial<ActionTemplate> = {}): ActionTemplate {
  return {
    kind: 'map.ner',
    sourceRequirements: [
      { mode: 'columns', param: 'input_columns', accepted_column_types: ['text'] },
    ],
    engines: [{ id: 'spacy', label: 'spaCy', tier: 'local', available: true }],
    ...over,
  } as ActionTemplate;
}

describe('nerImportOffer', () => {
  it('offers when an import produced a readable text column', () => {
    const offer = nerImportOffer({
      columns: [column('body'), column('n', { type: 'number' })],
      rowCount: 34,
      nerTemplate: nerTemplate(),
    });
    expect(offer).toEqual({ columnNames: ['body'], rowCount: 34, free: true });
  });

  it('names only text columns after a files import — not the byte count or the blob', () => {
    // The nudge reads the SAME catalog requirement the panel does
    // (NerRecipe.source_requirements), so this is the import-side half of the
    // 2026-07-26 "Imported 4 rows of text in filename, size and 1 more" defect.
    const offer = nerImportOffer({
      columns: [
        column('filename'),
        column('ocr_text'),
        column('size', { type: 'integer' }),
        column('ocr_text_blocks', { type: 'json' }),
        column('media', { type: 'image' }),
      ],
      rowCount: 4,
      nerTemplate: nerTemplate({
        sourceRequirements: [
          {
            mode: 'columns',
            param: 'input_columns',
            accepted_column_types: ['text', 'timestamped_transcript'],
          },
        ],
      } as Partial<ActionTemplate>),
    });
    expect(offer?.columnNames).toEqual(['filename', 'ocr_text']);
    expect(describeColumns(offer!.columnNames)).toBe('filename and ocr_text');
  });

  it('is keyed on a readable column, not on the file being a PDF', () => {
    // A CSV with prose qualifies; a spreadsheet of numbers does not.
    expect(
      nerImportOffer({
        columns: [column('a', { type: 'number' }), column('b', { type: 'date' })],
        rowCount: 10,
        nerTemplate: nerTemplate(),
      }),
    ).toBeNull();
  });

  it('says nothing when the sheet already has an entity column', () => {
    expect(
      nerImportOffer({
        columns: [
          column('body'),
          column('entities', { type: 'json', semanticType: 'entity_mentions' }),
        ],
        rowCount: 12,
        nerTemplate: nerTemplate(),
      }),
    ).toBeNull();
  });

  it('says nothing when the default engine is known-unavailable', () => {
    // Offering something that can only fail on an install hint is worse than
    // not offering.
    expect(
      nerImportOffer({
        columns: [column('body')],
        rowCount: 12,
        nerTemplate: nerTemplate({
          engines: [
            { id: 'spacy', label: 'spaCy', tier: 'local', available: false },
          ],
        }),
      }),
    ).toBeNull();
  });

  it('still offers when the catalog has no engine information', () => {
    // Absent catalog is not evidence of absence.
    const offer = nerImportOffer({
      columns: [column('body')],
      rowCount: 3,
      nerTemplate: nerTemplate({ engines: undefined }),
    });
    expect(offer).not.toBeNull();
    expect(offer?.free).toBe(false);
  });

  it('does not claim free when the engine bills', () => {
    const offer = nerImportOffer({
      columns: [column('body')],
      rowCount: 3,
      nerTemplate: nerTemplate({
        engines: [
          {
            id: 'spacy',
            label: 'spaCy',
            tier: 'local',
            available: true,
            billable: true,
          },
        ],
      }),
    });
    expect(offer?.free).toBe(false);
  });

  it('says nothing without a catalog entry or without rows', () => {
    expect(
      nerImportOffer({ columns: [column('body')], rowCount: 5, nerTemplate: undefined }),
    ).toBeNull();
    expect(
      nerImportOffer({ columns: [column('body')], rowCount: 0, nerTemplate: nerTemplate() }),
    ).toBeNull();
  });
});

describe('describeColumns', () => {
  it('reads as a sentence at every length', () => {
    expect(describeColumns(['body'])).toBe('body');
    expect(describeColumns(['body', 'notes'])).toBe('body and notes');
    expect(describeColumns(['body', 'notes', 'a', 'b'])).toBe('body, notes and 2 more');
  });
});
