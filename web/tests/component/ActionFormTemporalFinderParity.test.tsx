// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { EngineOption, GeneratedActionCatalogEntry,
  GeneratedActionDraft } from '../../src/api/types';
import { actionTemplatesFromCatalog } from '../../src/actions/model';
import { GeneratedActionForm, type GeneratedActionFormProps } from '../../src/components/action-panel/GeneratedActionForm';
import { columnDef } from '../support/domainFixtures';
import { completeCatalogPayload, sheetMeta } from '../support/actionFormFixtures';
import { syntheticActionCatalogEntry } from '../support/actionCatalogFixtures';

afterEach(cleanup);
const transcriptSheet = sheetMeta([
  columnDef({ id: '2', name: 'transcript', type: 'timestamped_transcript' }),
], { id: '7', rowCount: 3 });
const availableTopicEngines: EngineOption[] = [
  { id: 'deep_tiling', label: 'DeepTiling', tier: 'local', available: true, recommended: true },
  { id: 'texttiling', label: 'TextTiling', tier: 'local', available: true },
];
const entry = syntheticActionCatalogEntry('map.find_topic_sections', {
  title: 'Find topic changes',
  input_schema: { type: 'object', additionalProperties: false, required: ['source'],
    properties: { source: { type: 'string' }, engine: { type: 'string', default: 'texttiling' },
      settings: { type: 'object', default: {} } } },
  required_capabilities: ['project:write'], cost_policy: { kind: 'none', requires_confirmation: false },
  ui_hints: { form: 'generated', category: 'extract', semantic_controls: { source: 'column', engine: 'engine' },
    engines: availableTopicEngines, logical_outputs: [{ key: 'sections', column_type: 'timeline_ranges' }],
    source_requirements: [{ id: 'source', mode: 'column', param: 'source', min: 1, max: 1,
      label: 'Timestamped transcript', accepted_column_types: ['timestamped_transcript'] }],
  },
}) as GeneratedActionCatalogEntry;
const catalog = completeCatalogPayload([entry]);
const template = actionTemplatesFromCatalog(catalog)
  .find((candidate) => candidate.kind === 'map.find_topic_sections')!;
function form(overrides: Partial<GeneratedActionFormProps> = {}) {
  const onExecute = vi.fn();
  render(<GeneratedActionForm catalogEntry={entry} actionTemplate={template}
    sheet={transcriptSheet} running={false}
    resolveParams={async () => ({ diagnostics: {}, logical_outputs: entry.ui_hints.logical_outputs })}
    onExecute={onExecute} onClose={() => {}} {...overrides} />);
  return onExecute;
}
async function ready() {
  await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
}

describe('typed topic sections authoring', () => {
  it('uses the declared default engine, compatible source, detail and visible output', async () => {
    const onExecute = form();
    expect(screen.getByTestId('field-detail')).toHaveValue('balanced');
    expect(screen.getByTestId('field-output-sections')).toHaveValue('sections');
    await ready();
    fireEvent.change(screen.getByTestId('field-output-sections'), { target: { value: 'Topic sections' } });
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute.mock.calls[0][0]).toMatchObject({ action_id: 'map.find_topic_sections',
      scope: { kind: 'sheet_rows', sheet_id: 7 },
      params: { source: 'transcript', engine: 'texttiling', settings: {} },
      output_names: { sections: 'Topic sections' } });
    expect(onExecute.mock.calls[0][0].params).not.toHaveProperty('output_name');
  });

  it.each(['fewer', 'balanced', 'more'])('edits settings.detail=%s without discarding other saved settings', async (detail) => {
    const initialDraft: GeneratedActionDraft = { action_id: 'map.find_topic_sections',
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [3, 8] },
      params: { source: 'transcript', engine: 'texttiling', settings: { detail: 'more', window_size: 8 } },
      output_names: { sections: 'Saved sections' } };
    const onExecute = form({ initialDraft, selectedRowIds: ['3', '8'] });
    expect(screen.getByTestId('field-detail')).toHaveValue('more');
    fireEvent.change(screen.getByTestId('field-detail'), { target: { value: detail } });
    await ready();
    fireEvent.click(screen.getByTestId('generated-action-preview'));
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute).toHaveBeenCalledTimes(2);
    for (const [request] of onExecute.mock.calls) {
      expect(request).toMatchObject({ ...initialDraft,
        params: { ...initialDraft.params, settings: { detail, window_size: 8 } } });
      expect(request).not.toHaveProperty('confirmation');
    }
    expect(onExecute.mock.calls.map((call) => call[1])).toEqual(['preview', 'run']);
  });

  it('edits the full saved settings object and refuses unfinished JSON', async () => {
    form();
    const settings = screen.getByTestId('field-settings');
    fireEvent.change(settings, { target: { value: '{' } });
    expect(settings).toHaveValue('{');
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    fireEvent.change(settings, { target: { value: '{"detail":"fewer"}' } });
    expect(screen.getByTestId('field-detail')).toHaveValue('fewer');
    await ready();
  });

  it('blocks missing compatible transcript inputs', async () => {
    const onExecute = form({ sheet: sheetMeta([columnDef({ name: 'notes', type: 'text' })]) });
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute).not.toHaveBeenCalled();
  });

  it('deduplicates fresh names but refuses a colliding authored output', async () => {
    form({ sheet: sheetMeta([...transcriptSheet.columns,
      columnDef({ id: '8', name: 'sections', type: 'timeline_ranges' })], { id: '7' }) });
    expect(screen.getByTestId('field-output-sections')).toHaveValue('sections_2');
    fireEvent.change(screen.getByTestId('field-output-sections'), { target: { value: 'sections' } });
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
  });

  it('keeps unavailable engines visible, blocks Run and offers host Diagnose', async () => {
    const onOpenDiagnose = vi.fn();
    const onExecute = form({ actionTemplate: { ...template, engines: [{
      id: 'texttiling', label: 'TextTiling', tier: 'local', available: false,
      error: 'Install the segmentation extra.',
    }] }, onOpenDiagnose });
    expect(await screen.findByRole('alert')).toHaveTextContent('Install the segmentation extra.');
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    fireEvent.click(screen.getByText('Engine availability'));
    expect(screen.getByTestId('engine-availability-line-texttiling')).toHaveTextContent('unavailable');
    fireEvent.click(screen.getByTestId('engine-availability-open-diagnose'));
    expect(onOpenDiagnose).toHaveBeenCalledOnce();
    expect(onExecute).not.toHaveBeenCalled();
  });

  it('blocks an empty engine list without silently changing a saved engine', async () => {
    form({ actionTemplate: { ...template, engines: [] } });
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    expect(screen.getByRole('alert')).toHaveTextContent('texttiling is unavailable.');
  });

  it('retains selected-row preview and queue-while-running through the shared footer', async () => {
    const onExecute = form({ selectedRowIds: ['3', '8'], running: true });
    await ready();
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute.mock.calls[0][0].scope).toEqual({ kind: 'sheet_rows', sheet_id: 7, row_ids: [3, 8] });
    expect(screen.queryByTestId('debug-trace-toggle')).not.toBeInTheDocument();
  });

  it('reports local cost without invoking an estimate provider', async () => {
    const estimateAction = vi.fn();
    form({ estimateAction });
    await ready();
    expect(estimateAction).not.toHaveBeenCalled();
    expect(screen.getByTestId('cost-estimate')).toHaveTextContent('$0.00');
  });
});
