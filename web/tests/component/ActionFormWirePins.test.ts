// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor, render } from '@testing-library/react';
import { createElement } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { actionTemplatesFromCatalog } from '../../src/actions/model';
import type { GeneratedActionDraft } from '../../src/api/types';
import { WorkspaceStoresContext } from '../../src/bind/workspaceStoresContext';
import { ActionPanel } from '../../src/components/ActionPanel';
import { createWorkspaceStores, type WorkspaceStores } from '../../src/state/createWorkspaceStores';
import { aiMeta, columnDef } from '../support/domainFixtures';
import { sheetMeta } from '../support/actionFormFixtures';
import { servedActionCatalog } from '../support/servedActionCatalog';
import { createProjectApi } from '../../src/api/real';

let api = createProjectApi('test-project');

// Backend-served truth: the pinned envelopes below are exact typed requests
// against the served schemas, not handcrafted recipe fixtures.
const servedCatalog = servedActionCatalog();
const catalog = {
  ...servedCatalog,
  actions: servedCatalog.actions.map((entry) => (
    entry.ui_hints.engines
      ? { ...entry, ui_hints: { ...entry.ui_hints,
        engines: entry.ui_hints.engines.map((engine) => ({ ...engine, available: true })) } }
      : entry
  )),
};
const resolvedTemplates = actionTemplatesFromCatalog(catalog);

/** Logical outputs the backend resolves for each pinned action. */
const RESOLVED_OUTPUTS: Record<string, { key: string; column_type: string }[]> = {
  'media.ytdlp_download': [
    { key: 'audio', column_type: 'audio' }, { key: 'subtitles', column_type: 'json' },
    { key: 'subtitles_text', column_type: 'text' },
  ],
  'map.clean_column': [{ key: 'cleaned', column_type: 'text' }],
  'map.clean_dates': [{ key: 'cleaned', column_type: 'text' }],
  'map.translate': [{ key: 'translation', column_type: 'text' }],
  'map.ner': [{ key: 'entities', column_type: 'json' }],
  'reduce.group_summary': [],
};

const SHEET_DATA_WIRE = {
  total: 2,
  columns: [
    {
      id: 11,
      name: 'source',
      type: 'text',
      ai_generated: false,
      format: null,
      current_run_id: null,
      transcript_status: null,
      media_download_candidate: null,
    },
    {
      id: 12,
      name: 'generated_context',
      type: 'text',
      ai_generated: true,
      format: null,
      current_run_id: 41,
      transcript_status: null,
      media_download_candidate: null,
    },
    {
      id: 14,
      name: 'summary_existing',
      type: 'text',
      ai_generated: true,
      format: null,
      current_run_id: 42,
      transcript_status: null,
      media_download_candidate: null,
    },
  ],
  rows: [
    { id: 3, cells: {}, meta: {}, parent_row_id: null, child_count: 0 },
    { id: 7, cells: {}, meta: {}, parent_row_id: null, child_count: 0 },
  ],
};

const RUN_COMPLETED_WIRE = {
  schema_version: 'frisket.action_result.v1',
  status: 'completed',
  run_id: null,
  job_id: null,
  receipt_id: null,
  outputs: [],
  errors: [],
};

const PANEL_SHEET = sheetMeta(
  [
    columnDef({ id: '11', name: 'source', type: 'text' }),
    columnDef({
      id: '12',
      name: 'generated_context',
      type: 'text',
      ai: aiMeta({ actionName: 'Generate context' }),
    }),
    columnDef({
      id: '14',
      name: 'summary_existing',
      type: 'text',
      ai: aiMeta({ actionName: 'Summarize' }),
    }),
  ],
  { id: '1', rowCount: 2 },
);

let stores: WorkspaceStores | null = null;

/** Mount ActionPanel either on a launcher route (`actionKind`) or on a saved
 * typed envelope (`inspectProposalSpec`) and return the exact POST body the
 * generated form emits for Run. */
