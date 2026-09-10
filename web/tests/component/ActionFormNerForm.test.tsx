// @vitest-environment jsdom
//
// The typed `map.ner` form contract, mounted through GeneratedActionForm with
// the backend-served catalog entry:
//   C1  a real three-mode source picker (All / Column(s) / { } Template) whose
//       "All" is expanded to an EXPLICIT ordered `params.source` list at
//       submit, and whose template mode posts `params.source = { text }`.
//   C2  one output control ("Save entities to") writing `output_names.entities`.
//   C4a the entity-type chips render the 17 CANONICAL types, so the value
//       written into `params.labels` is `location`, never a raw `GPE` tag.
//   C4b an empty selection is INVALID (it never meant "all types").
// Plus engine-aware cost gating: the cost line shows the SERVER's rated bill
// for the LLM engine and the panel launches unconfirmed.

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ActionParamResolution, GeneratedActionDraft, GeneratedActionRequest } from '../../src/api/types';
import { aiMeta, columnDef } from '../support/domainFixtures';
import { sheetMeta } from '../support/actionFormFixtures';
import { installPopoverPolyfill } from '../support/domPolyfills';
import {
  chooseEngine,
  lastExecutedRequest,
  mockLocalProviders,
  mountTypedForm,
  pressRun,
  type MountTypedFormOptions,
} from './cutoverUiF1TypedForm';

const RECOMMENDED_LABELS = ['person', 'organization', 'location', 'date', 'money'];
const ALL_CANONICAL_LABELS = [
  'person', 'organization', 'location', 'date', 'money', 'product', 'event', 'law',
  'work_of_art', 'language', 'norp', 'fac', 'time', 'percent', 'quantity', 'ordinal',
  'cardinal',
];

let dispose: (() => void) | undefined;
afterEach(() => {
  cleanup();
  dispose?.();
  dispose = undefined;
  vi.restoreAllMocks();
});

beforeEach(() => {
  // The Column(s) mode's MultiColumnPicker menu is a real top-layer popover
  // (useNativePopover); jsdom has no Popover API.
  installPopoverPolyfill();
  mockLocalProviders();
});

/** A sheet whose NER-compatible columns are headline, body, notes — `lat`
 *  (geo_point) is not. */
function nerSheet() {
  return sheetMeta([
    columnDef({ id: '1', name: 'headline', type: 'text' }),
    columnDef({ id: '2', name: 'body', type: 'text' }),
    columnDef({ id: '3', name: 'notes', type: 'text' }),
    columnDef({ id: '4', name: 'lat', type: 'geo_point' }),
  ], { id: '7', rowCount: 3 });
}

/** A files sheet, where a naive "all columns" default once expanded to
 *  "filename, ocr_text, size, ocr_text_blocks, media" — a byte count and a
 *  blob handle included. */
function filesSheet() {
  return sheetMeta([
    columnDef({ id: '1', name: 'filename', type: 'text' }),
    columnDef({ id: '2', name: 'ocr_text', type: 'text' }),
    columnDef({ id: '3', name: 'size', type: 'integer' }),
    columnDef({ id: '4', name: 'ocr_text_blocks', type: 'json' }),
    columnDef({ id: '5', name: 'media', type: 'image' }),
  ], { id: '7', rowCount: 3 });
}

function savedNer(params: Record<string, unknown>): GeneratedActionDraft {
  return {
    action_id: 'map.ner',
    scope: { kind: 'sheet_rows', sheet_id: 7 },
    params: { source: ['headline'], engine: 'spacy', labels: RECOMMENDED_LABELS, ...params },
    output_names: { entities: 'entities' },
  };
}

const NER_OUTPUTS: ActionParamResolution['logical_outputs'] = [{ key: 'entities', column_type: 'json' }];

/** The server refuses an empty `labels` (NerParams: minItems 1) and a source
 *  naming a column the sheet no longer has. The typed form defers those
 *  verdicts to resolveParams. Tests that pass THIS resolver are asserting
 *  how the form surfaces a server diagnostic — the diagnostic itself is the
 *  mock's, so they prove nothing about client-side checks (the default
 *  resolver in mountNer is permissive so client gates stand alone). */
