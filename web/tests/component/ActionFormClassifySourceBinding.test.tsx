// @vitest-environment jsdom
//
// Classify binds to an explicit source column list or a template, including
// image/blob columns for multimodal use. The typed GeneratedActionForm puts
// that binding on `params.source` of the request it hands to onExecute — a
// column-name array in Column(s)/All mode, `{ text }` in Template mode.
//
// The served catalog's source requirement for map.classify accepts text-like/
// category/date/number/boolean/json/image/file columns — NOT link or
// audio/video — which is what makes the requirement test real rather than a
// hardcoded JS list.
//
// Labels are a server requirement (frisket/actions/classify_types.py:
// "category fields require non-empty labels"); the form relays that verdict
// through resolveParams diagnostics, so the resolver here mirrors it.

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';

import type { GeneratedActionDraft, LocalProviderCatalog, SheetMeta } from '../../src/api/types';
import { columnDef } from '../support/domainFixtures';
import { sheetMeta } from '../support/actionFormFixtures';
import { chooseActionSelector, openActionSelector, selectorTrigger } from '../support/selectorChoicesFixture';
import { installPopoverPolyfill } from '../support/domPolyfills';
import {
  mountTypedForm,
  servedTypedEntry,
  type TypedEstimateAction,
  type TypedResolveParams,
} from './typedFormCutoverF2';

vi.mock('../../src/api/open', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../src/api/open')>();
  return { ...actual, listProviders: vi.fn() };
});

import { listProviders } from '../../src/api/open';

beforeAll(() => {
  installPopoverPolyfill();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

// The product's own copy, verbatim: ClassifyField refuses an unlabeled
// category field with "category fields require non-empty labels"
// (src/frisket/actions/classify_types.py:30); the resolve endpoint projects
// that Pydantic error onto the `fields` control with its nested path prefix
// (src/frisket/server/services/action_param_validation.py:57-84), so the
// diagnostic the form routes to the labels control by its structured path.
const CATEGORY_LABELS_REQUIRED = 'Add at least one label.';

interface ClassifyFieldParam {
  name: string;
  type?: string;
  labels?: string[];
}

/** Mirrors the server: a category field without labels is refused on
 *  `fields`; otherwise every field is one logical output keyed by its name. */
const resolveClassify: TypedResolveParams = async ({ params }) => {
  const fields = Array.isArray(params.fields) ? params.fields as ClassifyFieldParam[] : [];
  const unlabeledIndex = fields.findIndex((field) => (
    (field.type ?? 'category') === 'category' && !(field.labels?.length)
  ));
  if (unlabeledIndex >= 0) {
    return { diagnostics: { fields: {
      ok: false, code: 'category_labels_required', message: CATEGORY_LABELS_REQUIRED,
      path: [unlabeledIndex, 'labels'],
    } }, logical_outputs: [] };
  }
  return {
    diagnostics: {},
    logical_outputs: fields.map((field) => ({ key: field.name, column_type: field.type ?? 'category' })),
  };
};

// Provider cost and the server-rated billed quote deliberately differ: the
// panel must render the $0.02 billed figure, not the $0.01 input.
const estimateClassify: TypedEstimateAction = async () => ({
  cost: 0.01,
  rows: 1,
  llm: true,
  billed_cost: 20_000,
  policy_id: 'acme.cost_plus.v1',
});

function summarySheet(): SheetMeta {
  return sheetMeta([columnDef({ id: '1', name: 'summary', type: 'text' })], { id: '7' });
}

function mountClassify(options: {
  sheet: SheetMeta;
  entry?: ReturnType<typeof servedTypedEntry>;
  initialDraft?: GeneratedActionDraft;
}) {
  return mountTypedForm({
    entry: options.entry ?? servedTypedEntry('map.classify'),
    sheet: options.sheet,
    initialDraft: options.initialDraft,
    resolveParams: resolveClassify,
    estimateAction: estimateClassify,
  });
}

function savedClassifyDraft(
  fields: Array<Record<string, unknown>>,
  outputNames: Record<string, string> = {},
): GeneratedActionDraft {
  return {
    action_id: 'map.classify',
    scope: { kind: 'sheet_rows', sheet_id: 7 },
    params: { source: ['story'], fields },
    output_names: outputNames,
  };
}

beforeEach(() => {
  (listProviders as unknown as Mock).mockRejectedValue(new Error('no provider catalog'));
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('No provider request in this test')));
});

