// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { ApiError } from '../../src/api/contractErrors';
import type { GridApiPort } from '../../src/api/ports';
import type { ActionCatalogEntry, ActionTemplate, SheetMeta } from '../../src/api/types';
import { useWorkspaceExpectedAnswers } from '../../src/workspace/useWorkspaceExpectedAnswers';
import { sheetMeta } from '../support/actionFormFixtures';

const calls = vi.hoisted(() => ({ getLinks: vi.fn(), previewLink: vi.fn(), getContext: vi.fn(), saveLink: vi.fn(), setRule: vi.fn() }));
vi.mock('../../src/api/evaluation', () => ({ createEvaluationApi: () => ({ getLinks: calls.getLinks, previewLink: calls.previewLink, saveLink: calls.saveLink, setRule: calls.setRule }) }));
vi.mock('../../src/api/actionComparison', () => ({ createActionComparisonApi: () => ({ getContext: calls.getContext }) }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });
const sheets: SheetMeta[] = [
  sheetMeta([{ id: '11', name: 'answer', type: 'text', latestRunId: '101' }], { id: '1', name: 'Current', rowCount: 1 }),
  sheetMeta([{ id: '22', name: 'answer', type: 'text', latestRunId: '202' }], { id: '2', name: 'Other', rowCount: 1 }),
];
const showError = vi.fn();
function prepare() {
  calls.getLinks.mockResolvedValue({ links: [{ id: 3, action_id: 'map.ask', target_sheet_id: 1, expected_sheet_id: 2, target_sheet_name: 'Current', expected_sheet_name: 'Other', keys: [], fields: [], hidden: false }] });
  calls.previewLink.mockResolvedValue({ matched: 1 });
  calls.getContext.mockImplementation(async ({ source }) => ({ available: true, draft: { action_id: 'map.ask' }, sheet: { id: source.run_id === 101 ? 1 : 2, name: 'sheet' }, generated_columns: [] }));
  const getSheetData = vi.fn(async (id: string) => ({ rows: [{ id: id === '1' ? '10' : '20' }] }));
  return { getSheetData } as unknown as GridApiPort;
}
it('reads only current-sheet runs until the join is opened', async () => {
  const api = prepare();
  const { result } = renderHook(() => useWorkspaceExpectedAnswers('p', sheets[0], sheets, api, [], [], '', showError));
  await waitFor(() => expect(calls.getContext).toHaveBeenCalledTimes(1));
  expect(api.getSheetData).toHaveBeenCalledTimes(1);
  expect(api.getSheetData).toHaveBeenCalledWith('1', 0, 1, expect.anything());
  act(() => result.current.openJoin());
  await waitFor(() => expect(calls.getContext).toHaveBeenCalledTimes(2));
  expect(api.getSheetData).toHaveBeenCalledWith('2', 0, 1, expect.anything());
  expect(api.getSheetData).toHaveBeenCalledTimes(2);
});
it('does not reload links, match counts or run contexts for unrelated data refreshes', async () => {
  const api = prepare();
  const { rerender } = renderHook(({ version, current, all }) => useWorkspaceExpectedAnswers('p', current, all, api, [], [], String(version), showError),
    { initialProps: { version: 0, current: sheets[0], all: sheets } });
  await waitFor(() => expect(calls.previewLink).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(calls.getContext).toHaveBeenCalledTimes(1));
  rerender({ version: 0, current: { ...sheets[0], columns: [...sheets[0].columns] }, all: sheets.map(item => ({ ...item, columns: [...item.columns] })) });
  await act(async () => { await Promise.resolve(); });
  expect(calls.getLinks).toHaveBeenCalledTimes(1);
  expect(calls.previewLink).toHaveBeenCalledTimes(1);
  expect(calls.getContext).toHaveBeenCalledTimes(1);
});

