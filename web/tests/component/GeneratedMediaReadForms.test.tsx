// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { generatedActionTemplateFromCatalogEntry } from '../../src/actions/model';
import { encodeSavedActionSpec, decodeSavedActionSpec } from '../../src/actions/savedActionSpec';
import { isGeneratedActionCatalogEntry, type EngineOption, type GeneratedActionDraft,
  type RunEstimate } from '../../src/api/types';
import { GeneratedActionForm } from '../../src/components/action-panel/GeneratedActionForm';
import { columnDef } from '../support/domainFixtures';
import { sheetMeta } from '../support/actionFormFixtures';
import { servedActionCatalog } from '../support/servedActionCatalog';
import { installPopoverPolyfill } from '../support/domPolyfills';

const catalog = servedActionCatalog();
type MediaAction = 'media.ocr' | 'media.transcribe';
const sheet = sheetMeta([
  columnDef({ id: '1', name: 'scan', type: 'file' }),
  columnDef({ id: '2', name: 'clip', type: 'audio' }),
], { id: '7', rowCount: 9 });
beforeAll(installPopoverPolyfill);
afterEach(cleanup);

function form(kind: MediaAction, options: {
  initialDraft?: GeneratedActionDraft;
  enginePatch?: (engine: EngineOption) => EngineOption;
  estimate?: RunEstimate;
  diagnostics?: Record<string, { ok: boolean; message: string }>;
  columns?: typeof sheet.columns;
  selectedRowIds?: string[];
} = {}) {
  const raw = catalog.actions.find((entry) => entry.kind === kind);
  if (!raw || !isGeneratedActionCatalogEntry(raw)) throw new Error(`Missing typed ${kind} catalog`);
  const entry = { ...raw, ui_hints: { ...raw.ui_hints,
    engines: raw.ui_hints.engines?.map((engine) => options.enginePatch?.(engine)
      ?? { ...engine, available: true, error: undefined }),
  } };
  const template = generatedActionTemplateFromCatalogEntry(entry)!;
  const onExecute = vi.fn();
  const onOpenDiagnose = vi.fn();
  const onOpenOcrCompare = vi.fn();
  // The server determines active outputs. These transport responses use its
  // declared outputs and engine facts; backend tests exercise the resolution.
  const resolveParams = vi.fn(async ({ params }: { params: Record<string, unknown> }) => {
    const selected = params.engine ?? entry.input_schema.properties?.engine?.default;
    const detects = entry.ui_hints.engines?.find((engine) => engine.id === selected)?.language?.detects;
    return { diagnostics: options.diagnostics ?? {},
      logical_outputs: entry.ui_hints.logical_outputs.filter(({ key }) => (
        key !== 'pdf' && key !== 'detected_language'
        || key === 'pdf' && params.searchable_pdf === true
        || key === 'detected_language' && detects === true
      )) };
  });
  const estimateAction = vi.fn(async () => options.estimate ?? {
    cost: 0, rows: 2, billed_cost: 0, policy_id: 'frisket.identity.v1',
  });
  render(<GeneratedActionForm catalogEntry={entry} actionTemplate={template}
    sheet={{ ...sheet, columns: options.columns ?? sheet.columns }} selectedRowIds={options.selectedRowIds ?? ['3', '8']} initialSourceColumn={kind === 'media.ocr' ? 'scan' : 'clip'}
    initialDraft={options.initialDraft} hasExactRowScopeInitializer={Boolean(options.initialDraft)}
    running={false} resolveParams={resolveParams} estimateAction={estimateAction}
    onExecute={onExecute} onClose={vi.fn()} onOpenDiagnose={onOpenDiagnose} onOpenOcrCompare={onOpenOcrCompare} />);
  return { onExecute, entry, resolveParams, estimateAction, onOpenDiagnose, onOpenOcrCompare };
}

function saved(kind: MediaAction, params: Record<string, unknown>, output_names: Record<string, string> = {}): GeneratedActionDraft {
  return { action_id: kind, scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [3, 8] },
    params: { source: kind === 'media.ocr' ? 'scan' : 'clip', ...params }, output_names };
}
async function run() {
  await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
  fireEvent.click(screen.getByTestId('generated-action-run'));
}
async function choose(engine: EngineOption) {
  await userEvent.click(screen.getByTestId('engine-picker-button'));
  await userEvent.click(screen.getByTestId(`engine-picker-tier-${engine.tier}`));
  await userEvent.click(screen.getByTestId(`engine-option-${engine.id.replace(/[^a-zA-Z0-9_-]/g, '-')}`));
}