describe('classify source binding', () => {
  it('keeps untouched category labels clean, then routes their server error beside the labels control', async () => {
    const user = userEvent.setup();
    const { onExecute } = mountClassify({ sheet: summarySheet() });

    expect(screen.getByTestId('classify-labels')).toHaveValue('');
    expect(screen.queryByText('Housing')).not.toBeInTheDocument();
    // No destination is proposed until the server accepts the field set.
    expect(screen.queryByTestId(/^field-output-/)).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeDisabled());
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    expect(screen.queryByText(CATEGORY_LABELS_REQUIRED)).not.toBeInTheDocument();

    fireEvent.focus(screen.getByTestId('classify-labels'));
    fireEvent.change(screen.getByTestId('classify-labels'), { target: { value: ' ' } });
    expect(await screen.findByTestId('classify-labels-error'))
      .toHaveTextContent(CATEGORY_LABELS_REQUIRED);
    expect(screen.getAllByText(CATEGORY_LABELS_REQUIRED)).toHaveLength(1);

    await user.type(screen.getByTestId('classify-labels'), 'housing, transit, other');
    await waitFor(() => expect(screen.queryByTestId('classify-labels-error')).not.toBeInTheDocument());
    // The accepted field set derives the destination from the field name.
    await waitFor(() => expect(screen.getByTestId('field-output-category')).toHaveValue('category'));
    await screen.findByText('$0.02', {}, { timeout: 2000 });
    expect(screen.getByTestId('generated-action-run')).toBeEnabled();

    await user.click(screen.getByTestId('generated-action-run'));
    expect(onExecute).toHaveBeenCalledTimes(1);
    expect(onExecute.mock.calls[0][0]).toMatchObject({
      action_id: 'map.classify',
      scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: {
        source: ['summary'],
        fields: [{ name: 'category', type: 'category', labels: ['housing', 'transit', 'other'] }],
      },
      output_names: { category: 'category' },
    });
    expect(onExecute.mock.calls[0][0].idempotency_key).toContain('map.classify');
  });

  it('anchors a nested labels diagnostic to the indexed remote field', async () => {
    mountClassify({
      sheet: summarySheet(),
      initialDraft: {
        action_id: 'map.classify', scope: { kind: 'sheet_rows', sheet_id: 7 }, output_names: {},
        params: {
          source: ['summary'], engine: 'llm',
          fields: [
            { name: 'section', type: 'category', labels: ['news'] },
            { name: 'tone', type: 'category', labels: [] },
          ],
        },
      },
    });

    const labels = await screen.findAllByTestId('classify-labels');
    const error = await screen.findByTestId('classify-labels-error');
    expect(labels).toHaveLength(2);
    expect(labels[0]).not.toHaveAttribute('aria-describedby');
    expect(labels[1]).toHaveAttribute('aria-describedby', error.id);
    expect(error).toHaveTextContent(CATEGORY_LABELS_REQUIRED);
  });

  it('hydrates an omitted field description and derives the proposed destination', async () => {
    mountClassify({
      sheet: sheetMeta([columnDef({ id: '1', name: 'story', type: 'text' })], { id: '7' }),
      initialDraft: savedClassifyDraft([{
        name: 'news_beat',
        type: 'category',
        labels: ['government', 'business', 'other'],
      }]),
    });

    expect(screen.getByTestId('output-field-description')).toHaveValue('');
    expect(screen.getByTestId('classify-labels')).toHaveValue('government, business, other');
    // A saved request that omits output_names runs on the server under the
    // logical key (actions/core.py: output_names.get(key, key)); the form
    // must propose that same destination rather than a blank, blocked one.
    // RED (product): GeneratedActionForm's resolution effect compares the
    // saved output_names keys against the resolved outputs and refuses the
    // draft with "Saved action outputs no longer match its parameters."
    // whenever a valid saved request omits its output_names.
    await waitFor(() => expect(screen.getByTestId('field-output-news_beat')).toHaveValue('news_beat'));
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
  });

  it('keeps an explicit destination ahead of the proposed field name', async () => {
    mountClassify({
      sheet: sheetMeta([columnDef({ id: '1', name: 'story', type: 'text' })], { id: '7' }),
      initialDraft: savedClassifyDraft([{
        name: 'news_beat',
        type: 'category',
        labels: ['government', 'business', 'other'],
      }], { news_beat: 'reviewed_beat' }),
    });

    await waitFor(() => expect(screen.getByTestId('field-output-news_beat')).toHaveValue('reviewed_beat'));
  });

  it('offers the engine picker and captures the local semantic engine on the typed wire', async () => {
    const user = userEvent.setup();
    const { onExecute } = mountClassify({ sheet: summarySheet() });

    // The combined selector defaults to the local engine.
    await waitFor(() => expect(selectorTrigger()).toHaveTextContent('Local semantic'));
    expect(screen.queryByTestId('engine-select')).not.toBeInTheDocument();
    expect(screen.queryByTestId('model-select')).not.toBeInTheDocument();
    expect(screen.queryByTestId('field-minimum_similarity')).not.toBeInTheDocument();
    expect(screen.queryByTestId('field-minimum_margin')).not.toBeInTheDocument();
    expect(screen.queryByTestId('classify-label-details')).not.toBeInTheDocument();
    await user.type(screen.getByTestId('classify-labels'), 'Housing, Transit');
    const labelDetails = screen.getByTestId('classify-label-details');
    expect(labelDetails).not.toHaveAttribute('open');
    expect(screen.getByTestId('classify-label-descriptions')).not.toBeVisible();
    expect(screen.getByTestId('classify-labels')).toBeVisible();
    expect(screen.getByTestId('field-engine')).toBeVisible();
    await user.click(screen.getByText('More details'));
    expect(labelDetails).toHaveAttribute('open');
    expect(screen.getByTestId('classify-label-descriptions')).toBeVisible();
    await user.type(screen.getByLabelText('Housing semantic description'), 'Housing, zoning, rent, or homelessness.');
    expect(labelDetails).toHaveAttribute('open');
    const destination = await screen.findByTestId('field-output-category');
    await user.clear(destination);
    await user.type(destination, 'topic');
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    await user.click(screen.getByTestId('generated-action-run'));

    expect(onExecute).toHaveBeenCalledTimes(1);
    const request = onExecute.mock.calls[0][0];
    expect(request.params.engine).toBe('local_semantic');
    expect(request.params).not.toHaveProperty('minimum_similarity');
    expect(request.params).not.toHaveProperty('minimum_margin');
    expect(request.params.fields).toEqual([expect.objectContaining({
      name: 'category',
      labels: ['Housing', 'Transit'],
      label_descriptions: { Housing: 'Housing, zoning, rent, or homelessness.' },
    })]);
    expect(request.output_names).toEqual({ category: 'topic' });
  });

  it('opens More details when a restored classify draft already has label descriptions', async () => {
    mountClassify({
      sheet: summarySheet(),
      initialDraft: savedClassifyDraft([{
        name: 'topic',
        type: 'category',
        labels: ['Housing', 'Transit'],
        label_descriptions: { Housing: 'Housing, zoning, rent, or homelessness.' },
      }], { topic: 'topic' }),
    });

    expect(screen.getByTestId('classify-label-details')).toHaveAttribute('open');
    expect(screen.getByTestId('classify-label-descriptions')).toBeVisible();
    expect(screen.getByLabelText('Housing semantic description')).toHaveValue(
      'Housing, zoning, rent, or homelessness.',
    );
    expect(screen.getByTestId('classify-labels')).toBeVisible();
    expect(screen.queryByTestId('advanced-params')).not.toBeInTheDocument();
    expect(screen.queryByTestId('field-minimum_similarity')).not.toBeInTheDocument();
    expect(screen.queryByTestId('field-minimum_margin')).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
  });

  it('resolves a hosted model leaf to engine=llm plus the selected model', async () => {
    const user = userEvent.setup();
    const providerCatalog: LocalProviderCatalog = {
      schemaVersion: 'frisket.providers.v1',
      tier: 'local',
      providers: [{
        id: 'openai',
        label: 'OpenAI',
        kind: 'platform_api',
        configured: true,
        source: 'env',
        hint: null,
        models: [{ id: 'openai/gpt-5-mini', label: 'GPT-5 mini — fast/cheap', price: null }],
      }],
    };
    (listProviders as unknown as Mock).mockResolvedValue(providerCatalog);
    const { onExecute } = mountClassify({ sheet: summarySheet() });

    await waitFor(() => expect(listProviders).toHaveBeenCalled());
    await chooseActionSelector('openai/gpt-5-mini');

    expect(screen.queryByTestId('engine-select')).not.toBeInTheDocument();
    expect(screen.queryByTestId('model-select')).not.toBeInTheDocument();
    expect(screen.queryByTestId('field-minimum_similarity')).not.toBeInTheDocument();
    expect(screen.queryByTestId('field-minimum_margin')).not.toBeInTheDocument();
    await waitFor(() => expect(selectorTrigger()).toHaveTextContent('GPT-5 mini'));
    await user.type(screen.getByTestId('classify-labels'), 'news, opinion');
    const destination = await screen.findByTestId('field-output-category');
    await user.clear(destination);
    await user.type(destination, 'topic');
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    await user.click(screen.getByTestId('generated-action-run'));

    expect(onExecute).toHaveBeenCalledTimes(1);
    expect(onExecute.mock.calls[0][0]).toMatchObject({
      params: { engine: 'llm', model: 'openai/gpt-5-mini' },
      output_names: { category: 'topic' },
    });
  });

  it('inspects an unavailable llm model with its reason and keeps the local engine selected', async () => {
    (listProviders as unknown as Mock).mockResolvedValue({
      schemaVersion: 'frisket.providers.v1', tier: 'local', providers: [{
        id: 'openai', label: 'OpenAI', kind: 'platform_api', configured: true, source: 'env', hint: null,
        models: [{ id: 'openai/gpt-5-mini', label: 'GPT-5 mini', price: null }],
      }],
    } satisfies LocalProviderCatalog);
    const entry = servedTypedEntry('map.classify', (engines) => engines.map((engine) => (
      engine.id === 'llm'
        ? { ...engine, available: false, error: 'Configure a provider API key.' }
        : engine
    )));
    const { onExecute } = mountClassify({ sheet: summarySheet(), entry });

    await chooseActionSelector('openai/gpt-5-mini');
    expect(screen.getByTestId('engine-selector-dialog')).toBeVisible();
    expect(screen.getByText('Configure a provider API key.')).toBeVisible();
    expect(selectorTrigger()).toHaveTextContent('Local semantic');
    expect(onExecute).not.toHaveBeenCalled();
  });

  it('keeps provider models unavailable when the served roster omits the llm engine', async () => {
    (listProviders as unknown as Mock).mockResolvedValue({
      schemaVersion: 'frisket.providers.v1', tier: 'local', providers: [{
        id: 'openai', label: 'OpenAI', kind: 'platform_api', configured: true, source: 'env', hint: null,
        models: [{ id: 'openai/gpt-5-mini', label: 'GPT-5 mini', price: null }],
      }],
    } satisfies LocalProviderCatalog);
    const entry = servedTypedEntry('map.classify', (engines) => engines.filter((engine) => engine.id !== 'llm'));
    const { onExecute } = mountClassify({ sheet: summarySheet(), entry });

    await chooseActionSelector('openai/gpt-5-mini');
    expect(screen.getByText('The LLM engine is not offered')).toBeVisible();
    expect(selectorTrigger()).toHaveTextContent('Local semantic');
    expect(onExecute).not.toHaveBeenCalled();
  });

  it('retains an unknown saved fixed engine without inventing a tier', async () => {
    const draft = savedClassifyDraft([{ name: 'category', labels: ['news', 'opinion'] }]);
    draft.params = { ...draft.params, engine: 'retired_engine', model: 'legacy/model' };
    const { onExecute } = mountClassify({ sheet: summarySheet(), initialDraft: draft });

    const dialog = await openActionSelector();
    fireEvent.change(within(dialog).getByRole('searchbox', { name: /^Search / }), { target: { value: 'retired_engine' } });
    const option = within(dialog).getByRole('button', { name: /retired_engine/i });
    expect(option).not.toHaveTextContent(/local|sidecar|hosted/i);
    await userEvent.click(option);
    expect(within(dialog).getByText('Saved choice is not offered')).toBeVisible();
    expect(selectorTrigger()).toHaveTextContent('retired_engine');
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    expect(onExecute).not.toHaveBeenCalled();
  });

  it('submits only the selected source column by default', async () => {
    const user = userEvent.setup();
    const { onExecute } = mountClassify({
      sheet: sheetMeta([
        columnDef({ id: '1', name: 'headline', type: 'text' }),
        columnDef({ id: '2', name: 'city', type: 'text' }),
        columnDef({ id: '3', name: 'notes', type: 'text' }),
      ], { id: '7' }),
    });

    expect(screen.getByTestId('text-source-mode')).toBeVisible();
    expect(screen.getByTestId('text-source-mode-columns')).toHaveAttribute('aria-pressed', 'true');
    const picker = screen.getByTestId('text-source-columns');
    expect(within(picker).getByText('headline')).toBeVisible();
    expect(within(picker).queryByText('city')).not.toBeInTheDocument();
    expect(within(picker).queryByText('notes')).not.toBeInTheDocument();
    await user.type(screen.getByTestId('classify-labels'), 'news, opinion');
    const destination = await screen.findByTestId('field-output-category');
    await user.clear(destination);
    await user.type(destination, 'news_beat');
    await screen.findByText('$0.02', {}, { timeout: 2000 });

    await user.click(screen.getByTestId('generated-action-run'));
    expect(onExecute).toHaveBeenCalledTimes(1);
    const request = onExecute.mock.calls[0][0];
    expect(request.params.source).toEqual(['headline']);
    expect(request.output_names).toEqual({ category: 'news_beat' });
  });

  it('rich source picker follows catalog source requirements (excludes audio/link)', async () => {
    const user = userEvent.setup();
    mountClassify({
      sheet: sheetMeta([
        columnDef({ id: '1', name: 'headline', type: 'text' }),
        columnDef({ id: '2', name: 'clip', type: 'audio' }),
        columnDef({ id: '3', name: 'source_url', type: 'link' }),
      ], { id: '7' }),
    });

    fireEvent.mouseDown(screen.getByTestId('text-source-columns'));
    // The menu is a manual popover; jsdom reports its options as hidden.
    const menu = await screen.findByTestId('text-source-columns-menu');
    expect(within(menu).getAllByRole('option', { hidden: true })
      .map((option) => option.textContent)).toEqual(['headlinetext']);
    await user.keyboard('{Escape}');
    await user.click(screen.getByTestId('text-source-mode-all'));
    expect(screen.getByTestId('text-source-all-columns'))
      .toHaveTextContent('Reads the 1 compatible column, in order: headline.');
  });

  it('supports template inputs referencing multiple columns', async () => {
    const user = userEvent.setup();
    const { onExecute } = mountClassify({
      sheet: sheetMeta([
        columnDef({ id: '1', name: 'headline', type: 'text' }),
        columnDef({ id: '2', name: 'city', type: 'text' }),
      ], { id: '7' }),
    });

    await user.click(screen.getByTestId('text-source-mode-template'));
    const templateInput = screen.getByTestId('text-source-template-input');
    // fireEvent, not user.type: userEvent.type parses `{` as the start of a
    // special key sequence (e.g. `{enter}`), so a literal `{{column}}` token
    // can't be typed through it without doubling every brace.
    fireEvent.change(templateInput, { target: { value: '{{headline}} located in {{city}}' } });
    await user.type(screen.getByTestId('classify-labels'), 'local, national');
    const destination = await screen.findByTestId('field-output-category');
    await user.clear(destination);
    await user.type(destination, 'place_type');
    await screen.findByText('$0.02', {}, { timeout: 2000 });

    await user.click(screen.getByTestId('generated-action-run'));
    expect(onExecute).toHaveBeenCalledTimes(1);
    const request = onExecute.mock.calls[0][0];
    expect(request.params.source).toEqual({ text: '{{headline}} located in {{city}}' });
    expect(request.output_names).toEqual({ category: 'place_type' });
  });

  it('binds an image column as a multimodal source', async () => {
    const user = userEvent.setup();
    const { onExecute } = mountClassify({
      sheet: sheetMeta([
        columnDef({ id: '1', name: 'headline', type: 'text' }),
        columnDef({ id: '2', name: 'media', type: 'image' }),
      ], { id: '7' }),
    });

    await user.click(screen.getByRole('button', { name: 'Remove headline' }));
    fireEvent.mouseDown(screen.getByTestId('text-source-columns'));
    const menu = await screen.findByTestId('text-source-columns-menu');
    await user.click(within(menu).getByRole('option', { name: /media/, hidden: true }));
    await user.keyboard('{Escape}');
    expect(within(screen.getByTestId('text-source-columns')).getByText('media')).toBeVisible();
    await user.type(screen.getByTestId('classify-labels'), 'document, photo');
    const destination = await screen.findByTestId('field-output-category');
    await user.clear(destination);
    await user.type(destination, 'image_category');
    await screen.findByText('$0.02', {}, { timeout: 2000 });

    await user.click(screen.getByTestId('generated-action-run'));
    expect(onExecute).toHaveBeenCalledTimes(1);
    expect(onExecute.mock.calls[0][0].params.source).toEqual(['media']);
  });
});
