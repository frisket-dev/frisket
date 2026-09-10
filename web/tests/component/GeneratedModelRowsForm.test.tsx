// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ComponentProps } from 'react';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';

import { actionTemplatesFromCatalog } from '../../src/actions/model';
import { listProviders } from '../../src/api/open';
import type {
  ActionTemplate,
  GeneratedActionCatalogEntry,
  LocalProviderCatalog,
  LocalProviderEntry,
  RunEstimate,
} from '../../src/api/types';
import { GeneratedActionForm } from '../../src/components/action-panel/GeneratedActionForm';
import { completeCatalogPayload, sheetMeta } from '../support/actionFormFixtures';
import { syntheticActionCatalogEntry } from '../support/actionCatalogFixtures';
import { installPopoverPolyfill } from '../support/domPolyfills';
import { aiMeta, columnDef } from '../support/domainFixtures';

vi.mock('../../src/api/open', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/open')>();
  return { ...actual, listProviders: vi.fn() };
});

beforeAll(installPopoverPolyfill);

const RICH_TYPES = [
  'text',
  'timestamped_transcript',
  'category',
  'date',
  'number',
  'integer',
  'boolean',
  'json',
  'image',
  'file',
] as const;

const SHEET = sheetMeta([
  columnDef({ id: '13', name: 'attachment', type: 'file', ai: aiMeta() }),
  columnDef({ id: '11', name: 'body', type: 'text' }),
  columnDef({ id: '12', name: 'published', type: 'date' }),
  columnDef({ id: '14', name: 'source_url', type: 'link' }),
], { id: '7', name: 'Stories', rowCount: 4 });

function modelRowsEntry(kind: 'map.ask' | 'map.summarize'): GeneratedActionCatalogEntry {
  const summarize = kind === 'map.summarize';
  return syntheticActionCatalogEntry(kind, {
    title: summarize ? 'Summarize rows' : 'Ask a question of each row',
    input_schema: {
      type: 'object',
      additionalProperties: false,
      required: summarize ? ['source', 'model'] : ['source', 'model', 'question'],
      properties: {
        source: {
          title: 'Source',
          anyOf: [{ type: 'array', items: { type: 'string' }, minItems: 1 },
            { type: 'object', additionalProperties: false,
              required: ['text'], properties: { text: { type: 'string' } } }],
        },
        model: { type: 'string', title: 'Model' },
        ...(summarize ? {
          preset: {
            type: 'string',
            enum: ['paragraph', 'one_line', 'topics', 'quotes'],
            default: 'paragraph',
            title: 'Summary style',
          },
          instruction: { default: null, title: 'Custom instruction' },
        } : { question: { type: 'string', title: 'Question' } }),
        context: { type: 'string', default: '', title: 'Context' },
      },
    },
    required_capabilities: ['project:write', 'model:complete'],
    cost_policy: { kind: 'model_metered', requires_confirmation: true },
    ui_hints: {
      form: 'generated',
      category: 'text',
      semantic_controls: { source: 'rich_source', model: 'model' },
      source_requirements: [{
        id: 'source',
        mode: 'columns_or_template',
        param: 'source',
        label: 'Source',
        min: 1,
        accepted_column_types: [...RICH_TYPES],
        accepted_cell_kinds: ['text', 'blob', 'template'],
        template_columns: 'exact',
        unset_fallback_includes_ai_generated: true,
      }],
      logical_outputs: [{ key: summarize ? 'summary' : 'answer', column_type: 'text' }],
    },
  }) as GeneratedActionCatalogEntry;
}

function generatedTemplate(entry: GeneratedActionCatalogEntry): ActionTemplate {
  const template = actionTemplatesFromCatalog(
    completeCatalogPayload([entry]),
  ).find((candidate) => candidate.actionKind === entry.kind);
  if (!template) throw new Error(`Missing template for ${entry.kind}`);
  return template;
}

function catalog(modelId = 'anthropic/claude-haiku-4-5'): LocalProviderCatalog {
  const openai = modelId.startsWith('openai/');
  const provider: LocalProviderEntry = {
    id: modelId.split('/')[0],
    label: openai ? 'OpenAI' : 'Anthropic',
    kind: 'platform_api',
    configured: true,
    source: 'environment',
    hint: null,
    models: [{ id: modelId, label: openai ? 'GPT-5 mini' : 'Claude Haiku 4.5', price: null }],
  };
  return { schemaVersion: 'frisket.providers.v1', tier: 'local', providers: [provider] };
}

