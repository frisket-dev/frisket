// @vitest-environment jsdom
//
// Translate's single-source/single-destination form shape (no output-fields
// builder, no "Save this setup") plus its source-language control and the
// run-scope split button's selected-row-only run, mounted through the typed
// GeneratedActionForm with the served map.translate catalog entry.
//
// The source-language rules are server validators (frisket/actions/
// translate_types.py: a no-auto engine such as opus_mt refuses an empty
// `language` with invalid_language_selection); the form relays that verdict
// through resolveParams diagnostics, so the resolver here mirrors it. An
// uninstalled Opus-MT pair is provisioned at run time by the server
// (src/frisket/ops/integrations/translation_engine.py:316-330 catches
// opus_mt.OpusPairNotInstalled and calls opus_mt.ensure_pair_installed —
// src/frisket/ops/integrations/opus_mt.py:202 — then retries once), so Run is
// gated on the PAIR BEING SET, not on it being installed.

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '../../src/api/open';
import type { EngineOption, GeneratedActionRequest, RunEstimate, SheetMeta } from '../../src/api/types';
import { columnDef } from '../support/domainFixtures';
import { sheetMeta } from '../support/actionFormFixtures';
import { installPopoverPolyfill } from '../support/domPolyfills';
import {
  mountTypedForm,
  servedTypedEntry,
  type TypedEstimateAction,
  type TypedResolveParams,
} from './typedFormCutoverF2';

beforeAll(installPopoverPolyfill);

beforeEach(() => {
  vi.spyOn(api, 'listProviders').mockResolvedValue({
    schemaVersion: 'frisket.providers.v1',
    tier: 'local',
    providers: [{ id: 'test', label: 'Test', kind: 'platform_api', configured: true, source: 'env',
      hint: null, models: [{ id: 'test/model', label: 'Test model', price: null }] }],
  });
  vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('No provider request in this test')));
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// The product's own copy, verbatim: TranslationOptions.normalize refuses a
// no-auto single-language engine with no source language via
// ValueError("invalid_language_selection")
// (src/frisket/actions/translate_types.py:63-67), raised from the
// TranslateParams model validator (src/frisket/actions/translate.py:58-60);
// the resolve endpoint strips Pydantic's "Value error, " prefix
// (src/frisket/server/services/action_param_validation.py:77-79). Note the
// server projects a MODEL-level validator onto the form-wide `__all__` key,
// not `language`; the mock keeps the field key so the diagnostic lands on
// the control, but the string is the product's.
const LANGUAGE_REQUIRED = 'invalid_language_selection';

/** Mirrors the server's language rules: a no-auto engine (opus_mt) refuses
 *  an unset source language; otherwise translate resolves one translation
 *  output plus detected_language when the sidecar is requested. */
const resolveTranslate: TypedResolveParams = async ({ params }) => {
  const engine = typeof params.engine === 'string' ? params.engine : 'llm';
  const language = Array.isArray(params.language) ? params.language : [];
  if (engine === 'opus_mt' && language.length === 0) {
    return { diagnostics: { language: { ok: false, message: LANGUAGE_REQUIRED } }, logical_outputs: [] };
  }
  return {
    diagnostics: {},
    logical_outputs: [
      { key: 'translation', column_type: 'text' },
      ...(params.save_detected_language === true
        ? [{ key: 'detected_language', column_type: 'category' }] : []),
    ],
  };
};

/** Server-rated quotes per engine: the llm engine is model-metered, a
 *  hosted MT engine carries its own venue/billing presentation. */
function estimateByEngine(quotes: Record<string, Partial<RunEstimate>>): TypedEstimateAction {
  return async (request: GeneratedActionRequest) => {
    const engine = typeof request.params.engine === 'string' ? request.params.engine : 'llm';
    return { cost: 0, rows: 3, llm: engine === 'llm', billed_cost: 0, policy_id: 'open', ...quotes[engine] };
  };
}

const LLM_QUOTE: Partial<RunEstimate> = { cost: 0.01, llm: true, billed_cost: 20_000, policy_id: 'acme.cost_plus.v1' };

function statementsSheet(): SheetMeta {
  return sheetMeta(
    [
      columnDef({ id: '1', name: 'statement', type: 'text' }),
      columnDef({ id: '2', name: 'speaker', type: 'text' }),
      columnDef({ id: '3', name: 'notes', type: 'text' }),
    ],
    { id: '7', rowCount: 3 },
  );
}