describe('typed OCR/transcription forms', () => {
  it.each([{ selectedRowIds: [] }, { selectedRowIds: ['3', '8'] }])('opens upload OCR comparison without requiring one selected project row (%j)', ({ selectedRowIds }) => {
    const { onOpenOcrCompare, onExecute } = form('media.ocr', { columns: [], selectedRowIds });
    expect(screen.getByTestId('ocr-compare-open')).toBeEnabled();
    fireEvent.click(screen.getByTestId('ocr-compare-open'));
    expect(onOpenOcrCompare).toHaveBeenCalledOnce();
    expect(onOpenOcrCompare.mock.calls[0][0]).not.toHaveProperty('request');
    expect(onExecute).not.toHaveBeenCalled();
  });

  it('submits a fresh transcription without manufacturing unsupported optional knobs', async () => {
    const { onExecute, estimateAction } = form('media.transcribe');
    expect(screen.getByTestId('field-source')).toHaveValue('clip');
    expect(screen.queryByTestId('action-prompt')).not.toBeInTheDocument();
    expect(screen.queryByTestId('output-fields-label')).not.toBeInTheDocument();
    expect(screen.getByTestId('field-vad')).toBeChecked();
    expect(screen.getByTestId('field-vad').closest('label')).toHaveTextContent('Voice Activity Detection');
    expect(screen.getByText('Only transcribe when speech is detected (reduces hallucinations)')).toBeVisible();
    await run();
    expect(onExecute.mock.calls[0][0]).toEqual({ action_id: 'media.transcribe',
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [3, 8] },
      params: { source: 'clip', engine: 'faster_whisper' },
      output_names: { text: 'transcript', segments: 'transcript_segments', detected_language: 'detected_language' },
      idempotency_key: expect.any(String) });
    await waitFor(() => expect(estimateAction).toHaveBeenCalled());
    await waitFor(() => expect(screen.getByTestId('cost-estimate')).toHaveClass('cost-local'));
    expect(estimateAction.mock.calls[0][0]).toMatchObject({ params: { source: 'clip', engine: 'faster_whisper' } });
  });

  it.each(['media.ocr', 'media.transcribe'] as const)('preserves every served engine in %s', async (kind) => {
    const { entry, onExecute } = form(kind);
    for (const engine of entry.ui_hints.engines ?? []) {
      await choose(engine);
      expect(screen.getByTestId('engine-picker-button')).toHaveTextContent(engine.label);
      await run();
      expect(onExecute.mock.lastCall?.[0].params.engine).toBe(engine.id);
    }
  });

  it('makes the engine choice before its language/options and output summary', () => {
    form('media.transcribe');
    const engine = screen.getByTestId('field-engine');
    for (const later of ['transcribe-language-select', 'field-vad', 'transcribe-output-summary']) {
      expect(engine.compareDocumentPosition(screen.getByTestId(later)) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    }
  });

  it('derives OCR text boxes from the result column and preserves searchable PDF naming', async () => {
    const { onExecute } = form('media.ocr', { columns: [...sheet.columns,
      columnDef({ id: '3', name: 'ocr_text', type: 'text' })] });
    expect(screen.getByTestId('field-searchable_pdf')).not.toBeChecked();
    await screen.findByTestId('field-output-prefix');
    expect(screen.getByTestId('field-output-prefix')).toHaveValue('ocr_text_2');
    expect(screen.queryByTestId('field-output-text')).not.toBeInTheDocument();
    expect(screen.queryByTestId('field-output-blocks')).not.toBeInTheDocument();
    expect(screen.queryByTestId('field-output-pdf')).not.toBeInTheDocument();
    await run();
    expect(onExecute.mock.lastCall?.[0].output_names).toEqual({
      text: 'ocr_text_2', blocks: 'ocr_text_2_boxes',
    });
    fireEvent.change(screen.getByTestId('field-output-prefix'), { target: { value: 'Read text' } });
    fireEvent.change(screen.getByTestId('field-dpi'), { target: { value: '300' } });
    fireEvent.click(screen.getByTestId('field-searchable_pdf'));
    await screen.findByTestId('field-output-pdf');
    fireEvent.change(screen.getByTestId('field-output-pdf'), { target: { value: 'Searchable' } });
    await run();
    expect(onExecute.mock.lastCall?.[0]).toMatchObject({
      params: { source: 'scan', engine: 'rapidocr', dpi: 300, searchable_pdf: true },
      output_names: { text: 'Read text', blocks: 'Read text_boxes', pdf: 'Searchable' },
    });
    fireEvent.click(screen.getByTestId('field-searchable_pdf'));
    await waitFor(() => expect(screen.queryByTestId('field-output-pdf')).not.toBeInTheDocument());
    await run();
    expect(onExecute.mock.lastCall?.[0].output_names).toEqual({ text: 'Read text', blocks: 'Read text_boxes' });
  });

  it('chooses a fresh OCR result prefix when only its derived boxes exist', async () => {
    form('media.ocr', { columns: [...sheet.columns,
      columnDef({ id: '3', name: 'ocr_text_boxes', type: 'json' })] });

    await screen.findByTestId('field-output-prefix');
    expect(screen.getByTestId('field-output-prefix')).toHaveValue('ocr_text_2');
  });

  it('preserves declared language choices and clears them on a deliberate fixed-engine switch', async () => {
    const { entry, onExecute } = form('media.transcribe');
    const picker = screen.getByTestId('transcribe-language-select');
    expect(picker).toHaveTextContent('Auto');
    await userEvent.selectOptions(picker, 'es');
    const fixed = entry.ui_hints.engines!.find((engine) => engine.language?.mode === 'fixed')!;
    await choose(fixed);
    expect(screen.getByTestId('transcribe-language-fixed')).toHaveTextContent(/English only/i);
    expect(screen.queryByTestId('transcribe-language-select')).not.toBeInTheDocument();
    expect(screen.getByTestId('transcribe-output-summary')).not.toHaveTextContent('detected_language');
    await waitFor(() => expect(screen.queryByTestId('field-output-detected_language')).not.toBeInTheDocument());
    await run();
    expect(onExecute.mock.lastCall?.[0].params).toEqual({ source: 'clip', engine: fixed.id });
    expect(onExecute.mock.lastCall?.[0].output_names).toEqual({ text: 'transcript', segments: 'transcript_segments' });
  });

  it('renders intrinsic and unsupported diarization without a misleading toggle', async () => {
    const { entry } = form('media.transcribe');
    const intrinsic = entry.ui_hints.engines!.find((engine) => engine.diarization?.mode === 'intrinsic')!;
    await choose(intrinsic);
    expect(screen.getByTestId('transcribe-diarization-intrinsic')).toHaveTextContent('always on');
    expect(screen.queryByTestId('transcribe-diarize-toggle')).not.toBeInTheDocument();
    expect(screen.queryByTestId('transcribe-language-select')).not.toBeInTheDocument();
    expect(screen.queryByTestId('field-vad')).not.toBeInTheDocument();
    expect(screen.queryByTestId('field-model_size')).not.toBeInTheDocument();
    expect(screen.getByTestId('field-context')).toBeInTheDocument();
    const none = entry.ui_hints.engines!.find((engine) => !engine.diarization?.supported)!;
    await choose(none);
    expect(screen.queryByTestId('transcribe-diarization-intrinsic')).not.toBeInTheDocument();
    expect(screen.queryByTestId('transcribe-diarize-toggle')).not.toBeInTheDocument();
  });

  it('clears a previously authored OCR language on a fixed-language engine switch', async () => {
    const { entry, onExecute } = form('media.ocr', {
      initialDraft: saved('media.ocr', { engine: 'rapidocr', language: 'es' },
        { text: 'Read', blocks: 'Bounds' }),
      enginePatch: (engine) => ({ ...engine, available: true, error: undefined,
        ...(engine.id === 'tesseract' ? { language: { mode: 'fixed' as const,
          default: 'en', fixed_language: 'en', detects: false,
          choices: [{ value: 'en', label: 'English' }] } } : {}),
      }),
    });
    await choose(entry.ui_hints.engines!.find((engine) => engine.id === 'tesseract')!);
    expect(screen.getByTestId('ocr-language-fixed')).toHaveTextContent('This engine reads English only.');
    expect(screen.queryByTestId('field-language')).not.toBeInTheDocument();
    await run();
    expect(onExecute.mock.lastCall?.[0].params).not.toHaveProperty('language');
  });

  it('shows a searchable-PDF validation refusal without dropping the requested option', async () => {
    const draft = saved('media.ocr', { engine: 'openai/gpt-4.1-mini', searchable_pdf: true },
      { text: 'Words', blocks: 'Bounds', pdf: 'Searchable' });
    const { resolveParams, onExecute } = form('media.ocr', { initialDraft: draft,
      diagnostics: { searchable_pdf: { ok: false, message: 'This engine returns no text geometry.' } } });
    await waitFor(() => expect(resolveParams).toHaveBeenCalled());
    expect(resolveParams.mock.calls[0][0].params).toHaveProperty('searchable_pdf', true);
    expect(screen.getByTestId('field-searchable_pdf')).toBeChecked();
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    expect(onExecute).not.toHaveBeenCalled();
  });

  it('keeps default-on diarization optional and serializes an explicit opt-out', async () => {
    const { entry, onExecute } = form('media.transcribe');
    const defaultOn = entry.ui_hints.engines!.find((engine) => engine.diarization?.default === true)!;
    await choose(defaultOn);
    expect(screen.getByTestId('transcribe-diarize-toggle')).toBeChecked();
    expect(screen.getByTestId('field-clean')).toBeInTheDocument();
    expect(screen.queryByTestId('transcribe-speaker-count-row')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('transcribe-diarize-toggle'));
    await run();
    expect(onExecute.mock.lastCall?.[0].params).toEqual({ source: 'clip', engine: defaultOn.id, diarize: false });
  });

  it('supports exact/range speaker counts, numeric values, and clears stale hints', async () => {
    const { entry, onExecute } = form('media.transcribe', {
      // Exercise an admitted count-capable declaration independently of which
      // deployed engine currently exposes this optional control.
      enginePatch: (engine) => ({ ...engine, available: true, error: undefined,
        ...(engine.id === 'faster_whisper' ? { diarization: {
          supported: true, mode: 'optional' as const, speaker_hint: 'count' as const,
        } } : {}),
      }),
    });
    fireEvent.click(screen.getByTestId('transcribe-diarize-toggle'));
    expect(screen.getByTestId('transcribe-diarize-hint')).not.toHaveTextContent('up to');
    fireEvent.change(screen.getByTestId('transcribe-num-speakers'), { target: { value: '3 people' } });
    expect(screen.getByTestId('transcribe-num-speakers')).toHaveValue('3');
    await run();
    expect(onExecute.mock.lastCall?.[0].params).toMatchObject({ diarize: true, num_speakers: 3 });
    fireEvent.click(screen.getByTestId('transcribe-speaker-count-mode-range'));
    fireEvent.change(screen.getByTestId('transcribe-min-speakers'), { target: { value: '2' } });
    fireEvent.change(screen.getByTestId('transcribe-max-speakers'), { target: { value: '4' } });
    await run();
    expect(onExecute.mock.lastCall?.[0].params).toMatchObject({ diarize: true, min_speakers: 2, max_speakers: 4 });
    expect(onExecute.mock.lastCall?.[0].params).not.toHaveProperty('num_speakers');
    fireEvent.click(screen.getByTestId('transcribe-diarize-toggle'));
    await run();
    expect(onExecute.mock.lastCall?.[0].params).not.toHaveProperty('diarize');
    expect(onExecute.mock.lastCall?.[0].params).not.toHaveProperty('min_speakers');
    await choose(entry.ui_hints.engines!.find((engine) => engine.id === 'parakeet-tdt')!);
    await run();
    expect(onExecute.mock.lastCall?.[0].params).toEqual({ source: 'clip', engine: 'parakeet-tdt' });
  });

  it('roundtrips saved/Copilot canonical Params and custom output names without adding defaults', async () => {
    const draft = saved('media.transcribe', { language: ['es'], vad: false },
      { text: 'Interview', segments: 'Timing', detected_language: 'Language' });
    expect(encodeSavedActionSpec(decodeSavedActionSpec(catalog, draft))).toEqual(draft);
    const { onExecute } = form('media.transcribe', { initialDraft: draft });
    expect(screen.getByTestId('transcribe-language-select')).toHaveValue('es');
    await userEvent.selectOptions(screen.getByTestId('transcribe-language-select'), 'en');
    await run();
    expect(onExecute.mock.lastCall?.[0]).toEqual({ ...draft,
      params: { ...draft.params, language: ['en'] }, idempotency_key: expect.any(String) });
  });

  it('keeps invalid saved option intent for validation instead of silently changing the request', async () => {
    const draft = saved('media.transcribe', { engine: 'moss', diarize: false },
      { text: 'Words', segments: 'Timing' });
    const { onExecute, resolveParams } = form('media.transcribe', { initialDraft: draft,
      diagnostics: { diarize: { ok: false, message: 'This engine identifies speakers intrinsically.' } } });
    await waitFor(() => expect(resolveParams).toHaveBeenCalled());
    expect(resolveParams.mock.calls[0][0].params).toHaveProperty('diarize', false);
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    expect(onExecute).not.toHaveBeenCalled();
  });

  it.each([
    { kind: 'media.transcribe' as const, engine: 'faster_whisper', language: 'es' },
    { kind: 'media.ocr' as const, engine: 'rapidocr', language: ['es'] },
  ])('keeps malformed saved language editable for server validation: $kind', async ({ kind, engine, language }) => {
    const { resolveParams, onExecute } = form(kind, {
      initialDraft: saved(kind, { engine, language }),
      diagnostics: { language: { ok: false, message: 'Invalid language value.' } },
    });
    await waitFor(() => expect(resolveParams).toHaveBeenCalled());
    expect(resolveParams.mock.calls[0][0].params.language).toEqual(language);
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    expect(onExecute).not.toHaveBeenCalled();
  });

  it.each(['media.ocr', 'media.transcribe'] as const)('previews the same named request without confirmation for free %s', async (kind) => {
    const { onExecute } = form(kind);
    await waitFor(() => expect(screen.getByTestId('generated-action-preview')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-preview'));
    await run();
    const [preview, intent] = onExecute.mock.calls[0];
    expect(intent).toBe('preview');
    expect(preview).not.toHaveProperty('confirmation');
    expect(onExecute.mock.calls[1][0]).toEqual({ ...preview, idempotency_key: expect.any(String) });
    expect(onExecute.mock.calls[1][1]).toBe('run');
  });

  it('preserves unavailable engines and the reason in the availability disclosure', async () => {
    const draft = saved('media.ocr', { engine: 'dots.mocr' }, { text: 'Words', blocks: 'Bounds' });
    const { onExecute, onOpenDiagnose } = form('media.ocr', { initialDraft: draft,
      enginePatch: (engine) => ({ ...engine, available: engine.id !== 'dots.mocr',
        error: engine.id === 'dots.mocr' ? 'Test runtime unavailable' : undefined }) });
    expect(screen.getByTestId('engine-availability-disclosure')).toHaveTextContent('Test runtime unavailable');
    expect(screen.getByTestId('engine-picker-selected-unavailable')).toHaveTextContent('unavailable');
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    expect(screen.getByTestId('generated-action-run')).toHaveAttribute('title', expect.stringContaining('Test runtime unavailable'));
    expect(screen.getByTestId('engine-picker-selected-reason')).toHaveTextContent('Test runtime unavailable');
    expect(onExecute).not.toHaveBeenCalled();
    const disclosure = screen.getByTestId('engine-availability-disclosure');
    expect(disclosure.tagName).toBe('DETAILS');
    expect(disclosure).not.toHaveAttribute('open');
    await userEvent.click(within(disclosure).getByText('Engine availability'));
    expect(disclosure).toHaveAttribute('open');
    await userEvent.click(screen.getByTestId('engine-availability-open-diagnose'));
    expect(onOpenDiagnose).toHaveBeenCalledTimes(1);
  });

  it.each(['media.ocr', 'media.transcribe'] as const)('keeps the declared default despite another recommendation for %s', async (kind) => {
    const defaultId = kind === 'media.ocr' ? 'rapidocr' : 'faster_whisper';
    const { onExecute } = form(kind, { enginePatch: (engine) => ({
      ...engine, available: true, error: undefined, recommended: engine.id !== defaultId,
    }) });
    expect(screen.queryByTestId('engine-availability-disclosure')).not.toBeInTheDocument();
    expect(screen.queryByTestId('engine-picker-selected-unavailable')).not.toBeInTheDocument();
    await run();
    expect(onExecute.mock.lastCall?.[0].params.engine).toBe(defaultId);
  });

  it.each([
    { existing: ['transcript_segments'], text: 'transcript', segments: 'transcript_segments_2', language: 'detected_language' },
    { existing: ['transcript', 'transcript_2_segments'], text: 'transcript_2', segments: 'transcript_segments', language: 'detected_language' },
    { existing: ['transcript', 'detected_language'], text: 'transcript_2', segments: 'transcript_segments', language: 'detected_language_2' },
  ])('dedupes only the output that collides: $existing', async ({ existing, text, segments, language }) => {
    const { onExecute } = form('media.transcribe', { columns: [...sheet.columns,
      ...existing.map((name, index) => columnDef({ id: String(10 + index), name, type: 'text' })),
    ] });
    await run();
    expect(onExecute.mock.lastCall?.[0].output_names).toEqual({ text, segments, detected_language: language });
  });
});

describe('media cost presentation', () => {
  it.each(['media.ocr', 'media.transcribe'] as const)('uses the server billed quote and keeps it beside Run for %s', async (kind) => {
    const { entry } = form(kind, { estimate: { cost: 0.11, rows: 2, billed_cost: 210_000,
      policy_id: 'acme.cost_plus.v1', billing_label: 'Billed through Acme credits',
      venue_label: 'Acme-operated infrastructure', ...(kind === 'media.transcribe' ? { audio_seconds: 10.64 } : {}) } });
    await waitFor(() => expect(screen.getByTestId('cost-estimate')).toHaveTextContent('$0.21'));
    const line = screen.getByTestId('cost-estimate');
    expect(line).not.toHaveTextContent('$0.11');
    expect(line).toHaveTextContent('Billed through Acme credits');
    expect(line).toHaveTextContent('Acme-operated infrastructure');
    if (kind === 'media.transcribe') expect(line).toHaveTextContent('10.6 s of audio');
    const engine = entry.ui_hints.engines?.find((candidate) => (
      candidate.id === entry.input_schema.properties?.engine?.default
    ));
    const outputCount = entry.ui_hints.logical_outputs.filter(({ key }) => (
      key !== 'pdf' && (key !== 'detected_language' || engine?.language?.detects === true)
    )).length;
    expect(line).toHaveTextContent(new RegExp(`\\b${outputCount} output ${outputCount === 1 ? 'column' : 'columns'}\\b`));
    expect(line.parentElement).toHaveClass('action-run-actions');
    expect(line.parentElement).toContainElement(screen.getByTestId('generated-action-run'));
  });

  it.each([null, undefined])('never substitutes provider cost for missing billed quote %s', async (billed_cost) => {
    const { onExecute } = form('media.transcribe', { estimate: {
      cost: 0.11, rows: 2, billed_cost, warning: 'Audio duration is missing; run metadata backfill',
      ...(billed_cost === null ? { policy_id: 'acme.cost_plus.v1' } : {}),
    } });
    await waitFor(() => expect(screen.getByTestId('cost-estimate')).toHaveTextContent('Audio duration is missing'));
    expect(screen.getByTestId('cost-estimate')).toHaveTextContent('UNKNOWN');
    expect(screen.getByTestId('cost-estimate')).not.toHaveTextContent('$0.11');
    await run();
    expect(onExecute.mock.lastCall?.[0]).not.toHaveProperty('confirmation');
    expect(onExecute.mock.lastCall?.[0].params).not.toHaveProperty('confirmed');
    expect(screen.queryByTestId('cost-gate-modal')).not.toBeInTheDocument();
  });
});
