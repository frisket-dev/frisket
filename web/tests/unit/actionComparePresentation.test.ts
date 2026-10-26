import { describe, expect, it } from 'vitest';

import type { ActionCatalogEntry, GeneratedActionDraft } from '../../src/api/types';
import { createActionComparePresentation } from '../../src/workbench/actionComparePresentation';

function entry(): ActionCatalogEntry {
  return {
    kind: 'map.extract',
    authoring_contract_version: 1,
    title: 'Extract',
    description: 'Extract fields',
    input_schema: {
      type: 'object',
      properties: {
        source: { type: 'string', title: 'Documents' },
        engine: { type: 'string', title: 'Engine' },
        model: { type: 'string', title: 'Model' },
        instructions: { type: 'string', title: 'Instructions' },
        api_token: { type: 'string', title: 'API token', writeOnly: true },
        fields: {
          type: 'array',
          title: 'Fields',
          items: {
            type: 'object',
            properties: {
              name: { type: 'string', title: 'Name' },
              type: { type: 'string', title: 'Type' },
              description: { type: 'string', title: 'Description' },
            },
          },
        },
      },
    },
    output_schema: {},
    errors: [],
    side_effects: [],
    required_capabilities: [],
    required_credentials: [],
    cost_policy: {},
    idempotency: {},
    retry_policy: {},
    execution_mode: 'async',
    async_mode: 'preview',
    writes_project: true,
    examples: [],
    ui_hints: {
      form: 'generated',
      semantic_controls: { source: 'columns', engine: 'engine', model: 'model' },
      form_params: [
        { name: 'instructions', label: 'Extra context', type: 'textarea' },
      ],
      engines: [{ id: 'llm', label: 'Language model', tier: 'hosted' }],
      logical_outputs: [{ key: 'result', column_type: 'json' }],
    },
    receipt_policy: 'writes_receipt',
  } as ActionCatalogEntry;
}

function draft(params: Record<string, unknown>): GeneratedActionDraft {
  return {
    action_id: 'map.extract',
    scope: { kind: 'sheet_rows', sheet_id: 7 },
    params,
    output_names: { result: 'Extracted data' },
  } as GeneratedActionDraft;
}

describe('action comparison presentation', () => {
  it('uses the current selector choice for a short variant label and preserves the baseline label', () => {
    const presentation = createActionComparePresentation(entry(), {
      baselineMode: 'current_values',
      lockedParams: ['source', 'fields'],
      selectorChoices: {
        engine: [{
          label: 'Claude Sonnet 4.5',
          authored_selection: { kind: 'engine_model', engine: 'llm', model: 'anthropic/claude-sonnet-4-5' },
        }],
      },
    });
    const value = draft({ source: ['body'], engine: 'llm', model: 'anthropic/claude-sonnet-4-5' });

    expect(presentation.labelForDraft(value, 'A')).toBe('Current values');
    expect(presentation.labelForDraft(value, 'B')).toBe('Claude Sonnet 4.5');
  });

  it('describes editable changes with catalog labels while hiding sources, secrets, and output names', () => {
    const presentation = createActionComparePresentation(entry(), {
      baselineMode: 'preview',
      lockedParams: ['source'],
      selectorChoices: {
        engine: [
          { label: 'Qwen 3 4B', authored_selection: { kind: 'engine_model', engine: 'llm', model: 'local/qwen3-4b' } },
          { label: 'Claude Sonnet 4.5', authored_selection: { kind: 'engine_model', engine: 'llm', model: 'anthropic/claude-sonnet-4-5' } },
        ],
      },
    });
    const before = draft({
      source: ['body'], engine: 'llm', model: 'local/qwen3-4b',
      instructions: 'Return the parties.', api_token: 'secret-before',
    });
    const after = {
      ...draft({
        source: ['other'], engine: 'llm', model: 'anthropic/claude-sonnet-4-5',
        instructions: 'Return the parties and requested relief.', api_token: 'secret-after',
      }),
      output_names: { result: 'Renamed output' },
    };

    expect(presentation.describeVariantChanges(before, after)).toEqual([
      { label: 'Model', before: 'Qwen 3 4B', after: 'Claude Sonnet 4.5' },
      { label: 'Extra context', before: 'Return the parties.', after: 'Return the parties and requested relief.' },
    ]);
  });

  it('summarizes nested field description edits without dumping structured JSON', () => {
    const presentation = createActionComparePresentation(entry(), {
      baselineMode: 'preview',
      lockedParams: ['source', 'fields'],
    });
    const longDescription = `Include procedural posture and disposition. ${'More context. '.repeat(20)}`;
    const before = draft({ fields: [
      { name: 'defendant', type: 'text', description: 'Named defendant.' },
      { name: 'relief', type: 'category', description: 'Requested relief.',
        label_descriptions: { damages: 'Money damages.' } },
    ] });
    const after = draft({ fields: [
      { name: 'defendant', type: 'text', description: longDescription },
      { name: 'relief', type: 'category', description: 'Requested relief.',
        label_descriptions: { damages: 'Compensatory or punitive damages.' } },
    ] });

    const changes = presentation.describeVariantChanges(before, after);
    expect(changes).toHaveLength(2);
    expect(changes[0]?.label).toBe('defendant');
    expect(changes[0]?.before).toBe('Named defendant.');
    expect(changes[0]?.after.endsWith('…')).toBe(true);
    expect(changes[0]?.after.length).toBeLessThanOrEqual(96);
    expect(changes[0]?.after).not.toContain('{');
    expect(changes[1]).toEqual({
      label: 'Relief · Label descriptions',
      before: 'Previous descriptions',
      after: 'Updated descriptions',
    });
  });

  it('bounds the number of settings shown and falls back without exposing a selector id', () => {
    const catalog = entry();
    catalog.input_schema.properties = {
      ...catalog.input_schema.properties,
      alpha: { type: 'string' }, beta: { type: 'string' }, gamma: { type: 'string' },
    };
    const presentation = createActionComparePresentation(catalog, {
      baselineMode: 'preview',
      lockedParams: [],
      maxChanges: 2,
    });
    const opaque = 'provider/internal/4c9f8ee0d3c24f27841457a285a0288b';

    expect(presentation.labelForDraft(draft({ model: opaque }), 'B')).toBe('Variant B');
    expect(presentation.describeVariantChanges(
      draft({ alpha: 'one', beta: 'two', gamma: 'three' }),
      draft({ alpha: 'changed', beta: 'changed', gamma: 'changed' }),
    )).toHaveLength(2);
  });
});