function mountTranslate(options: {
  engines?: (served: EngineOption[]) => EngineOption[];
  estimate?: TypedEstimateAction;
  selectedRowIds?: string[];
} = {}) {
  return mountTypedForm({
    entry: servedTypedEntry('map.translate', options.engines),
    sheet: statementsSheet(),
    selectedRowIds: options.selectedRowIds,
    resolveParams: resolveTranslate,
    estimateAction: options.estimate ?? estimateByEngine({ llm: LLM_QUOTE }),
  });
}

async function chooseFixedEngine(engineId: string): Promise<void> {
  const user = userEvent.setup();
  await user.click(screen.getByTestId('model-picker-button'));
  await user.type(screen.getByTestId('model-picker-search'), `engine:${engineId}`);
  await user.click(screen.getByTestId(`model-option-engine-${engineId.replace(/_/g, '-')}`));
}

async function runEnabled(): Promise<void> {
  await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
}

describe('translate form', () => {
  it('uses one source, one destination, and advanced sidecar toggles', async () => {
    const user = userEvent.setup();
    mountTranslate();
    const form = screen.getByTestId('generated-action-form');

    // The source label is "Content to translate" — "Translate from" labels the
    // source-LANGUAGE picker instead (asserted below via its own testid).
    expect(within(form).getByText('Content to translate')).toBeVisible();
    expect(within(form).queryByText('Target column')).not.toBeInTheDocument();
    expect(within(form).queryByTestId('output-fields-label')).not.toBeInTheDocument();
    expect(within(form).queryByTestId('target-column-select')).not.toBeInTheDocument();
    expect(within(form).queryByTestId('output-field-row')).not.toBeInTheDocument();
    expect(within(form).queryByText('Save this setup')).not.toBeInTheDocument();

    // The shared rich-source control: All / Column(s) / Template, defaulting
    // to the first compatible column, with every text column on offer.
    expect(within(form).getByTestId('text-source-mode-all')).toBeVisible();
    expect(within(form).getByTestId('text-source-mode-columns')).toHaveAttribute('aria-pressed', 'true');
    expect(within(form).getByTestId('text-source-mode-template')).toBeVisible();
    const picker = within(form).getByTestId('text-source-columns');
    expect(within(picker).getByText('statement')).toBeVisible();
    fireEvent.mouseDown(picker);
    const menu = await screen.findByTestId('text-source-columns-menu');
    expect(within(menu).getAllByRole('option', { hidden: true }).map((option) => option.textContent))
      .toEqual(expect.arrayContaining(['statementtext', 'speakertext', 'notestext']));
    await user.keyboard('{Escape}');

    // One destination, named after the resolved translation output.
    const destination = await within(form).findByTestId('field-output-translation');
    expect(destination).toHaveAccessibleName('Save to');
    expect(destination).toHaveValue('translation');
    expect(within(form).queryByTestId('field-output-detected_language')).not.toBeInTheDocument();
    await user.clear(destination);
    await user.type(destination, 'statement_spanish');
    expect(destination).toHaveValue('statement_spanish');

    // "Translate from" language picker — Auto-detect by default — and the
    // opt-in detected-language sidecar toggle (off by default).
    const sourceLanguage = within(form).getByTestId('translate-source-language-select') as HTMLSelectElement;
    expect(sourceLanguage).toHaveValue('auto');
    expect(within(form).getByTestId('field-save_detected_language')).not.toBeChecked();

    // One combined execution picker carries the default LLM model and fixed
    // translation engines; there is no two-step engine-then-model flow.
    expect(within(form).getByTestId('field-engine-model-choice')).toBeVisible();
    expect(within(form).getByTestId('model-picker-button')).toBeVisible();

    // Translate's confidence/justification toggles are gone — neither param
    // is in the served Params schema, so no control may offer them.
    expect(within(form).queryByTestId('advanced-run-options-toggle')).not.toBeInTheDocument();
    expect(within(form).queryByLabelText('Include confidence score')).not.toBeInTheDocument();
    expect(within(form).queryByLabelText('Include justification/explanation')).not.toBeInTheDocument();
    expect(within(form).queryByLabelText('Include confidence')).not.toBeInTheDocument();
    expect(within(form).queryByLabelText('Include justification')).not.toBeInTheDocument();
  });

  it('launches a template source verbatim on the typed wire', async () => {
    const user = userEvent.setup();
    const { onExecute } = mountTranslate();
    const form = screen.getByTestId('generated-action-form');

    await user.click(within(form).getByTestId('text-source-mode-template'));
    const composer = within(form).getByTestId('text-source-template-input');
    fireEvent.change(composer, {
      target: { value: '{{speaker}} says: {{statement}} ({{speaker}})' },
    });
    await runEnabled();
    await user.click(within(form).getByTestId('generated-action-run'));

    expect(onExecute).toHaveBeenCalledTimes(1);
    const request = onExecute.mock.calls[0][0];
    expect(request.action_id).toBe('map.translate');
    // The template rides as `{ text }`; the server derives its column set.
    expect(request.params.source).toEqual({ text: '{{speaker}} says: {{statement}} ({{speaker}})' });
    expect(request.params).not.toHaveProperty('input_columns');
    expect(request.params).not.toHaveProperty('input_template');
  });

  it('drops the LLM cost/model surfaces when a hosted engine is selected', async () => {
    const user = userEvent.setup();
    const { onExecute, estimateAction } = mountTranslate({
      estimate: estimateByEngine({
        llm: LLM_QUOTE,
        deepl: { cost: null, llm: false, billed_cost: 5_000, policy_id: 'acme.cost_plus.v1',
          venue_label: 'DeepL', billing_label: 'Billed per character' },
      }),
    });
    const form = screen.getByTestId('generated-action-form');

    // Default LLM model and dataset-context guidance are present,
    // model-dollar estimate shown.
    expect(within(form).getByTestId('model-picker-button')).toBeVisible();
    expect(within(form).getByTestId('field-context')).toBeVisible();
    await waitFor(() => expect(within(form).getByTestId('cost-estimate'))
      .toHaveTextContent('Estimated cost: $0.02'), { timeout: 2000 });

    // Switch to the hosted DeepL engine.
    await chooseFixedEngine('deepl');

    // The same picker now shows DeepL; no LLM guidance remains and the quote
    // is re-rated for the fixed engine
    // and carries the engine's own venue/billing presentation.
    expect(within(form).getByTestId('model-picker-button')).toHaveTextContent('DeepL');
    expect(within(form).queryByTestId('field-context')).not.toBeInTheDocument();
    await waitFor(() => expect(estimateAction!.mock.lastCall?.[0].params.engine).toBe('deepl'), { timeout: 2000 });
    const cost = within(form).getByTestId('cost-estimate');
    await waitFor(() => expect(cost).toHaveTextContent('Billed per character'), { timeout: 2000 });
    expect(within(cost).getByTestId('cost-venue-label')).toHaveTextContent('DeepL');
    expect(cost).not.toHaveTextContent('$0.02');

    await runEnabled();
    await user.click(within(form).getByTestId('generated-action-run'));
    const request = onExecute.mock.calls[0][0];
    expect(request.params.engine).toBe('deepl');
    expect(request.params).not.toHaveProperty('model');
    expect(request.params).not.toHaveProperty('context');
  });

  it('renders the source-language control per the engine declaration (no-auto Opus-MT)', async () => {
    mountTranslate();
    const form = screen.getByTestId('generated-action-form');

    // llm: an Auto-first single picker over EXACTLY the served choices, in
    // served order (EngineLanguageControl prepends Auto when allows_auto).
    const servedChoices = servedTypedEntry('map.translate').ui_hints.engines
      ?.find((engine) => engine.id === 'llm')?.language?.choices ?? [];
    expect(servedChoices.length).toBeGreaterThan(0);
    expect(servedChoices.map((choice) => choice.value)).toContain('es');
    const llmSelect = within(form).getByTestId('translate-source-language-select') as HTMLSelectElement;
    const values = Array.from(llmSelect.options, (option) => option.value);
    expect(values).toEqual(['auto', ...servedChoices.map((choice) => choice.value)]);
    expect(llmSelect).toHaveValue('auto');

    // Switch to Opus-MT (a pair engine): the generic source picker is REPLACED
    // by the Google-Translate-style paired pair picker.
    await chooseFixedEngine('opus_mt');
    expect(within(form).getByTestId('translate-pair-picker')).toBeInTheDocument();
    expect(within(form).queryByTestId('translate-source-language-select')).not.toBeInTheDocument();
  });

  it('renders a safe Auto picker (and no detected-language note) when the declaration is absent', async () => {
    // A version-skewed / cached catalog entry that lost its `language` field
    // must not silently drop the source control nor promise a detected-language
    // column. The control falls back to a single Auto-first picker; the note is
    // gated on detects === true, so an absent declaration hides it.
    mountTranslate({
      engines: (served) => served.filter((engine) => engine.id === 'llm').map((engine) => {
        const withoutLanguage = { ...engine };
        delete withoutLanguage.language;
        return withoutLanguage;
      }),
    });
    const form = screen.getByTestId('generated-action-form');

    const select = within(form).getByTestId('translate-source-language-select') as HTMLSelectElement;
    expect(Array.from(select.options, (option) => option.value)).toEqual(['auto']);
    expect(select).toHaveValue('auto');
    expect(within(form).queryByTestId('translate-source-language-required')).not.toBeInTheDocument();
    expect(within(form).queryByTestId('translate-detected-language-note')).not.toBeInTheDocument();
  });

  it('run split button owns selected row scope', async () => {
    const user = userEvent.setup();
    const { onExecute } = mountTranslate({
      selectedRowIds: ['10', '30'],
      // A free quote lets the split-button menu launch on scope selection.
      estimate: estimateByEngine({}),
    });
    const form = screen.getByTestId('generated-action-form');

    const destination = await within(form).findByTestId('field-output-translation');
    await user.clear(destination);
    await user.type(destination, 'statement_spanish');
    expect(within(form).getByText('2 selected rows will run')).toBeVisible();
    expect(within(form).queryByTestId('generated-action-row-scope-selected')).not.toBeInTheDocument();
    await runEnabled();
    await waitFor(() => expect(within(form).getByTestId('cost-estimate'))
      .toHaveTextContent('Estimated cost: $0.00'), { timeout: 2000 });

    await user.click(within(form).getByTestId('generated-action-run-scope-menu-button'));
    const menu = within(form).getByTestId('generated-action-run-scope-menu');
    expect(menu).toBeVisible();
    expect(within(menu).getByTestId('generated-action-row-scope-all')).toHaveTextContent('Run all');
    expect(within(menu).getByTestId('generated-action-row-scope-selected')).toHaveTextContent('Run on selected');
    expect(within(menu).getByTestId('generated-action-row-scope-selected')).toHaveTextContent('2 selected');

    await user.click(within(menu).getByTestId('generated-action-row-scope-selected'));
    expect(onExecute).toHaveBeenCalledTimes(1);
    const [request, intent] = onExecute.mock.calls[0];
    expect(intent).toBe('run');
    expect(request).toMatchObject({
      action_id: 'map.translate',
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [10, 30] },
      params: { source: ['statement'], engine: 'llm', target_language: 'English' },
      output_names: { translation: 'statement_spanish' },
    });
    expect(request.idempotency_key).toContain('map.translate');
    expect(request).not.toHaveProperty('canonicalAction');
  });
});