function deferred<T>(): { promise: Promise<T>; resolve(value: T): void } {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

function renderForm(
  entry: GeneratedActionCatalogEntry,
  options: Partial<ComponentProps<typeof GeneratedActionForm>> = {},
) {
  const onExecute = vi.fn();
  const resolveParams = vi.fn(async () => ({
    diagnostics: {},
    logical_outputs: entry.ui_hints.logical_outputs,
  }));
  const estimateAction = vi.fn(async () => ({
    cost: 0.01,
    rows: 4,
    billed_cost: 12_000,
    policy_id: 'test-policy',
  }));
  render(
    <GeneratedActionForm
      catalogEntry={entry}
      actionTemplate={generatedTemplate(entry)}
      sheet={SHEET}
      running={false}
      resolveParams={resolveParams}
      estimateAction={estimateAction}
      onExecute={onExecute}
      onClose={vi.fn()}
      {...options}
    />,
  );
  return { onExecute, resolveParams, estimateAction };
}

beforeEach(() => {
  vi.stubGlobal('crypto', { randomUUID: () => 'model-rows-request' });
  (listProviders as unknown as Mock).mockResolvedValue(catalog());
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe('generated model-row controls', () => {
  it('shows a quote for a priceable action even when this amount needs no confirmation', async () => {
    const entry = modelRowsEntry('map.ask');
    entry.cost_policy = { ...entry.cost_policy, requires_confirmation: false };
    const { estimateAction } = renderForm(entry);

    fireEvent.change(screen.getByTestId('action-prompt'), {
      target: { value: 'What changed?' },
    });
    fireEvent.change(screen.getByTestId('field-output-answer'), {
      target: { value: 'finding' },
    });

    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    await waitFor(() => expect(estimateAction).toHaveBeenCalled());
    expect(screen.getByTestId('cost-estimate')).toHaveTextContent('$0.01');
  });

  it('renders fresh Ask from one generated contract and submits its first rich source', async () => {
    const entry = modelRowsEntry('map.ask');
    const template = generatedTemplate(entry);
    expect(template.generatedAction).toBe(true);
    const { onExecute, estimateAction } = renderForm(entry);

    expect(screen.getByTestId('text-source-mode-columns')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByTestId('text-source-mode-all')).toHaveTextContent('All compatible columns');
    expect(screen.getByTestId('text-source-columns')).toHaveTextContent('body');
    expect(screen.getByTestId('text-source-columns')).not.toHaveTextContent('source_url');
    expect(screen.getByTestId('field-context')).toBeVisible();
    expect(screen.getByTestId('action-prompt')).toHaveValue('');
    expect(screen.getByText('Save answer to')).toBeVisible();
    expect(screen.getByTestId('action-prompt').compareDocumentPosition(
      screen.getByTestId('model-picker-button'),
    ) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();

    fireEvent.change(screen.getByTestId('action-prompt'), {
      target: { value: 'What changed?' },
    });
    fireEvent.change(screen.getByTestId('field-context'), {
      target: { value: 'Municipal planning records' },
    });
    fireEvent.change(screen.getByTestId('field-output-answer'), {
      target: { value: 'finding' },
    });

    await waitFor(() => expect(screen.getByTestId('model-picker-button'))
      .toHaveAccessibleName(/claude haiku/i));
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    await waitFor(() => expect(screen.getByTestId('cost-estimate')).toHaveTextContent('$0.01'));
    expect(estimateAction).toHaveBeenLastCalledWith(expect.objectContaining({
      action_id: 'map.ask',
      scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: expect.objectContaining({ model: 'anthropic/claude-haiku-4-5' }),
    }));
    // Preview remains an ordinary bounded attempt. If a selected provider has
    // a durable external effect, the server returns its standard refusal.
    expect(screen.getByTestId('generated-action-preview')).toBeEnabled();
    fireEvent.click(screen.getByTestId('generated-action-run'));

    expect(onExecute).toHaveBeenCalledWith({
      action_id: 'map.ask',
      scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: {
        source: ['body'],
        model: 'anthropic/claude-haiku-4-5',
        question: 'What changed?',
        context: 'Municipal planning records',
      },
      output_names: { answer: 'finding' },
      idempotency_key: 'web-map.ask:model-rows-request',
    }, 'run');
  });

  it('relaunches an exact plural-column Summarize draft visibly and unchanged', async () => {
    const entry = modelRowsEntry('map.summarize');
    const { onExecute } = renderForm(entry, {
      initialDraft: {
        action_id: 'map.summarize',
        scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [9, 3] },
        params: {
          source: ['attachment', 'body'],
          model: 'openrouter/minimax/minimax-m2.5',
          preset: 'quotes',
          instruction: null,
          context: 'A saved investigation',
        },
        output_names: { summary: 'key_quotes' },
      },
      selectedRowIds: ['9', '3'],
    });

    expect(screen.getByTestId('text-source-mode-columns')).toHaveAttribute('aria-pressed', 'true');
    expect(within(screen.getByTestId('text-source-columns')).getAllByRole('button')
      .map((node) => node.getAttribute('aria-label'))).toEqual([
        'Remove attachment',
        'Remove body',
      ]);
    expect(screen.getByTestId('field-preset')).toHaveValue('quotes');
    expect(screen.getByTestId('action-prompt')).toHaveValue('');
    expect(screen.getByTestId('field-context')).toHaveValue('A saved investigation');
    expect(screen.getByTestId('field-output-summary')).toHaveValue('key_quotes');

    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));

    expect(onExecute).toHaveBeenCalledWith({
      action_id: 'map.summarize',
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [9, 3] },
      params: {
        source: ['attachment', 'body'],
        model: 'openrouter/minimax/minimax-m2.5',
        preset: 'quotes',
        instruction: null,
        context: 'A saved investigation',
      },
      output_names: { summary: 'key_quotes' },
      idempotency_key: 'web-map.summarize:model-rows-request',
    }, 'run');
  });

  it('serializes Template mode as the same single source param and keeps explicit instructions', async () => {
    const entry = modelRowsEntry('map.summarize');
    const { onExecute } = renderForm(entry);

    fireEvent.click(screen.getByTestId('text-source-mode-template'));
    fireEvent.change(screen.getByTestId('text-source-template-input'), {
      target: { value: '{{published}} — {{body}} — {{published}}' },
    });
    fireEvent.click(screen.getByTestId('text-source-mode-columns'));
    expect(screen.getByTestId('text-source-columns')).toHaveTextContent('body');
    fireEvent.click(screen.getByTestId('text-source-mode-template'));
    expect(screen.getByTestId('text-source-template-input'))
      .toHaveValue('{{published}} — {{body}} — {{published}}');
    fireEvent.change(screen.getByTestId('action-prompt'), {
      target: { value: 'Lead with the newest fact.' },
    });

    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));

    expect(onExecute.mock.calls[0][0].params).toEqual({
      source: { text: '{{published}} — {{body}} — {{published}}' },
      model: 'anthropic/claude-haiku-4-5',
      preset: 'paragraph',
      instruction: 'Lead with the newest fact.',
      context: '',
    });
    expect(onExecute.mock.calls[0][0].params).not.toHaveProperty('input_columns');
    expect(onExecute.mock.calls[0][0].params).not.toHaveProperty('input_template');
  });

  it.each(['map.ask', 'map.summarize'] as const)('hydrates a saved literal Template object for %s without changing its Params', async (kind) => {
    const entry = modelRowsEntry(kind);
    const params = { source: { text: 'Literal source, not a column name' },
      model: 'anthropic/claude-haiku-4-5', context: 'Saved context',
      ...(kind === 'map.ask' ? { question: 'What changed?' }
        : { preset: 'quotes', instruction: null }) };
    const key = kind === 'map.ask' ? 'answer' : 'summary';
    const { onExecute } = renderForm(entry, { initialDraft: {
      action_id: kind, scope: { kind: 'sheet_rows', sheet_id: 7 },
      params, output_names: { [key]: 'saved_result' },
    } });
    expect(screen.getByTestId('text-source-template-input')).toHaveValue(params.source.text);
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute.mock.calls[0][0].params).toEqual(params);
  });

  it('re-estimates a metered scope selection before running that exact scope', async () => {
    const entry = modelRowsEntry('map.ask');
    const { onExecute, estimateAction } = renderForm(entry, {
      selectedRowIds: ['9', '3'],
      initialDraft: {
        action_id: 'map.ask',
        scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [9, 3] },
        params: {
          source: ['body'],
          model: 'anthropic/claude-haiku-4-5',
          question: 'What changed?',
          context: '',
        },
        output_names: { answer: 'finding' },
      },
    });

    await waitFor(() => expect(estimateAction).toHaveBeenLastCalledWith(expect.objectContaining({
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [9, 3] },
    })));
    fireEvent.click(screen.getByTestId('generated-action-run-scope-menu-button'));
    fireEvent.click(screen.getByTestId('generated-action-row-scope-all'));
    expect(onExecute).not.toHaveBeenCalled();
    await waitFor(() => expect(estimateAction).toHaveBeenLastCalledWith(expect.objectContaining({
      scope: { kind: 'sheet_rows', sheet_id: 7 },
    })));
    fireEvent.click(screen.getByTestId('generated-action-run'));

    expect(onExecute.mock.calls[0][0].scope).toEqual({ kind: 'sheet_rows', sheet_id: 7 });
  });

  it('keeps the newest server estimate when an older request settles late', async () => {
    const estimateA = deferred<RunEstimate>();
    const estimateB = deferred<RunEstimate>();
    const estimateAction = vi.fn()
      .mockImplementationOnce(() => estimateA.promise)
      .mockImplementationOnce(() => estimateB.promise);
    renderForm(modelRowsEntry('map.ask'), {
      estimateAction,
      initialDraft: {
        action_id: 'map.ask',
        scope: { kind: 'sheet_rows', sheet_id: 7 },
        params: {
          source: ['body'],
          model: 'anthropic/claude-haiku-4-5',
          question: 'What changed?',
          context: '',
        },
        output_names: { answer: 'finding' },
      },
    });

    await waitFor(() => expect(estimateAction).toHaveBeenCalledTimes(1));
    fireEvent.change(screen.getByTestId('field-output-answer'), {
      target: { value: 'revised_finding' },
    });
    await waitFor(() => expect(estimateAction).toHaveBeenCalledTimes(2));

    await act(async () => {
      estimateB.resolve({ cost: 1, rows: 4, billed_cost: 2_000_000, policy_id: 'new-policy' });
      await estimateB.promise;
    });
    expect(screen.getByTestId('cost-estimate')).toHaveTextContent('$2.00');

    await act(async () => {
      estimateA.resolve({ cost: 9, rows: 4, billed_cost: 9_000_000, policy_id: 'old-policy' });
      await estimateA.promise;
    });
    expect(screen.getByTestId('cost-estimate')).toHaveTextContent('$2.00');
    expect(screen.getByTestId('cost-estimate')).not.toHaveTextContent('$9.00');
  });

  it('renders the server commercial labels and reason for an unknown quote', async () => {
    renderForm(modelRowsEntry('map.summarize'), {
      estimateAction: vi.fn(async () => ({
        cost: 0.25,
        rows: 3,
        billed_cost: null,
        policy_id: 'hosted-offering',
        venue_label: 'Customer-selected compute venue',
        billing_label: 'Charged under customer contract',
        warning: 'This deployment has no tariff for the selected model',
      })),
    });

    await waitFor(() => expect(screen.getByTestId('cost-estimate-warning')).toHaveTextContent(
      'This deployment has no tariff for the selected model',
    ));
    expect(screen.getByTestId('cost-estimate')).toHaveTextContent('UNKNOWN');
    expect(screen.getByTestId('cost-estimate')).toHaveTextContent('3 rows');
    expect(screen.getByTestId('cost-estimate')).not.toHaveTextContent('$0.25');
    expect(screen.getByTestId('cost-venue-label')).toHaveTextContent(
      'Customer-selected compute venue',
    );
    expect(screen.getByTestId('cost-billing-label')).toHaveTextContent(
      'Charged under customer contract',
    );
  });

  it('keeps the normal immediate scope-menu run when the selected model estimates free', async () => {
    const estimateAction = vi.fn(async () => ({
      cost: 0,
      rows: 2,
      billed_cost: 0,
      policy_id: 'local-model',
    }));
    const entry = modelRowsEntry('map.ask');
    const { onExecute } = renderForm(entry, {
      estimateAction,
      selectedRowIds: ['9', '3'],
      initialDraft: {
        action_id: 'map.ask',
        scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [9, 3] },
        params: {
          source: ['body'],
          model: 'ollama/qwen3:8b',
          question: 'What changed?',
          context: '',
        },
        output_names: { answer: 'finding' },
      },
    });

    await waitFor(() => expect(screen.getByTestId('cost-estimate')).toHaveTextContent('$0.00'));
    fireEvent.click(screen.getByTestId('generated-action-run-scope-menu-button'));
    fireEvent.click(screen.getByTestId('generated-action-row-scope-all'));

    expect(onExecute.mock.calls[0][0].scope).toEqual({ kind: 'sheet_rows', sheet_id: 7 });
  });

  it('does not let a late provider default replace a user-picked model', async () => {
    const providers = deferred<LocalProviderCatalog>();
    // Child effects run before their parent's: ModelPicker gets the usable
    // catalog while the form-level default lookup remains deliberately late.
    (listProviders as unknown as Mock)
      .mockResolvedValueOnce(catalog('openai/gpt-5-mini'))
      .mockReturnValueOnce(providers.promise);
    const user = userEvent.setup();
    renderForm(modelRowsEntry('map.ask'));

    await waitFor(() => expect(listProviders).toHaveBeenCalledTimes(2));
    await user.click(screen.getByTestId('model-picker-button'));
    await user.type(screen.getByTestId('model-picker-search'), 'openai/gpt-5-mini');
    await user.click(screen.getByTestId('model-option-openai-gpt-5-mini'));
    await waitFor(() => expect(screen.getByTestId('model-picker-button'))
      .toHaveTextContent('GPT-5 mini'));

    await act(async () => {
      providers.resolve(catalog('anthropic/claude-haiku-4-5'));
      await providers.promise;
    });

    expect(screen.getByTestId('model-picker-button')).toHaveTextContent('GPT-5 mini');
  });
});
