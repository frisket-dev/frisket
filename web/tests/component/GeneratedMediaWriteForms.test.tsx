// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { generatedActionTemplateFromCatalogEntry } from '../../src/actions/model';
import { isGeneratedActionCatalogEntry, type GeneratedActionDraft } from '../../src/api/types';
import { GeneratedActionForm } from '../../src/components/action-panel/GeneratedActionForm';
import { servedActionCatalog } from '../support/servedActionCatalog';
import { sheetMeta } from '../support/actionFormFixtures';
import { columnDef } from '../support/domainFixtures';
import { renderYtdlpForm } from '../support/renderYtdlpForm';

afterEach(cleanup);
const catalog = servedActionCatalog();
const sheet = sheetMeta([
  columnDef({ id: '11', name: 'recording', type: 'video' }),
  columnDef({ id: '12', name: 'url', type: 'link' }),
], { id: '7', rowCount: 3 });

function form(draft: GeneratedActionDraft) {
  const entry = catalog.actions.find((item) => item.kind === draft.action_id);
  if (!entry || !isGeneratedActionCatalogEntry(entry)) throw new Error(`Missing typed ${draft.action_id}`);
  const onExecute = vi.fn();
  render(<GeneratedActionForm catalogEntry={entry}
    actionTemplate={generatedActionTemplateFromCatalogEntry(entry)!}
    sheet={sheet} running={false} onClose={() => {}} onExecute={onExecute}
    initialDraft={draft} hasExactRowScopeInitializer
    selectedRowIds={draft.scope.kind === 'sheet_rows' ? draft.scope.row_ids?.map(String) : []}
    resolveParams={async () => ({ diagnostics: {}, logical_outputs: entry.kind === 'temporal.extract_range'
      ? [{ key: 'clip', column_type: 'video' }, { key: 'clip_notes', column_type: 'timeline_annotations' }]
      : entry.ui_hints.logical_outputs })} />);
  return onExecute;
}
async function run() {
  await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
  fireEvent.click(screen.getByTestId('generated-action-run'));
}

it('enclosure action sends only force and explicit request-owned membership, without new output names', async () => {
  const draft: GeneratedActionDraft = { action_id: 'media.enclosure_materialize',
    scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [101, 102] }, params: { force: true }, output_names: {} };
  const execute = form(draft);
  await run();
  expect(execute).toHaveBeenCalledWith({ ...draft, idempotency_key: expect.any(String) }, 'run');
});

it('extract keeps exact scope, independent sibling names and rich saved selection in the canonical request', async () => {
  const entry = catalog.actions.find((item) => item.kind === 'temporal.extract_range')!;
  expect(entry.ui_hints.form).toBe('generated');
  expect(entry.ui_hints.dynamic_outputs).toBe(true);
  const draft: GeneratedActionDraft = { action_id: 'temporal.extract_range',
    scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [101] },
    params: { source: 'recording', selection: { kind: 'draft_range', start_ms: 1000,
      end_ms: 2000, repeat_for_rows: false } }, output_names: { clip: 'excerpt', clip_notes: 'excerpt_notes' } };
  const execute = form(draft);
  await run();
  expect(screen.getByTestId('field-output-clip')).toHaveValue('excerpt');
  expect(screen.getByTestId('field-output-clip_notes')).toHaveValue('excerpt_notes');
  expect(execute).toHaveBeenCalledWith({ ...draft, idempotency_key: expect.any(String) }, 'run');
});

it('split leaves sheet creation and final naming with the common request host', async () => {
  const draft: GeneratedActionDraft = { action_id: 'derive.temporal_segments',
    scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [101] }, sheet_name: 'Quoted clips',
    params: { source: 'recording', selection: { kind: 'draft_points',
      items: [{ at_ms: 1000, label: 'Opening' }], repeat_for_rows: false } },
    output_names: { clip: 'excerpt', source_range: 'origin' } };
  const execute = form(draft);
  await run();
  expect(execute).toHaveBeenCalledWith({ ...draft, idempotency_key: expect.any(String) }, 'run');
});

it('yt-dlp preserves hidden saved options and independent active sidecar names after editing', async () => {
  const extra_opts = { allsubtitles: true, writeinfojson: true, socket_timeout: 12,
    subtitlesformat: 'vtt/srt/best', writeautomaticsub: false };
  const { onExecute } = renderYtdlpForm({ sheet,
    initialDraft: { action_id: 'media.ytdlp_download',
      scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [101] },
      params: { source: 'url', media_type: 'video', extra_opts },
      output_names: { video: 'download', info: 'details', subtitles: 'captions_file', subtitles_text: 'captions' } },
    hasExactRowScopeInitializer: true, selectedRowIds: ['101'],
  });
  fireEvent.change(screen.getByTestId('youtube-adv-retries'), { target: { value: '3' } });
  await run();
  expect(onExecute.mock.calls[0][0]).toMatchObject({ action_id: 'media.ytdlp_download',
    scope: { kind: 'sheet_rows', sheet_id: 7, row_ids: [101] },
    params: { source: 'url', media_type: 'video', extra_opts: { ...extra_opts, retries: 3 } },
    output_names: { video: 'download', info: 'details', subtitles: 'captions_file', subtitles_text: 'captions' } });
});