describe('opus_mt pair run-gating', () => {
  function withInstalledPairs(models: string[]) {
    return (served: EngineOption[]) => served.map((engine) => (
      engine.id === 'opus_mt' ? { ...engine, models: models.length ? models : undefined } : engine
    ));
  }

  it('blocks Run until the selected pair is set, then allows it', async () => {
    const user = userEvent.setup();
    // No pair installed: engine selectable, Run gated on the server's
    // language verdict until a pair is chosen.
    const { onExecute } = mountTranslate({ engines: withInstalledPairs([]) });
    let form = screen.getByTestId('generated-action-form');
    await chooseFixedEngine('opus_mt');
    // the paired picker renders (engine WAS selectable despite zero pairs)
    expect(within(form).getByTestId('translate-pair-picker')).toBeInTheDocument();
    await waitFor(() => expect(within(form).getByTestId('generated-action-run'))
      .toHaveAttribute('title', LANGUAGE_REQUIRED));
    expect(within(form).getByTestId('generated-action-run')).toBeDisabled();
    expect(within(form).getAllByText(LANGUAGE_REQUIRED).length).toBeGreaterThan(0);

    await user.selectOptions(within(form).getByTestId('translate-pair-source'), 'en');
    await user.selectOptions(within(form).getByTestId('translate-pair-target'), 'es');
    // The pair is not installed: the inline download is offered, and the
    // server provisions it at run time, so Run is no longer gated on it.
    expect(within(form).getByTestId('translate-pair-download')).toBeInTheDocument();
    expect(within(form).queryByTestId('translate-pair-installed')).not.toBeInTheDocument();
    await runEnabled();
    await user.click(within(form).getByTestId('generated-action-run'));
    expect(onExecute).toHaveBeenCalledTimes(1);
    expect(onExecute.mock.calls[0][0]).toMatchObject({
      action_id: 'map.translate',
      params: { source: ['statement'], engine: 'opus_mt', language: ['en'], target_language: 'es' },
    });
    expect(onExecute.mock.calls[0][0].params).not.toHaveProperty('model');
    cleanup();

    // Same selection with the pair installed (engine.models) -> badge + Run.
    mountTranslate({ engines: withInstalledPairs(['en-es']) });
    form = screen.getByTestId('generated-action-form');
    await chooseFixedEngine('opus_mt');
    await user.selectOptions(within(form).getByTestId('translate-pair-source'), 'en');
    await user.selectOptions(within(form).getByTestId('translate-pair-target'), 'es');
    expect(within(form).getByTestId('translate-pair-installed')).toBeInTheDocument();
    expect(within(form).queryByTestId('translate-pair-download')).not.toBeInTheDocument();
    await runEnabled();
  });
});
