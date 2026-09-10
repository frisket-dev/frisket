// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, createProjectApi } from '../../src/api/real';
import { isGeneratedActionCatalogEntry, type GeneratedActionDraft } from '../../src/api/types';
import { generatedActionTemplateFromCatalogEntry } from '../../src/actions/model';
import { GeneratedActionForm } from '../../src/components/action-panel/GeneratedActionForm';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';
import { servedActionCatalog } from '../support/servedActionCatalog';
import { sheetMeta } from '../support/actionFormFixtures';
import { columnDef } from '../support/domainFixtures';

const entry = servedActionCatalog().actions.find((item) => item.kind === 'cluster.values');
if (!entry || !isGeneratedActionCatalogEntry(entry)) throw new Error('Missing typed cluster catalog');
const template = generatedActionTemplateFromCatalogEntry(entry)!;
const api = createProjectApi('test-project');
const { render } = createWorkspaceTestHarness({ projectId: 'test-project', api: { projectApi: api } });
const sheet = sheetMeta([
  columnDef({ id: '11', name: 'name', type: 'text' }),
  columnDef({ id: '12', name: 'notes', type: 'text' }),
  columnDef({ id: '13', name: 'score', type: 'number' }),
], { id: '7', name: 'People', rowCount: 5 });
const HASH = `sha256:${'a'.repeat(64)}`;
const PREVIEW = { clusters: [{ key: 'jon smith', canonical: 'Jon Smith', size: 3,
  values: [{ value: 'Jon Smith', count: 2 }, { value: 'Smith, Jon', count: 1 }],
  rowIds: ['1', '2', '3'] }], count: 1, valueHash: HASH, method: 'fingerprint', semantic: false };
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

function form(params: Record<string, unknown> = {}, names = { canonical: 'name_canonical' }) {
  const onExecute = vi.fn();
  const resolveParams = vi.fn(async () => ({ diagnostics: {},
    logical_outputs: [{ key: 'canonical', column_type: 'text' }], creates_sheet: false }));
  const draft: GeneratedActionDraft = { action_id: 'cluster.values',
    scope: { kind: 'sheet_rows', sheet_id: 7 }, params: { source: 'name', ...params }, output_names: names };
  const props = { catalogEntry: entry, actionTemplate: template, sheet, selectedRowIds: ['2'],
    initialDraft: draft, running: false, resolveParams, onExecute, onClose: vi.fn() };
  const view = render(<GeneratedActionForm {...props} />);
  return { onExecute, rerenderRunning: (running: boolean) => view.rerender(<GeneratedActionForm {...props} running={running} />) };
}
async function preview() {
  fireEvent.click(screen.getByTestId('cluster-preview-button'));
  await screen.findByTestId('cluster-card');
}
async function commit() {
  await waitFor(() => expect(screen.getByTestId('cluster-commit-button')).toBeEnabled());
  fireEvent.click(screen.getByTestId('cluster-commit-button'));
}