it('offers the existing Translate engine comparison only when its catalog action exists', async () => {
  const api = prepare();
  const entries = [{ kind: 'map.translate', title: 'Translate', ui_hints: {} }] as ActionCatalogEntry[];
  const { result } = renderHook(() => useWorkspaceExpectedAnswers('p', sheets[0], sheets, api, entries, [], '', showError));
  expect(result.current.actionChoices).toEqual([expect.objectContaining({ kind: 'map.translate', group: 'rows' })]);
  expect(result.current.actionChoices).not.toEqual(expect.arrayContaining([expect.objectContaining({ kind: 'media.to_markdown' })]));
});

it('never gates scratch media compares on the current sheet or its columns', () => {
  const api = prepare();
  const entries = [
    { kind: 'media.ocr', title: 'OCR', ui_hints: {} },
    { kind: 'media.transcribe', title: 'Transcribe', ui_hints: {} },
    { kind: 'map.find_topic_sections', title: 'Find topic sections', ui_hints: {} },
    { kind: 'map.translate', title: 'Translate', ui_hints: {} },
  ] as ActionCatalogEntry[];
  const templates: ActionTemplate[] = [
    {
      kind: 'media.ocr',
      name: 'OCR',
      description: 'Read text from PDFs and images',
      defaultPrompt: '',
      defaultFields: [],
      sourceRequirements: [{
        id: 'source',
        mode: 'column',
        param: 'source',
        label: 'a file or image column',
        accepted_column_types: ['file', 'image'],
        min: 1,
      }],
    },
    {
      kind: 'media.transcribe',
      name: 'Transcribe',
      description: 'Transcribe audio',
      defaultPrompt: '',
      defaultFields: [],
      sourceRequirements: [{
        id: 'source',
        mode: 'column',
        param: 'source',
        label: 'an audio column',
        accepted_column_types: ['audio'],
        min: 1,
      }],
    },
    {
      kind: 'map.find_topic_sections',
      name: 'Find topic sections',
      description: 'Segment transcript',
      defaultPrompt: '',
      defaultFields: [],
      sourceRequirements: [{
        id: 'source',
        mode: 'column',
        param: 'source',
        label: 'a transcript column',
        accepted_column_types: ['file'],
        min: 1,
      }],
    },
    {
      kind: 'map.translate',
      name: 'Translate',
      description: 'Translate text',
      defaultPrompt: '',
      defaultFields: [],
      sourceRequirements: [{
        id: 'source',
        mode: 'column',
        param: 'source',
        label: 'a text column',
        accepted_column_types: ['text'],
        min: 1,
      }],
    },
  ];

  // Number-only sheet (no media or text columns): scratch media compares stay enabled; row translate requires a text column.
  const numberOnlySheet = sheetMeta([{ id: '11', name: 'amount', type: 'number' }], { id: '1', name: 'Amounts', rowCount: 2 });
  const { result: withNumberSheet } = renderHook(() =>
    useWorkspaceExpectedAnswers('p', numberOnlySheet, [numberOnlySheet], api, entries, templates, '', showError),
  );
  for (const kind of ['media.ocr', 'media.transcribe', 'map.find_topic_sections']) {
    expect(withNumberSheet.current.actionChoices.find((choice) => choice.kind === kind)).toEqual({
      kind,
      label: expect.any(String),
      group: 'media',
    });
  }
  expect(withNumberSheet.current.actionChoices.find((choice) => choice.kind === 'map.translate')?.disabledReason)
    .toBe('Needs a text column');

  // No sheet selected: scratch media compares stay enabled; row translate requires a sheet.
  const { result: withoutSheet } = renderHook(() =>
    useWorkspaceExpectedAnswers('p', undefined, [], api, entries, templates, '', showError),
  );
  for (const kind of ['media.ocr', 'media.transcribe', 'map.find_topic_sections']) {
    expect(withoutSheet.current.actionChoices.find((choice) => choice.kind === kind)).not.toHaveProperty('disabledReason');
  }
  expect(withoutSheet.current.actionChoices.find((choice) => choice.kind === 'map.translate')?.disabledReason)
    .toBe('Choose a sheet');

  // Scratch media compares still respect unavailable_reason and missing_credentials.
  const gatedEntries = [
    { kind: 'media.ocr', title: 'OCR', ui_hints: { unavailable_reason: 'OCR runtime disabled' } },
    { kind: 'media.transcribe', title: 'Transcribe', ui_hints: { missing_credentials: ['OPENAI_API_KEY'] } },
  ] as ActionCatalogEntry[];
  const { result: gated } = renderHook(() =>
    useWorkspaceExpectedAnswers('p', undefined, [], api, gatedEntries, templates, '', showError),
  );
  expect(gated.current.actionChoices.find((choice) => choice.kind === 'media.ocr')?.disabledReason)
    .toBe('OCR runtime disabled');
  expect(gated.current.actionChoices.find((choice) => choice.kind === 'media.transcribe')?.disabledReason)
    .toBe('Set up credentials first');
});

