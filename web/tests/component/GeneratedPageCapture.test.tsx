// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { generatedActionTemplateFromCatalogEntry } from '../../src/actions/model';
import { encodeSavedActionSpec, decodeSavedActionSpec } from '../../src/actions/savedActionSpec';
import { isGeneratedActionCatalogEntry, type GeneratedActionDraft } from '../../src/api/types';
import { GeneratedActionForm } from '../../src/components/action-panel/GeneratedActionForm';
import { servedActionCatalog } from '../support/servedActionCatalog';
import { sheetMeta } from '../support/actionFormFixtures';
import { columnDef } from '../support/domainFixtures';

const catalog = servedActionCatalog();
const entry = catalog.actions.find((item) => item.kind === 'web.capture_page');
if (!entry || !isGeneratedActionCatalogEntry(entry)) throw new Error('Missing typed capture catalog');
const template = generatedActionTemplateFromCatalogEntry(entry)!;
const sheet = sheetMeta([
  columnDef({ id: '1', name: 'url', type: 'link' }),
  columnDef({ id: '2', name: 'page', type: 'file' }),
], { id: '7', name: 'Sources', rowCount: 3 });
afterEach(cleanup);

function form(initialDraft?: GeneratedActionDraft) {
  const onExecute = vi.fn();
  const resolveParams = vi.fn(async ({ params }: { params: Record<string, unknown> }) => ({
    diagnostics: {}, creates_sheet: params.output_mode === 'links',
    logical_outputs: params.output_mode === 'links'
      ? [{ key: 'url', column_type: 'link' }, { key: 'anchor_text', column_type: 'text' },
        { key: 'source_url', column_type: 'link' }]
      : [{ key: 'page', column_type: 'file' }],
  }));
  render(<GeneratedActionForm catalogEntry={entry} actionTemplate={template}
    sheet={sheet} selectedRowIds={['4', '6']} initialSourceColumn="url" initialDraft={initialDraft}
    hasExactRowScopeInitializer={Boolean(initialDraft)} running={false} resolveParams={resolveParams}
    onExecute={onExecute} onClose={vi.fn()} />);
  return onExecute;
}
async function run() {
  await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
  fireEvent.click(screen.getByTestId('generated-action-run'));
}
function saved(params: Record<string, unknown>, output_names: Record<string, string>, sheet_name?: string) {
  const draft: GeneratedActionDraft = { action_id: 'web.capture_page',
    scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [4, 6] }, params, output_names,
    ...(sheet_name ? { sheet_name } : {}) };
  const decoded = decodeSavedActionSpec(catalog, draft);
  expect(encodeSavedActionSpec(decoded)).toEqual(draft);
  if (!decoded.registeredDraft) throw new Error('Typed capture did not round-trip');
  return decoded.registeredDraft;
}

describe('typed page capture controls', () => {
  it('defaults to static pages and dedupes a new destination without changing acquisition limits', async () => {
    const execute = form();
    expect(screen.getByTestId('field-output_mode')).toHaveValue('page');
    expect(screen.getByTestId('field-render_mode')).toHaveValue('static');
    expect(screen.getByTestId('field-max_bytes')).toHaveValue('5');
    expect(screen.getByTestId('field-timeout_ms')).toHaveValue('30');
    expect(screen.queryByTestId('field-include_warc')).not.toBeInTheDocument();
    await run();
    expect(execute.mock.calls[0][0]).toMatchObject({ action_id: 'web.capture_page',
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [4, 6] },
      params: { source: 'url' }, output_names: { page: 'page_2' } });
    expect(execute.mock.calls[0][0]).not.toHaveProperty('sheet_name');
  });
  it('switches destinations while keeping render and scaled limit controls', async () => {
    const execute = form();
    fireEvent.change(screen.getByTestId('field-render_mode'), { target: { value: 'playwright' } });
    fireEvent.click(screen.getByTestId('field-include_warc'));
    fireEvent.change(screen.getByTestId('field-max_bytes'), { target: { value: '8' } });
    fireEvent.change(screen.getByTestId('field-timeout_ms'), { target: { value: '45' } });
    fireEvent.change(screen.getByTestId('field-output_mode'), { target: { value: 'links' } });
    expect(await screen.findByTestId('field-sheet_name')).toHaveValue('Links');
    expect(screen.queryByTestId('field-output-page')).not.toBeInTheDocument();
    expect(screen.queryByTestId('field-include_warc')).not.toBeInTheDocument();
    expect(screen.getByTestId('field-render_mode')).toHaveValue('playwright');
    fireEvent.change(screen.getByTestId('field-sheet_name'), { target: { value: 'Source links' } });
    await run();
    expect(execute.mock.calls[0][0]).toMatchObject({ sheet_name: 'Source links', params: {
      output_mode: 'links', render_mode: 'playwright', include_warc: false, max_bytes: 8_000_000, timeout_ms: 45_000,
    } });
    expect(execute.mock.calls[0][0].output_names).not.toHaveProperty('page');
    fireEvent.change(screen.getByTestId('field-output_mode'), { target: { value: 'page' } });
    await waitFor(() => expect(screen.queryByTestId('field-sheet_name')).not.toBeInTheDocument());
    expect(screen.getByTestId('field-max_bytes')).toHaveValue('8');
    expect(screen.getByTestId('field-timeout_ms')).toHaveValue('45');
    await run();
    expect(execute.mock.calls.at(-1)![0]).not.toHaveProperty('sheet_name');
  });
  it('preserves a saved browser/WARC request and exact output rename', async () => {
    const params = { source: 'url', output_mode: 'page', render_mode: 'playwright',
      include_warc: true, max_bytes: 12_000_000, timeout_ms: 90_000 };
    const execute = form(saved(params, { page: 'Evidence HTML' }));
    expect(screen.getByTestId('field-include_warc')).toBeChecked();
    expect(screen.getByTestId('field-max_bytes')).toHaveValue('12');
    expect(screen.getByTestId('field-timeout_ms')).toHaveValue('90');
    await run();
    expect(execute.mock.calls[0][0].params).toEqual(params);
    expect(execute.mock.calls[0][0].output_names).toEqual({ page: 'Evidence HTML' });
  });
  it('preserves saved links scope and independent child-column names', async () => {
    const params = { source: 'url', output_mode: 'links', render_mode: 'static' };
    const names = { url: 'link', anchor_text: 'label', source_url: 'origin' };
    const execute = form(saved(params, names, 'Collected')); await run();
    expect(execute.mock.calls[0][0]).toMatchObject({ params, output_names: names, sheet_name: 'Collected',
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [4, 6] } });
  });
  it('refuses a saved sheet destination when actual preparation resolves column output', async () => {
    const execute = form(saved({ source: 'url', output_mode: 'page' }, { page: 'archive' }, 'Wrong destination'));
    expect(await screen.findByText(/saved sheet destination does not match/)).toBeVisible();
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    expect(execute).not.toHaveBeenCalled();
  });
  it('keeps omitted saved defaults omitted and clears WARC only on an explicit incompatible mode change', async () => {
    const execute = form(saved({ source: 'url' }, { page: 'page_archive' })); await run();
    expect(execute.mock.calls[0][0].params).toEqual({ source: 'url' });
    fireEvent.change(screen.getByTestId('field-render_mode'), { target: { value: 'playwright' } });
    fireEvent.click(screen.getByTestId('field-include_warc'));
    fireEvent.change(screen.getByTestId('field-render_mode'), { target: { value: 'static' } });
    await run();
    expect(execute.mock.calls.at(-1)![0].params).toMatchObject({ render_mode: 'static', include_warc: false });
  });
});
