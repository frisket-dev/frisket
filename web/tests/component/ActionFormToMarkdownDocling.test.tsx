// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { generatedActionTemplateFromCatalogEntry } from '../../src/actions/model';
import { decodeSavedActionSpec, encodeSavedActionSpec } from '../../src/actions/savedActionSpec';
import { isGeneratedActionCatalogEntry, type EngineOption, type GeneratedActionDraft } from '../../src/api/types';
import { GeneratedActionForm } from '../../src/components/action-panel/GeneratedActionForm';
import { columnDef } from '../support/domainFixtures';
import { sheetMeta } from '../support/actionFormFixtures';
import { servedActionCatalog } from '../support/servedActionCatalog';
import { installPopoverPolyfill } from '../support/domPolyfills';

const catalog = servedActionCatalog();
beforeAll(installPopoverPolyfill);
afterEach(cleanup);
const sheet = sheetMeta([
  columnDef({ id: '1', name: 'doc', type: 'file' }),
  columnDef({ id: '2', name: 'html', type: 'text' }),
  columnDef({ id: '3', name: 'image', type: 'image' }),
], { id: '7', rowCount: 9 });

function form(initialDraft?: GeneratedActionDraft, available = true, roster?: EngineOption[]) {
  const raw = catalog.actions.find((entry) => entry.kind === 'media.to_markdown');
  if (!raw || !isGeneratedActionCatalogEntry(raw)) throw new Error('Missing typed Markdown catalog');
  // Availability is the test environment; the served catalog still owns the roster.
  const entry = { ...raw, ui_hints: { ...raw.ui_hints, engines: (roster ?? raw.ui_hints.engines)?.map((engine) => ({
    ...engine, available: available || engine.id === 'markitdown',
    error: !available && engine.id !== 'markitdown' ? 'Test runtime unavailable' : undefined,
  })) } };
  const template = generatedActionTemplateFromCatalogEntry(entry)!;
  const onExecute = vi.fn();
  const resolveParams = vi.fn(async ({ params }: { params: Record<string, unknown> }) => ({
    diagnostics: {}, logical_outputs: entry.ui_hints.logical_outputs.filter(({ key }) => (
      key === 'markdown' || entry.ui_hints.engines?.some((engine) => engine.id === params.engine && engine.tier === 'sidecar')
    )),
  }));
  render(<GeneratedActionForm catalogEntry={entry} actionTemplate={template} sheet={sheet}
    selectedRowIds={['3', '8']} initialSourceColumn="doc" initialDraft={initialDraft}
    hasExactRowScopeInitializer={Boolean(initialDraft)} running={false}
    resolveParams={resolveParams} onExecute={onExecute} onClose={vi.fn()}
    estimateAction={vi.fn(async () => ({ cost: 0, rows: 2, billed_cost: 0 }))} />);
  return { onExecute, entry };
}
async function run() {
  await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
  fireEvent.click(screen.getByTestId('generated-action-run'));
}
async function choose(engine: string, tier?: string) {
  await userEvent.click(screen.getByTestId('engine-picker-button'));
  if (tier) await userEvent.click(screen.getByTestId('engine-picker-tier-' + tier));
  await userEvent.click(screen.getByTestId('engine-option-' + engine));
}