async function postedBodyFromDispatch({
  actionKind,
  wireId,
  prepare,
  inspectProposalSpec,
}: {
  actionKind:
    | 'map.clean_column'
    | 'map.clean_dates'
    | 'map.translate'
    | 'map.ner'
    | 'reduce.group_summary'
    | 'media.ytdlp_download';
  wireId: string;
  prepare?: () => void;
  inspectProposalSpec?: GeneratedActionDraft;
}): Promise<string> {
  api = createProjectApi('p1');
  let runInit: RequestInit | undefined;
  vi.stubGlobal('crypto', { randomUUID: () => wireId });
  vi.spyOn(api, 'resolveActionParams').mockImplementation(async (request) => ({
    diagnostics: {},
    ...(request.action_id === 'reduce.group_summary' ? { creates_sheet: true } : {}),
    logical_outputs: RESOLVED_OUTPUTS[request.action_id] ?? [],
  }));
  vi.spyOn(api, 'estimateAction').mockResolvedValue({ cost: 0, rows: 2, llm: true });
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? 'GET').toUpperCase();
      if (method === 'GET' && url.startsWith('/api/projects/p1/sheets/1/data')) {
        return new Response(JSON.stringify(SHEET_DATA_WIRE), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        });
      }
      if (method === 'POST' && url === '/api/projects/p1/actions/v1/validate-params') {
        return new Response(JSON.stringify({
          schema_version: 'frisket.action_param_validation_result.v1',
          action: { kind: actionKind },
          project_id: 'p1',
          diagnostics: {},
          logical_outputs: [],
        }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        });
      }
      if (method === 'POST' && url === '/api/projects/p1/actions/v1/run') {
        runInit = init;
        return new Response(JSON.stringify(RUN_COMPLETED_WIRE), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        });
      }
      throw new Error(`Unexpected fetch: ${method} ${url}`);
    }),
  );

  stores = createWorkspaceStores('p1', api);
  stores.actionCatalog.store.set(() => ({
    status: 'ready',
    error: null,
    catalog,
    resolvedTemplates,
    version: 1,
  }));

  let runPromise: Promise<unknown> | null = null;
  const onRun = vi.fn();
  render(
    createElement(
      WorkspaceStoresContext.Provider,
      { value: stores },
      createElement(ActionPanel, {
        sheet: PANEL_SHEET,
        running: false,
        ...(inspectProposalSpec
          ? {
              inspectProposal: {
                seq: 1,
                title: 'Summarize proposal',
                spec: inspectProposalSpec,
              },
            }
          : {
              routeActionKind: actionKind,
              routeActionInitial: { sourceColumn: 'source' },
              routeActionLaunchId: 1,
            }),
        onRun,
        onExecuteRegisteredAction: (request, mode) => {
          if (mode === 'run') runPromise = api.runAction(request);
        },
      }),
    ),
  );

  const runButtonTestId = 'generated-action-run';
  await waitFor(() => expect(screen.getByTestId(runButtonTestId)).toBeEnabled());
  prepare?.();
  await waitFor(() => expect(screen.getByTestId(runButtonTestId)).toBeEnabled());
  fireEvent.click(screen.getByTestId(runButtonTestId));
  // Typed actions never fall back to the generic dispatch callback.
  expect(onRun).not.toHaveBeenCalled();

  await waitFor(() => expect(runInit).toBeDefined());
  await runPromise;
  if (typeof runInit?.body !== 'string') throw new Error(`Missing ${actionKind} POST body`);
  return runInit.body;
}

