// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { PreviewTable } from '../../src/components/PreviewTable';
import { createProjectApi } from '../../src/api/real';
import { createActionPreviewDomainApi } from '../../src/api/actionPreviewRuns';
import type { PreviewTableSampleResult } from '../../src/api/types';
import { parseTimelineValue } from '../../src/temporal/model';
import { parsePreviewTemporalValue } from '../../src/temporal/preview';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const { render } = createWorkspaceTestHarness({ projectId: 'preview', api: { projectApi: createProjectApi('preview') } });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const clock = { preview_clock_id: 'a'.repeat(32), fingerprint: `sha256:${'b'.repeat(64)}`, duration_ms: 3000 };
const value = { schema_version: 'frisket.preview_temporal.v1', column_type: 'timeline_points', timeline: clock,
  items: [{ id: 'marker', at_ms: 1500, label: '<img src="https://external.invalid/pixel">', metadata: { note: 'Retained' } }] };

it('renders scratch timestamps and labels without artifact links or editable selection', () => {
  const sample: PreviewTableSampleResult = { kind: 'table', previewId: 'sample', status: 'done',
    progress: { done: 1, total: 1 }, columns: [{ name: 'Markers', columnType: 'timeline_points', format: null, hidden: false, overwritesColumnId: null }],
    rows: [{ Markers: { value } }], sampled: 1, total: 1, warnings: [], error: null };
  const fetch = vi.fn();
  vi.stubGlobal('fetch', fetch);
  const { container } = render(<PreviewTable result={sample} />);
  expect(screen.getByTestId('preview-temporal-value')).toHaveTextContent('Preview timeline');
  expect(container).toHaveTextContent('0:01.500');
  expect(container).toHaveTextContent(value.items[0].label);
  expect(container).toHaveTextContent('Retained');
  expect(container.querySelector('img, a, button, input')).toBeNull();
  expect(fetch).not.toHaveBeenCalled();
  expect(parseTimelineValue(value, 'timeline_points')).toBeNull();
});

it('transports the preview-only envelope without converting it to opaque text', async () => {
  const responses = [
    { schema_version: 'frisket.action_preview.v1', preview_id: 'sample', total: null },
    { schema_version: 'frisket.action_preview.v1', preview_id: 'sample', status: 'done', progress: { done: 1, total: 1 },
      result: { kind: 'table', columns: [{ name: 'Markers', column_type: 'timeline_points' }], rows: [{ Markers: { value } }], sampled: 1, total: 1, warnings: [] } },
  ];
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(responses.shift()), { status: 200, headers: { 'Content-Type': 'application/json' } })));
  const api = createActionPreviewDomainApi({ errorFactory: (status) => new Error(String(status)) }, 'preview');
  await api.start({ action_id: 'derive.temporal_segments', scope: { kind: 'sheet_rows', sheet_id: 1 }, params: {}, output_names: {}, sheet_name: 'Clips', idempotency_key: 'preview' }, {});
  const result = await api.get('sample');
  expect(result).toMatchObject({ kind: 'table', rows: [{ Markers: { value } }] });
});

it('rejects mixed durable/scratch identities, wrong types, and invalid clock bounds', () => {
  expect(parsePreviewTemporalValue(value, 'timeline_points')).not.toBeNull();
  expect(parsePreviewTemporalValue(value, 'timeline_ranges')).toBeNull();
  expect(parsePreviewTemporalValue({ ...value, timeline: { ...clock, artifact_stable_id: 'source_artifact:pretend' } })).toBeNull();
  expect(parsePreviewTemporalValue({ ...value, timeline: { ...clock, duration_ms: 1000 } })).toBeNull();
  expect(parsePreviewTemporalValue({ ...value, schema_version: 'frisket.timeline_points.v1' })).toBeNull();
});