describe('typed Markdown form', () => {
  it('defaults to MarkItDown and submits source, exact rows and generic output names', async () => {
    const { onExecute } = form();
    expect(screen.getByTestId('engine-picker-button')).toHaveTextContent(/MarkItDown/i);
    expect(screen.getByTestId('field-source')).toHaveValue('doc');
    expect(screen.getByTestId('field-source')).toHaveTextContent('html');
    expect(screen.getByTestId('field-source')).not.toHaveTextContent('image');
    fireEvent.change(screen.getByTestId('field-source'), { target: { value: 'html' } });
    await screen.findByTestId('field-output-markdown');
    expect(screen.getByTestId('field-engine').compareDocumentPosition(screen.getByTestId('field-output-markdown'))
      & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    fireEvent.change(screen.getByTestId('field-output-markdown'), { target: { value: 'Readable' } });
    await run();
    expect(onExecute.mock.calls[0][0]).toEqual({ action_id: 'media.to_markdown',
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [3, 8] },
      params: { source: 'html', engine: 'markitdown' }, output_names: { markdown: 'Readable' },
      idempotency_key: expect.any(String) });
  });

  it('keeps the engine picker stable when the schema default is absent from the served roster', async () => {
    const raw = catalog.actions.find((entry) => entry.kind === 'media.to_markdown');
    if (!raw || !isGeneratedActionCatalogEntry(raw)) throw new Error('Missing typed Markdown catalog');
    const docling = raw.ui_hints.engines?.find((engine) => engine.id === 'docling');
    if (!docling) throw new Error('Missing Docling engine');
    form(undefined, true, [docling]);

    expect(screen.getByTestId('engine-picker-button')).toHaveTextContent('markitdown (unavailable)');
    expect(screen.queryByTestId('engine-select')).not.toBeInTheDocument();
    await userEvent.click(screen.getByTestId('engine-picker-button'));
    expect(screen.getByTestId('engine-option-markitdown')).toHaveAttribute('aria-disabled', 'true');
    expect(screen.getByTestId('engine-option-reason-markitdown')).toHaveTextContent(
      'This engine is unavailable for this action.',
    );
    await userEvent.click(screen.getByTestId('engine-picker-tier-sidecar'));
    await userEvent.click(screen.getByTestId('engine-option-docling'));
    expect(screen.getByTestId('engine-picker-button')).toHaveTextContent('Docling');
  });

  it('retains local, sidecar and hosted engines plus experimental/license details', async () => {
    form();
    await userEvent.click(screen.getByTestId('engine-picker-button'));
    expect(screen.getByTestId('engine-option-trafilatura_html')).toHaveTextContent(/Trafilatura/i);
    await userEvent.click(screen.getByTestId('engine-picker-tier-sidecar'));
    expect(screen.getByTestId('engine-option-docling')).toHaveTextContent(/Docling/i);
    expect(screen.getByTestId('engine-option-chandra')).toHaveTextContent(/experimental/i);
    await userEvent.click(screen.getByTestId('engine-option-chandra'));
    expect(screen.getByText(/has a restrictive license/)).toBeInTheDocument();
    await userEvent.click(screen.getByTestId('engine-picker-button'));
    await userEvent.click(screen.getByTestId('engine-picker-tier-hosted'));
    expect(screen.getByTestId('engine-option-datalab')).toHaveTextContent(/Datalab/i);
  });

  it('names sidecar OCR-use output and drops it when switching to a local engine', async () => {
    const { onExecute } = form();
    await choose('docling', 'sidecar');
    await screen.findByTestId('field-output-ocr_used');
    fireEvent.change(screen.getByTestId('field-output-ocr_used'), { target: { value: 'Page OCR' } });
    await run();
    expect(onExecute.mock.calls[0][0].output_names.ocr_used).toBe('Page OCR');
    await choose('trafilatura_html', 'local');
    await waitFor(() => expect(screen.queryByTestId('field-output-ocr_used')).not.toBeInTheDocument());
    await run();
    expect(onExecute.mock.calls[1][0].params.engine).toBe('trafilatura_html');
    expect(onExecute.mock.calls[1][0].output_names).toEqual({ markdown: 'markdown' });
  });

  it.each([undefined, 'docling'])('roundtrips saved engine %s without materializing omitted defaults', async (engine) => {
    const draft: GeneratedActionDraft = { action_id: 'media.to_markdown',
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [3, 8] },
      params: { source: 'html', ...(engine ? { engine } : {}) },
      output_names: { markdown: 'Saved markdown', ...(engine ? { ocr_used: 'Saved page OCR' } : {}) } };
    const { onExecute } = form(draft);
    expect(screen.getByTestId('engine-picker-button')).toHaveTextContent(engine ? /Docling/i : /MarkItDown/i);
    expect(encodeSavedActionSpec(decodeSavedActionSpec(catalog, draft))).toEqual(draft);
    await run();
    expect(onExecute.mock.calls[0][0]).toEqual({ ...draft, idempotency_key: expect.any(String) });
  });

  it('keeps an unavailable saved engine visible and refuses execution', async () => {
    const { onExecute } = form({ action_id: 'media.to_markdown',
      scope: { kind: 'sheet_rows', sheet_id: 7 }, params: { source: 'doc', engine: 'docling' },
      output_names: { markdown: 'Saved', ocr_used: 'Saved OCR' } }, false);
    await screen.findByTestId('field-output-markdown');
    expect(screen.getByTestId('engine-picker-selected-unavailable')).toHaveTextContent('unavailable');
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    expect(onExecute).not.toHaveBeenCalled();
  });
});