function nerResolver(sheetColumns: string[]): NonNullable<MountTypedFormOptions['resolveParams']> {
  return async ({ params }) => {
    const diagnostics: ActionParamResolution['diagnostics'] = {};
    if (Array.isArray(params.labels) && params.labels.length === 0) {
      diagnostics.labels = { ok: false, message: 'Pick at least one entity type.' };
    }
    if (Array.isArray(params.source)
      && params.source.some((name) => !sheetColumns.includes(String(name)))) {
      diagnostics.source = { ok: false, message: 'Unknown source column.' };
    }
    return { diagnostics, logical_outputs: NER_OUTPUTS };
  };
}

function mountNer(options: Partial<MountTypedFormOptions> = {}) {
  const sheet = options.sheet ?? nerSheet();
  const mounted = mountTypedForm({
    kind: 'map.ner',
    sheet,
    resolveParams: async () => ({ diagnostics: {}, logical_outputs: NER_OUTPUTS }),
    ...options,
  });
  dispose = mounted.dispose;
  return mounted;
}

/** Mount with the server-mirroring resolver (see nerResolver). */
function mountNerWithServerDiagnostics(options: Partial<MountTypedFormOptions> = {}) {
  const sheet = options.sheet ?? nerSheet();
  return mountNer({ sheet, resolveParams: nerResolver(sheet.columns.map((column) => column.name)), ...options });
}

/** The typed RunScopeFooter carries its disabled reason only as the Run
 *  button's `title` (the legacy form rendered a visible `run-disabled-reason`
 *  line — an affordance the typed footer does not have). Waiting on the title
 *  also gets past the transient "Validating fields…" state, during which
 *  Run is disabled for an unrelated reason. */
async function expectRunBlockedByForm(): Promise<void> {
  await waitFor(() => expect(screen.getByTestId('generated-action-run'))
    .toHaveAttribute('title', 'Complete the required fields.'));
  expect(screen.getByTestId('generated-action-run')).toBeDisabled();
}

async function selectAllSource(): Promise<void> {
  await userEvent.click(screen.getByTestId('text-source-mode-all'));
}