describe('generated cluster review', () => {
  it('lists text-like sources, keeps naming in the host and requires preview', async () => {
    form();
    expect(within(screen.getByTestId('cluster-column-select')).getAllByRole('option')
      .map((item) => item.textContent)).toEqual(['name', 'notes']);
    expect(await screen.findByTestId('field-output-canonical')).toHaveValue('name_canonical');
    expect(screen.getByTestId('cluster-commit-button')).toBeDisabled();
  });
  it('writes the reviewed hash, canonical edits and exclusions over the whole column', async () => {
    const spy = vi.spyOn(api, 'clusterPreview').mockResolvedValue(PREVIEW);
    const { onExecute } = form();
    await preview();
    expect(spy).toHaveBeenCalledWith(expect.objectContaining({ sheetId: '7', inputColumn: 'name', method: 'fingerprint' }));
    fireEvent.change(screen.getByTestId('cluster-canonical-input'), { target: { value: 'Jonathan Smith' } });
    fireEvent.click(screen.getAllByTestId('cluster-member-checkbox')[1]);
    await commit();
    expect(onExecute.mock.calls[0][0]).toMatchObject({ action_id: 'cluster.values',
      scope: { kind: 'sheet_rows', sheet_id: 7 }, output_names: { canonical: 'name_canonical' },
      params: { source: 'name', review: { source_hash: HASH,
        canonical_overrides: { 'jon smith': 'Jonathan Smith' }, excluded_members: { 'jon smith': ['Smith, Jon'] } } } });
    expect(onExecute.mock.calls[0][0].scope).not.toHaveProperty('row_ids');
  });
  it.each([['semantic', 'threshold', 0.85], ['ngram_fingerprint', 'ngramSize', 2]])(
    'previews %s with only its applicable knob', async (method, key, value) => {
      const spy = vi.spyOn(api, 'clusterPreview').mockResolvedValue(PREVIEW);
      form();
      fireEvent.change(screen.getByTestId('cluster-method-select'), { target: { value: method } });
      await preview();
      expect(spy).toHaveBeenCalledWith(expect.objectContaining({ method, [key]: value }));
      expect(spy.mock.calls[0][0][key === 'threshold' ? 'ngramSize' : 'threshold']).toBeUndefined();
    });
  it('surfaces semantic unavailability without discarding other methods', async () => {
    vi.spyOn(api, 'clusterPreview').mockRejectedValue(new ApiError(400, 'No embedding backend', 'embedding_backend_unavailable'));
    form({ method: 'semantic' });
    fireEvent.click(screen.getByTestId('cluster-preview-button'));
    expect(await screen.findByTestId('cluster-semantic-unavailable')).toHaveTextContent('Fingerprint');
    expect(screen.getByTestId('cluster-commit-button')).toBeDisabled();
  });
  it('locks source and method while preview is pending', async () => {
    let complete!: (value: typeof PREVIEW) => void;
    vi.spyOn(api, 'clusterPreview').mockReturnValue(new Promise((resolve) => { complete = resolve; }));
    form();
    fireEvent.click(screen.getByTestId('cluster-preview-button'));
    expect(screen.getByTestId('cluster-column-select')).toBeDisabled();
    expect(screen.getByTestId('cluster-method-select')).toBeDisabled();
    complete(PREVIEW);
    await screen.findByTestId('cluster-card');
    expect(screen.getByTestId('cluster-column-select')).toBeEnabled();
  });
  it.each(['cluster-column-select', 'cluster-method-select', 'cluster-key-template'])(
    'invalidates reviewed groups when %s changes', async (field) => {
      vi.spyOn(api, 'clusterPreview').mockResolvedValue(PREVIEW);
      form(); await preview();
      fireEvent.change(screen.getByTestId(field), { target: { value: field === 'cluster-column-select'
        ? 'notes' : field === 'cluster-method-select' ? 'semantic' : '{{value|lower}}' } });
      expect(screen.queryByTestId('cluster-card')).not.toBeInTheDocument();
      expect(screen.getByTestId('cluster-commit-button')).toBeDisabled();
    });
  it('binds an advanced key to preview and writing, and omits an untouched key', async () => {
    const spy = vi.spyOn(api, 'clusterPreview').mockResolvedValue(PREVIEW);
    const { onExecute } = form(); await preview();
    expect(spy.mock.calls[0][0].keyTemplate).toBeUndefined();
    fireEvent.change(screen.getByTestId('cluster-key-template'), { target: { value: '{{value|lower}}' } });
    await preview(); await commit();
    expect(spy.mock.calls.at(-1)![0].keyTemplate).toBe('{{value|lower}}');
    expect(onExecute.mock.calls[0][0].params.key_template).toBe('{{value|lower}}');
  });
  it('re-previews saved edits and exclusions before binding a fresh source hash', async () => {
    vi.spyOn(api, 'clusterPreview').mockResolvedValue(PREVIEW);
    const { onExecute } = form({ source: 'notes', method: 'semantic', min_size: 3, threshold: 0.91,
      key_template: '{{value|lower}}', review: { source_hash: `sha256:${'b'.repeat(64)}`,
        canonical_overrides: { 'jon smith': 'Jonathan Smith' }, excluded_members: { 'jon smith': ['Smith, Jon'] } } },
    { canonical: 'notes_clustered' });
    expect(screen.getByTestId('cluster-column-select')).toHaveValue('notes');
    expect(screen.getByTestId('cluster-min-size')).toHaveValue(3);
    expect(within(screen.getByTestId('cluster-threshold-field')).getByRole('spinbutton')).toHaveValue(0.91);
    expect(screen.getByTestId('cluster-commit-button')).toBeDisabled();
    await preview();
    expect(screen.getByTestId('cluster-canonical-input')).toHaveValue('Jonathan Smith');
    expect(screen.getAllByTestId('cluster-member-checkbox')[1]).not.toBeChecked();
    await commit();
    expect(onExecute.mock.calls[0][0].params.review.source_hash).toBe(HASH);
    expect(onExecute.mock.calls[0][0].output_names).toEqual({ canonical: 'notes_clustered' });
  });
  it.each([
    { canonical_overrides: { missing: 'Someone' }, excluded_members: {} },
    { canonical_overrides: {}, excluded_members: { 'jon smith': ['Missing person'] } },
  ])('requires explicit repair of saved edits that no longer match: %j', async (review) => {
    vi.spyOn(api, 'clusterPreview').mockResolvedValue(PREVIEW);
    form({ review }); await preview();
    expect(screen.getByTestId('cluster-saved-review-repair')).toBeVisible();
    expect(screen.getByTestId('cluster-commit-button')).toBeDisabled();
    fireEvent.click(screen.getByTestId('cluster-saved-review-reset'));
    await waitFor(() => expect(screen.getByTestId('cluster-commit-button')).toBeEnabled());
  });
  it('keeps destination edits live without invalidating the reviewed groups', async () => {
    vi.spyOn(api, 'clusterPreview').mockResolvedValue(PREVIEW);
    const { onExecute } = form(); await preview();
    fireEvent.change(screen.getByTestId('field-output-canonical'), { target: { value: 'reviewed_names' } });
    await commit();
    expect(onExecute.mock.calls[0][0].output_names).toEqual({ canonical: 'reviewed_names' });
    expect(onExecute.mock.calls[0][0].params.review.source_hash).toBe(HASH);
  });
  it('blocks blank canonicals and keeps the reviewed draft available to queue during another run', async () => {
    vi.spyOn(api, 'clusterPreview').mockResolvedValue(PREVIEW);
    const { rerenderRunning } = form(); await preview();
    await userEvent.clear(screen.getByTestId('cluster-canonical-input'));
    expect(screen.getByTestId('cluster-canonical-blank')).toBeVisible();
    expect(screen.getByTestId('cluster-commit-button')).toBeDisabled();
    fireEvent.change(screen.getByTestId('cluster-canonical-input'), { target: { value: 'Name' } });
    await waitFor(() => expect(screen.getByTestId('cluster-commit-button')).toBeEnabled());
    rerenderRunning(true);
    expect(screen.getByTestId('cluster-commit-button')).toBeEnabled();
    expect(screen.getByTestId('cluster-commit-button')).toHaveTextContent('Queue');
    rerenderRunning(false);
    await waitFor(() => expect(screen.getByTestId('cluster-commit-button')).toBeEnabled());
  });
});
