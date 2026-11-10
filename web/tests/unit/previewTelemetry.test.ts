// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createPreviewViewStore } from '../../src/state/previewViewStore';
import { configureProductTelemetry, setTelemetryPreference } from '../../src/telemetry/productTelemetry';
import type { ActionExecutionRequest, PreviewTableSampleResult, PreviewSampleResult } from '../../src/api/open';

const network = vi.fn<typeof fetch>(async () => new Response(null, { status: 204 }));
const req: ActionExecutionRequest = { action_id: 'map.extract', scope: { kind: 'sheet_rows', sheet_id: 1 }, params: { prompt: 'Private instructions' }, output_names: { result: 'Private column' }, idempotency_key: 'private-request' };
const done: PreviewTableSampleResult = { previewId: 'preview-private', kind: 'table', status: 'done', progress: { done: 1, total: 1 }, sampled: 1, total: 1, columns: [], rows: [], error: null, warnings: [] };
const deps = () => ({ invalidateProjectData: vi.fn(), requestCostConfirmation: vi.fn() });
const events = () => network.mock.calls.map(([, init]) => JSON.parse(String(init?.body)));

beforeEach(() => {
  vi.useFakeTimers();
  localStorage.clear();
  configureProductTelemetry(true);
  setTelemetryPreference(true);
  network.mockClear();
  vi.stubGlobal('fetch', network);
});
afterEach(() => { configureProductTelemetry(false); vi.unstubAllGlobals(); vi.useRealTimers(); });

it('records a preview attempt and one observed result, not every progress poll', async () => {
  const getPreview = vi.fn().mockResolvedValueOnce({ ...done, status: 'running', progress: { done: 0, total: 1 } }).mockResolvedValue(done);
  const previews = createPreviewViewStore({ startPreview: vi.fn().mockResolvedValue({ previewId: 'preview-private', total: 1 }), getPreview, cancelPreview: vi.fn() }, 'private-project');
  try {
    await previews.openPreviewView(req, { id: '1', rowCount: 10 }, deps());
    await vi.advanceTimersByTimeAsync(1600);
    expect(getPreview).toHaveBeenCalledTimes(2);
    expect(events().map(e => e.type)).toEqual(['Action.previewStarted', 'Action.previewObserved']);
    expect(events()[1].properties).toMatchObject({ action: 'map.extract', result: 'success', failureCategory: 'none' });
    expect(JSON.stringify(events())).not.toMatch(/Private|private-/);
  } finally { previews.dispose(); }
});

it('records a failed launch without sending the error or request content', async () => {
  const previews = createPreviewViewStore({ startPreview: vi.fn().mockRejectedValue(new Error('Private provider response')), getPreview: vi.fn(), cancelPreview: vi.fn() }, 'p');
  try {
    await previews.openPreviewView(req, { id: '1', rowCount: 1 }, deps());
    expect(events().map(e => e.type)).toEqual(['Action.previewStarted', 'Action.previewObserved']);
    expect(events()[1].properties).toMatchObject({ action: 'map.extract', result: 'failed' });
    expect(JSON.stringify(events())).not.toContain('Private');
  } finally { previews.dispose(); }
});

it('does not turn a detached preview into installation telemetry', async () => {
  const previews = createPreviewViewStore({ startPreview: vi.fn().mockResolvedValue({ previewId: 'p', total: 1 }), getPreview: vi.fn().mockResolvedValue(done), cancelPreview: vi.fn() });
  try {
    await previews.openPreviewView(req, { id: '1', rowCount: 1 }, deps());
    await vi.advanceTimersByTimeAsync(750);
    expect(network).not.toHaveBeenCalled();
  } finally { previews.dispose(); }
});


it('reports partial row-overlay results without transmitting cell errors', async () => {
  const partial: PreviewSampleResult = { ...done, kind: 'row_overlay', sheetId: '1', rowIds: [1], rows: { '1': { result: { value: null, error: 'Private cell failure' } } } };
  const previews = createPreviewViewStore({ startPreview: vi.fn().mockResolvedValue({ previewId: 'preview-private', total: 1 }), getPreview: vi.fn().mockResolvedValue(partial), cancelPreview: vi.fn() }, 'p');
  try {
    await previews.openPreviewView(req, { id: '1', rowCount: 1 }, deps());
    await vi.advanceTimersByTimeAsync(750);
    expect(events().at(-1)?.properties).toMatchObject({ result: 'partial', failureCategory: 'unknown' });
    expect(JSON.stringify(events())).not.toContain('Private cell failure');
  } finally { previews.dispose(); }
});
