// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { GeneratedActionCatalogEntry, GeneratedActionDraft } from '../../src/api/types';
import { actionTemplatesFromCatalog } from '../../src/actions/model';
import { GeneratedActionForm } from '../../src/components/action-panel/GeneratedActionForm';
import { buildActionHelp } from '../../src/components/action-panel/actionHelpModel';
import { columnDef } from '../support/domainFixtures';
import { completeCatalogPayload, sheetMeta } from '../support/actionFormFixtures';
import { syntheticActionCatalogEntry } from '../support/actionCatalogFixtures';

const ENTRY = syntheticActionCatalogEntry('map.find_visual_cuts') as GeneratedActionCatalogEntry;
const SHEET = sheetMeta([
  columnDef({ id: '1', name: 'first_video', type: 'video' }),
  columnDef({ id: '2', name: 'saved_video', type: 'video' }),
  columnDef({ id: '3', name: 'notes', type: 'text' }),
], { id: '7', rowCount: 5 });

function renderCuts(options: { initialDraft?: GeneratedActionDraft; sheet?: typeof SHEET } = {}) {
  const template = actionTemplatesFromCatalog(completeCatalogPayload([ENTRY]))
    .find((item) => item.kind === ENTRY.kind)!;
  const onExecute = vi.fn();
  render(<GeneratedActionForm catalogEntry={ENTRY} actionTemplate={template}
    sheet={options.sheet ?? SHEET} selectedRowIds={['3', '8']} running={false}
    initialDraft={options.initialDraft}
    resolveParams={async () => ({ diagnostics: {}, logical_outputs: ENTRY.ui_hints.logical_outputs })}
    onExecute={onExecute} onClose={vi.fn()} />);
  return { template, onExecute };
}

afterEach(cleanup);

describe('canonical visual-cuts generated form', () => {
  it('keeps its title, source-bound help and canonical launcher', () => {
    const { template } = renderCuts();
    expect(template.generatedAction).toBe(true);
    expect(template.name).toBe('Find visual cuts');
    expect(template.kind).toBe('map.find_visual_cuts');
    expect(buildActionHelp(template).how).toContain('editable scene-boundary timestamps');
    expect(screen.getByTestId('field-source')).toHaveValue('first_video');
    expect(screen.getByTestId('field-source')).not.toHaveTextContent('notes');
  });

  it.each(['run', 'preview'] as const)('emits exact typed names and selected rows for %s', async (mode) => {
    const { onExecute } = renderCuts();
    fireEvent.change(screen.getByTestId('field-output-cuts'), { target: { value: 'Visual cuts' } });
    await waitFor(() => expect(screen.getByTestId(`generated-action-${mode}`)).toBeEnabled());
    fireEvent.click(screen.getByTestId(`generated-action-${mode}`));
    expect(onExecute).toHaveBeenCalledWith({
      action_id: ENTRY.kind,
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [3, 8] },
      params: { source: 'first_video' },
      output_names: { cuts: 'Visual cuts' },
      idempotency_key: expect.any(String),
    }, mode);
  });

  it('restores the saved source and output name without legacy params', async () => {
    const { onExecute } = renderCuts({ initialDraft: {
      action_id: ENTRY.kind,
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [3, 8] },
      params: { source: 'saved_video' }, output_names: { cuts: 'Saved cuts' },
    } });
    expect(screen.getByTestId('field-source')).toHaveValue('saved_video');
    expect(screen.getByTestId('field-output-cuts')).toHaveValue('Saved cuts');
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    fireEvent.click(screen.getByTestId('generated-action-run'));
    expect(onExecute.mock.calls[0][0]).toMatchObject({
      params: { source: 'saved_video' }, output_names: { cuts: 'Saved cuts' },
    });
  });

  it('refuses a sheet without a video source', () => {
    renderCuts({ sheet: sheetMeta([columnDef({ id: '3', name: 'notes', type: 'text' })]) });
    expect(screen.getByTestId('generated-action-run')).toBeDisabled();
    expect(screen.getByTestId('generated-action-preview')).toBeDisabled();
  });
});