describe('engine-aware cost gating', () => {
  it('renders the LLM engine\'s extra_instructions as a textarea', async () => {
    mountNer();
    expect(screen.queryByTestId('field-extra_instructions')).not.toBeInTheDocument();
    await chooseEngine('llm');
    expect(screen.getByTestId('field-extra_instructions').tagName).toBe('TEXTAREA');
  });

  it('shows the SERVER price for the LLM engine and launches unconfirmed', async () => {
    const user = userEvent.setup();
    const { onExecute, estimateAction } = mountNer({
      estimateAction: async (request) => (
        request.params.engine === 'llm'
          // The displayed $2.00 is the deployment's rated bill, deliberately
          // different from the provider cost so this remains a wire-cut test.
          ? { cost: 1.25, rows: 3, billed_cost: 2_000_000, policy_id: 'acme.cost_plus.v1' }
          : { cost: 0, rows: 3, billed_cost: 0, policy_id: 'frisket.identity.v1' }
      ),
    });

    await waitFor(() => {
      expect(screen.getByTestId('cost-estimate')).toHaveClass('cost-local');
      expect(screen.getByTestId('cost-estimate')).toHaveTextContent('$0.00');
    });
    expect(screen.getByTestId('model-picker-button')).toBeInTheDocument();

    await user.click(screen.getByTestId('model-picker-button'));
    await user.type(screen.getByTestId('model-picker-search'), 'test/model');
    await user.click(await screen.findByTestId('model-option-test-model'));
    await waitFor(() => {
      expect(estimateAction).toHaveBeenLastCalledWith(
        expect.objectContaining({ action_id: 'map.ner', params: expect.objectContaining({ engine: 'llm' }) }),
      );
      // The panel can only project unconfirmed requests.
      expect(estimateAction.mock.lastCall?.[0]).not.toHaveProperty('confirmed');
      expect(screen.getByTestId('cost-estimate')).toHaveTextContent('Estimated cost: $2.00');
    });
    expect(screen.getByTestId('cost-estimate')).toHaveClass('cost-paid');

    // $2.00 is over the server's $1 gate, but the PANEL does not know that and
    // does not pretend to: it launches, and the server's 402 raises the modal
    // (src/state/jobStore.test.ts). The number above is the server's own
    // estimate, shown so the click is informed — not a second opinion.
    await pressRun();
    expect(screen.queryByTestId('cost-gate-modal')).not.toBeInTheDocument();
    expect(onExecute).toHaveBeenCalledTimes(1);
    const request = lastExecutedRequest(onExecute);
    expect(request).not.toHaveProperty('confirmed');
    expect(request.params.engine).toBe('llm');
    expect(request.idempotency_key).toEqual(expect.stringContaining('map.ner'));
  });

  it('retains hidden engine-specific values locally across engine switches', async () => {
    const user = userEvent.setup();
    mountNer();

    await chooseEngine('gliner');
    fireEvent.change(screen.getByTestId('field-threshold'), { target: { value: '0.73' } });

    await chooseEngine('llm');
    await user.type(screen.getByTestId('field-extra_instructions'), 'Prefer public agencies.');

    await chooseEngine('spacy');
    expect(screen.queryByTestId('field-threshold')).not.toBeInTheDocument();
    expect(screen.queryByTestId('field-extra_instructions')).not.toBeInTheDocument();

    await chooseEngine('gliner');
    expect(screen.getByTestId('field-threshold')).toHaveValue('0.73');
    await chooseEngine('llm');
    expect(screen.getByTestId('field-extra_instructions')).toHaveValue('Prefer public agencies.');
  });

  it.each(['0.73', '0.703'])('accepts threshold %s typed keystroke by keystroke', async (value) => {
    const user = userEvent.setup();
    mountNer();

    await chooseEngine('gliner');
    await user.clear(screen.getByTestId('field-threshold'));
    await user.type(screen.getByTestId('field-threshold'), value);
    expect(screen.getByTestId('field-threshold')).toHaveValue(value);
  });

  it('omits hidden engine-specific values from estimate and run', async () => {
    const user = userEvent.setup();
    const { onExecute, estimateAction } = mountNer();

    await chooseEngine('gliner');
    fireEvent.change(screen.getByTestId('field-threshold'), { target: { value: '0.73' } });
    await chooseEngine('llm');
    await user.type(screen.getByTestId('field-extra_instructions'), 'Prefer public agencies.');
    await chooseEngine('spacy');

    await waitFor(() => {
      const estimate = estimateAction.mock.lastCall?.[0];
      expect(estimate).toHaveProperty('params.engine', 'spacy');
      expect(estimate).not.toHaveProperty('params.threshold');
      expect(estimate).not.toHaveProperty('params.extra_instructions');
    });
    const estimated = estimateAction.mock.lastCall?.[0] as GeneratedActionRequest;

    await pressRun();
    const ran = { ...lastExecutedRequest(onExecute), idempotency_key: undefined };
    const quoted = { ...estimated, idempotency_key: undefined };
    expect(ran).toEqual(quoted);
  });
});

// ---------------------------------------------------------------- C2

