// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import { listProviders } from '../../src/api/open';
import { sheetMeta } from '../support/actionFormFixtures';
import { columnDef } from '../support/domainFixtures';
import { installPopoverPolyfill } from '../support/domPolyfills';
import { chooseActionSelector, selectorTrigger } from '../support/selectorChoicesFixture';
import { mountTypedForm, servedTypedEntry, type TypedResolveParams } from './typedFormCutoverF2';

vi.mock('../../src/api/open', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/open')>();
  return { ...actual, listProviders: vi.fn() };
});

beforeAll(installPopoverPolyfill);
beforeEach(() => {
  vi.mocked(listProviders).mockResolvedValue({
    schemaVersion: 'frisket.providers.v1', tier: 'local', providers: [{
      id: 'openai', label: 'OpenAI', kind: 'platform_api', configured: true,
      source: 'env', hint: null,
      models: [{ id: 'openai/gpt-5-mini', label: 'GPT-5 mini', price: null }],
    }],
  });
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('No network in this test')));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const citationMessage = 'Clef does not support citations. Turn Citations Off or choose a compatible model.';
const fieldMessage = 'Clef supports category, boolean, and score fields only.';
const resolveExtract: TypedResolveParams = async ({ params }) => {
  const fields = params.fields as Array<{ name: string; type: string }>;
  const grounding = params.grounding as { enabled?: boolean } | undefined;
  if (params.engine !== 'llm') {
    if (fields.some((field) => !['category', 'boolean', 'score'].includes(field.type))) {
      return { diagnostics: { fields: { ok: false, message: fieldMessage } }, logical_outputs: [] };
    }
    if (grounding === undefined || grounding.enabled) {
      return { diagnostics: { grounding: { ok: false, message: citationMessage } }, logical_outputs: [] };
    }
  }
  return {
    diagnostics: {},
    logical_outputs: fields.map((field) => ({ key: field.name, column_type: field.type })),
  };
};

function mountExtract(type: 'boolean' | 'text') {
  return mountTypedForm({
    entry: servedTypedEntry('map.extract'),
    sheet: sheetMeta([columnDef({ id: '1', name: 'body', type: 'text' })], { id: '7', rowCount: 1 }),
    initialDraft: {
      action_id: 'map.extract', scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: {
        source: ['body'], engine: 'llm', model: 'openai/gpt-5-mini',
        fields: [{ name: 'finding', type, description: 'Is a finding present?', required: true }],
        grounding: { enabled: true },
      },
      output_names: { finding: 'Finding' },
    },
    resolveParams: resolveExtract,
    estimateAction: async () => ({ cost: 0, rows: 1, billed_cost: 0 }),
  });
}

describe('Extract decision engines', () => {
  it.each([
    ['clef', 'Clef (Cloudflare)'], ['clef-flash', 'Clef Flash'],
  ])('selects %s through the combined picker and requires explicit citation changes', async (engine, label) => {
    const user = userEvent.setup();
    const form = mountExtract('boolean');
    await chooseActionSelector(label);
    await waitFor(() => expect(selectorTrigger()).toHaveTextContent(label));

    expect(screen.queryByTestId('engine-select')).not.toBeInTheDocument();
    expect(screen.queryByTestId('model-select')).not.toBeInTheDocument();
    expect(screen.getByText(/Always selects an answer, even when the input is inconclusive/)).toBeVisible();
    expect(screen.getByText(/Does not return/)).toHaveTextContent('not found');
    expect(screen.getByTestId('field-citation_mode')).toHaveValue('cite');
    expect(await screen.findByText(citationMessage)).toBeVisible();
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();

    fireEvent.change(screen.getByTestId('field-citation_mode'), { target: { value: 'none' } });
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    await user.click(screen.getByTestId('generated-action-run'));
    expect(form.lastRequest()).toMatchObject({
      params: {
        engine, grounding: { enabled: false },
        fields: [{ name: 'finding', type: 'boolean', required: true }],
      },
      output_names: { finding: 'Finding' },
    });
    expect(form.lastRequest().params.model).toBeNull();
  });

  it('keeps incompatible fields visible with backend diagnostics and restores the model path', async () => {
    const form = mountExtract('text');
    await chooseActionSelector('Clef Flash');
    expect(await screen.findByText(fieldMessage)).toBeVisible();
    expect(screen.getByTestId('output-field-type')).toHaveTextContent('text');
    expect(screen.getByTestId('field-citation_mode')).toHaveValue('cite');
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    expect(form.resolveParams.mock.lastCall?.[0].params.fields).toEqual([
      expect.objectContaining({ name: 'finding', type: 'text', required: true }),
    ]);

    await chooseActionSelector('openai/gpt-5-mini');
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    expect(form.resolveParams.mock.lastCall?.[0].params).toMatchObject({
      engine: 'llm', model: 'openai/gpt-5-mini', grounding: { enabled: true },
      fields: [{ name: 'finding', type: 'text', required: true }],
    });
  });
});