afterEach(() => {
  cleanup();
  stores?.dispose();
  stores = null;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('ActionForm dispatch-routed whole-envelope wire pins', () => {
  it('preserves a Copilot-authored Translate language and non-default engine through Inspect', async () => {
    const body = await postedBodyFromDispatch({
      actionKind: 'map.translate',
      wireId: 'proposal-translate-inspect-wire-pin',
      inspectProposalSpec: {
        action_id: 'map.translate',
        scope: { kind: 'sheet_rows', sheet_id: 1 },
        params: {
          source: ['source'],
          target_language: 'French',
          engine: 'deepl',
        },
        output_names: { translation: 'translation' },
      },
    });

    expect(screen.getByTestId('model-picker-button')).toHaveTextContent('DeepL');
    expect(screen.getByTestId('field-target_language')).toHaveValue('French');
    expect(JSON.parse(body)).toEqual({
      action_id: 'map.translate',
      scope: { kind: 'sheet_rows', sheet_id: 1 },
      params: {
        source: ['source'],
        target_language: 'French',
        engine: 'deepl',
      },
      output_names: { translation: 'translation' },
      idempotency_key: 'web-map.translate:proposal-translate-inspect-wire-pin',
    });
  });

  it('preserves exact Copilot NER label arrays through Inspect', async () => {
    const body = await postedBodyFromDispatch({
      actionKind: 'map.ner',
      wireId: 'proposal-ner-labels-inspect-wire-pin',
      inspectProposalSpec: {
        action_id: 'map.ner',
        scope: { kind: 'sheet_rows', sheet_id: 1 },
        params: {
          source: ['source'],
          engine: 'gliner',
          labels: ['person', 'organization, nonprofit', 'person'],
        },
        output_names: { entities: 'entities' },
      },
    });

    expect(screen.getByTestId('model-picker-button')).toHaveTextContent('GLiNER');
    expect(screen.getAllByTestId('ner-gliner-labels-chip').map((chip) => chip.textContent))
      .toEqual(['person', 'organization, nonprofit', 'person']);
    expect(JSON.parse(body)).toEqual({
      action_id: 'map.ner',
      scope: { kind: 'sheet_rows', sheet_id: 1 },
      params: {
        source: ['source'],
        engine: 'gliner',
        labels: ['person', 'organization, nonprofit', 'person'],
      },
      output_names: { entities: 'entities' },
      idempotency_key: 'web-map.ner:proposal-ner-labels-inspect-wire-pin',
    });
  });

  it('preserves an exact Copilot grouped-summary through Inspect', async () => {
    const body = await postedBodyFromDispatch({
      actionKind: 'reduce.group_summary',
      wireId: 'proposal-reduce-group-inspect-wire-pin',
      inspectProposalSpec: {
        action_id: 'reduce.group_summary',
        scope: { kind: 'sheet_rows', sheet_id: 1 },
        params: {
          source: ['generated_context'],
          group_by: 'source',
          instruction: 'Summarize each group.',
          model: 'openrouter/test-model',
        },
        output_names: {},
        sheet_name: 'Grouped proposal results',
      },
    });

    expect(screen.getByTestId('field-group_by')).toHaveValue('source');
    expect(JSON.parse(body)).toEqual({
      action_id: 'reduce.group_summary',
      scope: { kind: 'sheet_rows', sheet_id: 1 },
      params: {
        source: ['generated_context'],
        group_by: 'source',
        instruction: 'Summarize each group.',
        model: 'openrouter/test-model',
      },
      output_names: {},
      sheet_name: 'Grouped proposal results',
      idempotency_key: 'web-reduce.group_summary:proposal-reduce-group-inspect-wire-pin',
    });
  });

  it('preserves structured yt-dlp options and output names from a saved request through Inspect', async () => {
    const body = await postedBodyFromDispatch({
      actionKind: 'media.ytdlp_download',
      wireId: 'saved-ytdlp-extra-opts-inspect-wire-pin',
      prepare: () => {
        fireEvent.change(screen.getByTestId('field-output-audio'), {
          target: { value: 'edited_media' },
        });
      },
      inspectProposalSpec: {
        action_id: 'media.ytdlp_download',
        scope: { kind: 'sheet_rows', sheet_id: 1 },
        output_names: { audio: 'saved_media', subtitles: 'saved_subtitles', subtitles_text: 'saved_text' },
        params: {
          source: 'source',
          media_type: 'audio',
          extra_opts: {
            writesubtitles: true,
            subtitleslangs: ['en', 'fr'],
            retries: 5,
          },
        },
      },
    });

    expect(JSON.parse(body).scope).toEqual({ kind: 'sheet_rows', sheet_id: 1 });
    expect(JSON.parse(body).output_names).toEqual({
      audio: 'edited_media', subtitles: 'saved_subtitles', subtitles_text: 'saved_text',
    });
    expect(JSON.parse(body).params).toEqual({
      source: 'source',
      media_type: 'audio',
      extra_opts: {
        writesubtitles: true,
        subtitleslangs: ['en', 'fr'],
        retries: 5,
      },
    });
  });

  it('pins the complete map.clean_column pilot envelope', async () => {
    const body = await postedBodyFromDispatch({
      actionKind: 'map.clean_column',
      wireId: 'action-04b-clean-column-wire-pin',
      prepare: () => {
        fireEvent.change(screen.getByTestId('field-case'), { target: { value: 'keep' } });
        fireEvent.change(screen.getByTestId('field-null_tokens'), {
          target: { value: 'missing,unknown' },
        });
        fireEvent.click(screen.getByTestId('field-blank_null_tokens'));
      },
    });

    expect(JSON.parse(body)).toEqual({
      action_id: 'map.clean_column',
      scope: { kind: 'sheet_rows', sheet_id: 1 },
      params: {
        source: 'source',
        case: 'keep',
        null_tokens: 'missing,unknown',
        blank_null_tokens: false,
        lowercase_emails: true,
        normalize_us_phone: true,
        expand_abbreviations: true,
        reorder_person_name: true,
        canonicalize_duplicates: true,
        strip_edge_punct: false,
        normalize_unicode_punct: false,
        remove_thousands_separators: false,
        remove_all_commas: false,
        make_numeric: false,
      },
      output_names: { cleaned: 'source_clean' },
      idempotency_key: 'web-map.clean_column:action-04b-clean-column-wire-pin',
    });
  });

  it('pins the complete map.clean_dates pilot envelope', async () => {
    const body = await postedBodyFromDispatch({
      actionKind: 'map.clean_dates',
      wireId: 'action-04b-clean-dates-wire-pin',
      prepare: () => {
        fireEvent.change(screen.getByTestId('clean-dates-format-select'), {
          target: { value: '%d/%m/%Y' },
        });
        fireEvent.change(screen.getByTestId('field-output-cleaned'), {
          target: { value: 'date_iso' },
        });
      },
    });

    expect(JSON.parse(body)).toEqual({
      action_id: 'map.clean_dates',
      scope: { kind: 'sheet_rows', sheet_id: 1 },
      params: {
        source: 'source',
        format: '%d/%m/%Y',
      },
      output_names: { cleaned: 'date_iso' },
      idempotency_key: 'web-map.clean_dates:action-04b-clean-dates-wire-pin',
    });
  });
});