it('updates only explicitly changed rules after saving an existing link', async () => {
  const api = prepare();
  calls.saveLink.mockResolvedValue({}); calls.setRule.mockResolvedValue({});
  const { result } = renderHook(() => useWorkspaceExpectedAnswers('p', sheets[0], sheets, api, [], [], '', showError));
  await waitFor(() => expect(result.current.links).toHaveLength(1));
  const link = result.current.links[0];
  act(() => result.current.openJoin(undefined, link, 'columns'));
  await act(async () => result.current.saveLink(link, [{ expected_column_id: 22, rule: 'number' }]));
  expect(calls.saveLink).toHaveBeenCalledWith(link, 3);
  expect(calls.setRule).toHaveBeenCalledExactlyOnceWith(22, 'number');
});

it('keeps unavailable links visible for removal but omits their virtual columns and previews', async () => {
  const api = prepare();
  calls.getLinks.mockResolvedValue({ links: [{ id: 4, action_id: 'map.ask', target_sheet_id: 1, expected_sheet_id: 2, target_sheet_name: 'Current', expected_sheet_name: 'Other', keys: [], fields: [{ expected_column_id: 22, generated_column_id: 11, logical_key: 'answer', rule: 'exact', hidden: false, source: 'import' }], hidden: false, available: false, unavailable_reason: 'Expected sheet was removed' }] });
  const { result } = renderHook(() => useWorkspaceExpectedAnswers('p', sheets[0], sheets, api, [], [], '', showError));
  await waitFor(() => expect(result.current.unavailableLinks).toHaveLength(1));
  expect(result.current.expectedColumns).toEqual([]);
  expect(result.current.availableLinks).toEqual([]);
  expect(calls.previewLink).not.toHaveBeenCalled();
});

it('keeps same-sheet links during refresh without leaking them across sheet switches', async () => {
  const api = prepare();
  const first = { id: 3, action_id: 'map.ask', target_sheet_id: 1, expected_sheet_id: 2, target_sheet_name: 'Current', expected_sheet_name: 'Other', keys: [], fields: [{ expected_column_id: 22, generated_column_id: 11, logical_key: 'answer', rule: 'exact', hidden: false, source: 'import' }], hidden: false };
  let finishRefresh!: (value: { links: typeof first[] }) => void;
  calls.getLinks.mockResolvedValueOnce({ links: [first] }).mockImplementationOnce(() => new Promise(resolve => { finishRefresh = resolve; })).mockImplementation(() => new Promise(() => {}));
  const { result, rerender } = renderHook(({ project, current }) => useWorkspaceExpectedAnswers(project, current, sheets, api, [], [], '', showError),
    { initialProps: { project: 'p', current: sheets[0] } });
  await waitFor(() => expect(result.current.expectedColumns).toHaveLength(1));
  act(() => result.current.refresh());
  await waitFor(() => expect(calls.getLinks).toHaveBeenCalledTimes(2));
  expect(result.current.expectedColumns).toHaveLength(1);
  rerender({ project: 'p', current: sheets[1] });
  expect(result.current.links).toEqual([]);
  expect(result.current.expectedColumns).toEqual([]);
  await act(async () => finishRefresh({ links: [first] }));
  expect(result.current.links).toEqual([]);
  rerender({ project: 'other-project', current: sheets[0] });
  expect(result.current.links).toEqual([]);
});