describe('C2 — one output control', () => {
  it('renders "Save entities to" and NOT a duplicate generic output param', () => {
    mountNer();

    expect(screen.getByLabelText('Save entities to')).toHaveValue('entities');
    expect(screen.getByTestId('field-output-entities')).toHaveValue('entities');
    expect(screen.queryByTestId('field-output_name')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Output Name')).not.toBeInTheDocument();
  });

  it('offers exactly one control writing the output column name', () => {
    const { container } = mountNer();

    const outputControls = container.querySelectorAll(
      'input[data-testid^="field-output-"], [data-testid="field-output_name"], [data-testid="new-column-name"]',
    );
    expect(outputControls).toHaveLength(1);
    expect(outputControls[0]).toHaveAttribute('data-testid', 'field-output-entities');
  });

  it('projects an edited fresh Save-to name into the typed request', async () => {
    const user = userEvent.setup();
    const { onExecute } = mountNer();

    await user.clear(screen.getByTestId('field-output-entities'));
    await user.type(screen.getByTestId('field-output-entities'), 'named_entities');
    await pressRun();

    const request = lastExecutedRequest(onExecute);
    expect(request.output_names).toEqual({ entities: 'named_entities' });
    expect(request).not.toHaveProperty('targetColumnId');
    expect(request).not.toHaveProperty('canonicalAction');
    expect(request.params).not.toHaveProperty('output_name');
  });
});

describe('exact row scope output', () => {
  function sheetWithExistingOutput() {
    return sheetMeta([
      columnDef({ id: '1', name: 'body', type: 'text' }),
      columnDef({ id: '2', name: 'prior_entities', type: 'json', ai: aiMeta(), generationManaged: true }),
    ], { id: '7', rowCount: 3 });
  }

  it('allows compatible replacement without widening an exact row selection', async () => {
    const user = userEvent.setup();
    const { onExecute } = mountNer({
      sheet: sheetWithExistingOutput(),
      selectedRowIds: ['1'],
      hasExactRowScopeInitializer: true,
    });

    const saveTo = screen.getByTestId('field-output-entities');
    await user.click(saveTo);
    await user.click(screen.getByTestId('field-output-entities-option-prior-entities'));
    expect(screen.getByTestId('save-to-overwrite-warning')).toBeVisible();
    await pressRun();
    const request = lastExecutedRequest(onExecute);
    expect(request.scope).toEqual({ kind: 'sheet_rows', sheet_id: 7, row_ids: [1] });
    expect(request.output_names).toEqual({ entities: 'prior_entities' });
  });

  it('retains existing-column targets for all rows', async () => {
    const user = userEvent.setup();
    const { onExecute } = mountNer({
      sheet: sheetWithExistingOutput(),
      selectedRowIds: ['1'],
      hasExactRowScopeInitializer: true,
      // A priced estimate keeps the split-button menu a scope SELECTOR rather
      // than an immediate launch, so the scope can change before Run.
      estimateAction: async () => ({ cost: 0.01, rows: 3, billed_cost: 10_000, policy_id: 'acme.cost_plus.v1' }),
    });

    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    await user.click(screen.getByTestId('generated-action-run-scope-menu-button'));
    await user.click(screen.getByTestId('generated-action-row-scope-all'));
    expect(onExecute).not.toHaveBeenCalled();
    await user.click(screen.getByTestId('field-output-entities'));
    await user.click(screen.getByTestId('field-output-entities-option-prior-entities'));
    expect(screen.getByTestId('save-to-overwrite-warning')).toBeVisible();

    await pressRun();
    const request = lastExecutedRequest(onExecute);
    expect(request.scope).toEqual({ kind: 'sheet_rows', sheet_id: 7 });
    expect(request.output_names).toEqual({ entities: 'prior_entities' });
  });

  it('preserves an initialized empty exact membership on the request', async () => {
    const { onExecute } = mountNer({
      selectedRowIds: [],
      hasExactRowScopeInitializer: true,
    });

    await pressRun();
    expect(lastExecutedRequest(onExecute).scope).toEqual({ kind: 'sheet_rows', sheet_id: 7, row_ids: [] });
  });
});

// ---------------------------------------------------------------- C1

describe('C1 — a real source picker', () => {
  it('replaces the bare "Input Template" field with a three-mode source control', () => {
    mountNer();

    expect(screen.getByTestId('text-source-mode')).toBeVisible();
    expect(screen.getByTestId('text-source-mode-all')).toBeVisible();
    expect(screen.getByTestId('text-source-mode-columns')).toBeVisible();
    expect(screen.getByTestId('text-source-mode-template')).toBeVisible();
    expect(screen.queryByTestId('field-input_template')).not.toBeInTheDocument();
    expect(screen.queryByTestId('field-input_columns')).not.toBeInTheDocument();
  });

  it('names exactly which columns All will read', async () => {
    mountNer();
    await selectAllSource();

    const summary = screen.getByTestId('text-source-all-columns');
    expect(summary).toHaveTextContent('headline, body, notes');
    expect(summary).not.toHaveTextContent('lat');
  });

  it('leaves a byte count and a blob column out of All text columns', async () => {
    const { onExecute } = mountNer({ sheet: filesSheet() });
    await selectAllSource();

    const summary = screen.getByTestId('text-source-all-columns');
    expect(summary).toHaveTextContent('filename, ocr_text');
    for (const column of ['size', 'ocr_text_blocks', 'media']) {
      expect(summary).not.toHaveTextContent(column);
    }

    await pressRun();
    expect(lastExecutedRequest(onExecute).params.source).toEqual(['filename', 'ocr_text']);
  });

  it('serializes All text columns as an EXPLICIT ordered source list — never [] and never a boolean', async () => {
    const { onExecute } = mountNer();
    await selectAllSource();
    await pressRun();

    const request = lastExecutedRequest(onExecute);
    expect(request.params.source).toEqual(['headline', 'body', 'notes']);
  });

  it('uses column-mode compatibility when switching from a numeric template to All', async () => {
    const user = userEvent.setup();
    const { onExecute } = mountNer({ sheet: filesSheet() });
    await user.click(screen.getByTestId('text-source-mode-template'));
    await user.selectOptions(screen.getByTestId('text-source-template-column-insert'), 'size');
    expect(screen.getByTestId('text-source-template-input')).toHaveValue('{{size}}');

    await selectAllSource();
    await pressRun();
    expect(lastExecutedRequest(onExecute).params.source).toEqual(['filename', 'ocr_text']);
  });

  it('blocks Run instead of posting an empty list when no compatible column exists', async () => {
    const { onExecute } = mountNer({
      sheet: sheetMeta([columnDef({ id: '1', name: 'lat', type: 'geo_point' })], { id: '7' }),
    });

    expect(screen.getByTestId('text-source-empty')).toBeVisible();
    await expectRunBlockedByForm();
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute).not.toHaveBeenCalled();
  });

  it('serializes a MULTI-column selection in pick order', async () => {
    const user = userEvent.setup();
    const { onExecute } = mountNer();

    await user.click(screen.getByTestId('text-source-mode-columns'));
    await user.click(screen.getByRole('button', { name: 'Remove headline' }));
    await user.click(screen.getByTestId('text-source-columns'));
    const menu = screen.getByTestId('text-source-columns-menu');
    // hidden:true is the established workaround (DeriveJoinForm.test.tsx et
    // al.) for querying inside a `popover` subtree under jsdom.
    await user.click(within(menu).getByRole('option', { name: /notes/, hidden: true }));
    await user.click(within(menu).getByRole('option', { name: /headline/, hidden: true }));

    await pressRun();
    expect(lastExecutedRequest(onExecute).params.source).toEqual(['notes', 'headline']);
  });

  it('blocks Run while the Column(s) mode has nothing selected', async () => {
    const user = userEvent.setup();
    const { onExecute } = mountNer();

    await user.click(screen.getByTestId('text-source-mode-columns'));
    await user.click(screen.getByRole('button', { name: 'Remove headline' }));
    await expectRunBlockedByForm();
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute).not.toHaveBeenCalled();
  });

  it('keeps the { } Template mode composing {{column}} tokens and posts source.text', async () => {
    const user = userEvent.setup();
    const { onExecute } = mountNer();

    await user.click(screen.getByTestId('text-source-mode-template'));
    await user.selectOptions(screen.getByTestId('text-source-template-column-insert'), 'body');
    expect(screen.getByTestId('text-source-template-input')).toHaveValue('{{body}}');

    await pressRun();
    expect(lastExecutedRequest(onExecute).params.source).toEqual({ text: '{{body}}' });
  });

  it('reopens a recorded explicit column list in Column(s) mode, never re-widened to All', () => {
    mountNer({ initialDraft: savedNer({ source: ['notes'] }) });

    expect(screen.queryByTestId('text-source-all-columns')).not.toBeInTheDocument();
    expect(screen.getByTestId('text-source-mode-columns')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByTestId('text-source-columns')).toHaveTextContent('notes');
    expect(screen.getByTestId('text-source-columns')).not.toHaveTextContent('headline');
  });

  it('blocks a saved selection containing a deleted column instead of silently narrowing it', async () => {
    // The typed form has no client-side column-existence check: the block
    // below is the SERVER's diagnostic (mirrored by nerResolver) surfaced by
    // the form. What the form itself guarantees is that the stale name is
    // shown and sent as saved, never narrowed to the columns that exist.
    const { onExecute, resolveParams } = mountNerWithServerDiagnostics({
      initialDraft: savedNer({ source: ['headline', 'deleted_notes'] }),
    });

    expect(screen.getByTestId('text-source-columns')).toHaveTextContent('headline');
    expect(screen.getByTestId('text-source-columns')).toHaveTextContent('deleted_notes');
    await waitFor(() => expect(resolveParams).toHaveBeenCalledWith(expect.objectContaining({
      params: expect.objectContaining({ source: ['headline', 'deleted_notes'] }),
    })));
    await waitFor(() => expect(screen.getAllByRole('alert').map((alert) => alert.textContent))
      .toContain('Unknown source column.'));
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------- C4a

describe('C4a — canonical entity-type chips', () => {
  it('writes the CANONICAL value into labels, and offers no raw OntoNotes tag', async () => {
    const user = userEvent.setup();
    const { onExecute } = mountNer();

    // No `GPE` chip exists to write `GPE` — the tag whose request side asked
    // for `gpe` while every emitted span said `location`.
    expect(screen.queryByTestId('ner-spacy-types-gpe')).not.toBeInTheDocument();
    expect(screen.getByTestId('ner-spacy-types')).not.toHaveTextContent('GPE');
    expect(screen.getByTestId('ner-spacy-types')).not.toHaveTextContent('NORP');
    expect(screen.getByTestId('ner-spacy-types')).not.toHaveTextContent('FAC');

    // Leave a single chip so the assertion is unambiguous.
    await user.click(screen.getByTestId('ner-spacy-types-person'));
    await user.click(screen.getByTestId('ner-spacy-types-organization'));
    await user.click(screen.getByTestId('ner-spacy-types-date'));
    await user.click(screen.getByTestId('ner-spacy-types-money'));

    await pressRun();
    expect(lastExecutedRequest(onExecute).params.labels).toEqual(['location']);
  });

  it('names the unguessable acronyms and renders the noise tier visibly', () => {
    mountNer();

    expect(screen.getByTestId('ner-spacy-types-norp')).toHaveTextContent(
      'Nationalities, religious & political groups',
    );
    expect(screen.getByTestId('ner-spacy-types-fac')).toHaveTextContent('Facilities');
    expect(screen.getByTestId('ner-spacy-types-location')).toHaveTextContent('Places');
    // The noisy tier is demoted, not hidden behind an Advanced disclosure.
    const cardinal = screen.getByTestId('ner-spacy-types-cardinal');
    expect(cardinal).toBeVisible();
    expect(cardinal.closest('[data-tier]')).toHaveAttribute('data-tier', 'noise');
    expect(screen.getByTestId('ner-spacy-types-person').closest('[data-tier]'))
      .toHaveAttribute('data-tier', 'recommended');
  });
});

// ---------------------------------------------------------------- C4b

describe('C4b — an empty selection is invalid', () => {
  it('defaults a new run to spaCy and the five recommended types', async () => {
    const { onExecute } = mountNer();

    expect(screen.getByTestId('model-picker-button')).toHaveTextContent('spaCy');
    for (const type of RECOMMENDED_LABELS) {
      expect(screen.getByTestId(`ner-spacy-types-${type}`)).toHaveAttribute('aria-pressed', 'true');
    }
    for (const type of ['product', 'norp', 'fac', 'time', 'cardinal']) {
      expect(screen.getByTestId(`ner-spacy-types-${type}`)).toHaveAttribute('aria-pressed', 'false');
    }

    await pressRun();
    const request = lastExecutedRequest(onExecute);
    expect(request.action_id).toBe('map.ner');
    expect(request.scope).toEqual({ kind: 'sheet_rows', sheet_id: 7 });
    expect(request.params.engine).toBe('spacy');
    expect(request.params.labels).toEqual(RECOMMENDED_LABELS);
    expect(request.output_names).toEqual({ entities: 'entities' });
  });

  it('never promises that an empty selection means "all types"', () => {
    const { container } = mountNer();
    expect(container.textContent).not.toMatch(/leave empty for all/i);
    expect(container.textContent).not.toMatch(/empty selection = no filter/i);
  });

  it('blocks submission once the final type is cleared — and does NOT silently submit a default', async () => {
    const user = userEvent.setup();
    const { onExecute, resolveParams } = mountNer();

    for (const type of RECOMMENDED_LABELS) {
      await user.click(screen.getByTestId(`ner-spacy-types-${type}`));
    }

    // The form's OWN gate (labels is required and empty), with a permissive
    // resolver so no server diagnostic can be what disables Run.
    await expectRunBlockedByForm();
    // The empty list went to the server as-is: no hidden default was
    // substituted on the way out.
    expect(resolveParams.mock.lastCall?.[0]).toHaveProperty('params.labels', []);
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute).not.toHaveBeenCalled();
  });

  // The legacy form authored the blocking copy itself ('an empty selection
  // extracts nothing, it does not mean "all types"'). The typed form renders
  // whatever the server's `labels` diagnostic says, in the chips' error slot
  // for the active engine; the wording asserted here is the mirror's.
  it('surfaces the server\'s labels diagnostic in the active engine\'s error slot', async () => {
    const user = userEvent.setup();
    mountNerWithServerDiagnostics();

    for (const type of RECOMMENDED_LABELS) {
      await user.click(screen.getByTestId(`ner-spacy-types-${type}`));
    }
    await waitFor(() => expect(screen.getByTestId('ner-spacy-types-error')).toBeVisible());
    expect(screen.getByTestId('ner-spacy-types-error')).toHaveTextContent('Pick at least one entity type.');
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();

    await chooseEngine('gliner');
    await waitFor(() => expect(screen.getByTestId('ner-gliner-labels-error')).toBeVisible());
    expect(screen.getByTestId('ner-gliner-labels-error')).toHaveTextContent('Pick at least one entity type.');
  });

  it('Select all checks every canonical type; Reset recommended returns to the five', async () => {
    const user = userEvent.setup();
    const { onExecute } = mountNer();

    await user.click(screen.getByTestId('ner-spacy-types-select-all'));
    for (const type of ['person', 'norp', 'fac', 'work_of_art', 'cardinal']) {
      expect(screen.getByTestId(`ner-spacy-types-${type}`)).toHaveAttribute('aria-pressed', 'true');
    }
    await pressRun();
    expect(lastExecutedRequest(onExecute).params.labels).toEqual(ALL_CANONICAL_LABELS);

    await user.click(screen.getByTestId('ner-spacy-types-reset-recommended'));
    expect(screen.getByTestId('ner-spacy-types-cardinal')).toHaveAttribute('aria-pressed', 'false');
    await pressRun();
    expect(lastExecutedRequest(onExecute).params.labels).toEqual(RECOMMENDED_LABELS);
  });

  it('normalizes backend-supported aliases in saved spaCy drafts into canonical picker values', async () => {
    const { onExecute } = mountNer({ initialDraft: savedNer({
      labels: ['ORG', 'People', 'GPE', ' WORK OF ART '],
    }) });

    for (const type of ['organization', 'person', 'location', 'work_of_art']) {
      await waitFor(() => expect(screen.getByTestId(`ner-spacy-types-${type}`))
        .toHaveAttribute('aria-pressed', 'true'));
    }
    await pressRun();
    expect(lastExecutedRequest(onExecute).params.labels).toEqual([
      'organization', 'person', 'location', 'work_of_art',
    ]);
  });

  it('refuses unknown spaCy labels even when supported aliases are also selected', async () => {
    const { onExecute } = mountNer({ initialDraft: savedNer({ labels: ['GPE', 'spaceship'] }) });

    await waitFor(() => expect(screen.getByTestId('ner-spacy-types-location')).toHaveAttribute('aria-pressed', 'true'));
    await waitFor(() => expect(screen.getByTestId('ner-spacy-types-error')).toBeVisible());
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute).not.toHaveBeenCalled();
  });

  it('enforces the same required-ness for GLiNER free text', async () => {
    const user = userEvent.setup();
    const { onExecute } = mountNer();

    // The fresh spaCy posture hides GLiNER's threshold field.
    expect(screen.queryByTestId('field-threshold')).not.toBeInTheDocument();

    await chooseEngine('gliner');
    expect(screen.getByTestId('field-threshold')).toBeInTheDocument();

    // The engine switch keeps the recommended set rather than blanking a
    // now-required field.
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    expect(screen.getAllByTestId('ner-gliner-labels-chip')).toHaveLength(5);
    while (screen.queryAllByTestId('ner-gliner-labels-chip').length) {
      const chip = screen.getAllByTestId('ner-gliner-labels-chip')[0];
      await user.click(within(chip).getByRole('button'));
    }
    await expectRunBlockedByForm();
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute).not.toHaveBeenCalled();
  });
});