it('discovers a run after its pending context becomes available on completion', async () => {
  const api = prepare();
  calls.getContext.mockRejectedValueOnce(new ApiError(409, 'Run is still pending'))
    .mockResolvedValueOnce({ available: true, draft: { action_id: 'map.ask' },
      sheet: { id: 1, name: 'Current' },
      generated_columns: [{ column_id: 11, current_name: 'answer', logical_key: 'answer' }] });
  const { result, rerender } = renderHook(({ version }) => useWorkspaceExpectedAnswers('p', sheets[0], sheets, api, [], [], String(version), showError),
    { initialProps: { version: 0 } });
  await waitFor(() => expect(calls.getContext).toHaveBeenCalledTimes(1));
  expect(result.current.joinActions).toEqual([]);
  rerender({ version: 1 });
  await waitFor(() => expect(result.current.joinActions).toEqual([expect.objectContaining({ actionId: 'map.ask' })]));
  expect(calls.getContext).toHaveBeenCalledTimes(2);
  rerender({ version: 2 });
  await act(async () => { await Promise.resolve(); });
  expect(calls.getContext).toHaveBeenCalledTimes(2);
});


it('keeps one discovery in flight across row updates and retries after completion', async () => {
  const api = prepare();
  let finish!: (value: unknown) => void;
  calls.getContext.mockImplementationOnce(() => new Promise((_resolve, reject) => { finish = () => reject(new ApiError(409, 'Run pending')); }))
    .mockResolvedValueOnce({ available: true, draft: { action_id: 'map.ask' }, sheet: { id: 1, name: 'Current' }, generated_columns: [] });
  const { result, rerender } = renderHook(({ signal, sheet }) => useWorkspaceExpectedAnswers('p', sheet, sheets, api, [], [], signal, showError),
    { initialProps: { signal: '', sheet: sheets[0] } });
  await waitFor(() => expect(calls.getContext).toHaveBeenCalledTimes(1));
  for (let i = 0; i < 5; i++) rerender({ signal: '', sheet: { ...sheets[0], columns: [...sheets[0].columns] } });
  rerender({ signal: '101:complete', sheet: sheets[0] });
  expect(calls.getContext).toHaveBeenCalledTimes(1);
  await act(async () => finish(undefined));
  await waitFor(() => expect(result.current.joinActions).toEqual([expect.objectContaining({ actionId: 'map.ask' })]));
  expect(calls.getContext).toHaveBeenCalledTimes(2);
});

it('accepts an in-flight successful discovery despite intervening completion updates', async () => {
  const api = prepare();
  let finish!: (value: unknown) => void;
  calls.getContext.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const { result, rerender } = renderHook(({ signal }) => useWorkspaceExpectedAnswers('p', sheets[0], sheets, api, [], [], signal, showError),
    { initialProps: { signal: '' } });
  await waitFor(() => expect(calls.getContext).toHaveBeenCalledTimes(1));
  rerender({ signal: 'other:complete' });
  rerender({ signal: 'other:complete,third:complete' });
  expect(calls.getContext).toHaveBeenCalledTimes(1);
  await act(async () => finish({ available: true, draft: { action_id: 'map.ask' }, sheet: { id: 1, name: 'Current' }, generated_columns: [] }));
  await waitFor(() => expect(result.current.joinActions).toEqual([expect.objectContaining({ actionId: 'map.ask' })]));
  expect(calls.getContext).toHaveBeenCalledTimes(1);
});
