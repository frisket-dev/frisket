// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { PreviewTable } from '../../src/components/PreviewTable';
import { ActionPreviewBanner } from '../../src/components/ActionPreviewBanner';
import type { PreviewGridView } from '../../src/state/previewViewStore';
import type { PreviewTableSampleResult } from '../../src/api/types';
import { createProjectApi } from '../../src/api/real';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const { render } = createWorkspaceTestHarness({ projectId: 'table-project',
  api: { projectApi: createProjectApi('table-project') } });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const artifact = '/api/projects/table-project/actions/v1/preview/sample/artifacts/abc123';
function sample(type: string, value: string): PreviewTableSampleResult {
  return { kind: 'table', previewId: 'sample', status: 'done', progress: { done: 1, total: null },
    columns: [{ name: 'Sample', columnType: type, format: null, hidden: false, overwritesColumnId: null }],
    rows: [{ Sample: { value } }], sampled: 1, total: null, error: null, warnings: [] };
}

it('shows host scratch images using the ordinary read-only preview renderer', () => {
  render(<PreviewTable result={sample('image', artifact)} />);
  expect(screen.getByRole('table', { name: 'Preview sample' })).toBeVisible();
  expect(screen.getByRole('img', { name: 'Sample' })).toHaveAttribute('src', artifact);
  expect(screen.queryByRole('button', { name: /edit|retry/i })).toBeNull();
});

it('keeps fallback warnings visible as text alongside nullable output cells', () => {
  const result = sample('image', '');
  result.rows[0].Sample.value = null;
  result.warnings = ['Page image unavailable; text retained.', '<img src="https://external.invalid/pixel">'];
  const { container } = render(<PreviewTable result={result} />);
  expect(screen.getByTestId('preview-table-warnings')).toHaveTextContent(result.warnings[0]);
  expect(screen.getByTestId('preview-table-warnings')).toHaveTextContent(result.warnings[1]);
  expect(screen.getByTestId('preview-table')).toHaveTextContent('(empty)');
  expect(container.querySelector('img')).toBeNull();
});

it('preserves the ordinary open/download link for a scratch file', () => {
  render(<PreviewTable result={sample('file', artifact)} />);
  expect(screen.getByRole('link')).toHaveAttribute('href', artifact);
  expect(screen.getByRole('link')).toHaveAttribute('rel', 'noopener noreferrer');
});

it.each(['image', 'file'])('keeps durable %s blobs on the current project route', (type) => {
  const digest = 'a'.repeat(64);
  render(<PreviewTable result={sample(type, JSON.stringify({ blob: digest,
    mime: 'image/png', filename: 'existing.png' }))} />);
  const node = screen.getByRole(type === 'image' ? 'img' : 'link');
  expect(node).toHaveAttribute(type === 'image' ? 'src' : 'href',
    `/api/projects/table-project/blobs/${digest}`);
  if (type === 'file') expect(node).toHaveTextContent('existing.png');
});

it.each(['https://external.invalid/pixel.png', 'javascript:alert(1)',
  '/api/projects/other/actions/v1/preview/sample/artifacts/abc123',
  '/api/projects/table-project/actions/v1/preview/other/artifacts/abc123',
  `${artifact}/../../secret`])('does not load unowned media %s', (value) => {
  const { container } = render(<PreviewTable result={sample('image', value)} />);
  expect(container.querySelector('img, video, audio, a, iframe')).toBeNull();
  expect(screen.getByTestId('preview-table')).toHaveTextContent(value);
});

it('does not turn sampled text markup into fetchable content', () => {
  const result = sample('text', '<img src="https://external.invalid/pixel">');
  result.columns[0].format = 'markdown';
  const { container } = render(<PreviewTable result={result} />);
  expect(container.querySelector('img')).toBeNull();
  expect(container).toHaveTextContent(result.rows[0].Sample.value as string);
});

it('preserves structured timeline values in the no-fetch literal fallback', () => {
  const timeline = {
    schema_version: 'frisket.timeline_points.v1',
    timeline: { artifact_stable_id: 'source_artifact:original',
      fingerprint: `sha256:${'a'.repeat(64)}`, duration_ms: 30_000 },
    items: [{ id: 'cut', at_ms: 12_500, label: '<img src="https://external.invalid/pixel">' }],
  };
  const result = sample('timeline_points', '');
  result.rows[0].Sample.value = timeline;
  const fetch = vi.fn();
  vi.stubGlobal('fetch', fetch);
  const { container } = render(<PreviewTable result={result} />);
  expect(screen.getByTestId('preview-field-value')).toHaveTextContent(JSON.stringify(timeline));
  expect(container).not.toHaveTextContent('[object Object]');
  expect(container.querySelector('a, img, video, audio, iframe')).toBeNull();
  expect(fetch).not.toHaveBeenCalled();
  expect(result.rows[0].Sample.value).toBe(timeline);
});

it('shows indeterminate progress and sample-only counts, retaining Run and Close controls', () => {
  const onRun = vi.fn();
  const onClose = vi.fn();
  const view: PreviewGridView = { sheetId: null, previewId: 'sample', status: 'running',
    progress: { done: 2, total: null }, result: null, actionName: 'Import files',
    rowCount: 0, totalRows: null, error: null,
    req: { action_id: 'import.files', scope: { kind: 'project' }, params: {}, output_names: {},
      sheet_name: 'Files', idempotency_key: 'sample' } };
  const { rerender } = render(<ActionPreviewBanner view={view} onRun={onRun} onClose={onClose} />);
  expect(screen.getByTestId('preview-tab-stats')).toHaveTextContent(/^Previewing… 2$/);
  expect(screen.queryByTestId('preview-run-for-real-button')).toBeNull();
  rerender(<ActionPreviewBanner view={{ ...view, status: 'done', rowCount: 2 }} onRun={onRun} onClose={onClose} />);
  expect(screen.getByTestId('preview-tab-stats')).toHaveTextContent(/^Preview · 2 rows$/);
  fireEvent.click(screen.getByTestId('preview-run-for-real-button'));
  fireEvent.click(screen.getByTestId('preview-close-button'));
  expect(onRun).toHaveBeenCalledOnce();
  expect(onClose).toHaveBeenCalledOnce();
});

it('retains source-row known total count wording', () => {
  const view = { sheetId: '7', previewId: 'sample', status: 'done' as const,
    progress: { done: 2, total: 2 }, result: null, actionName: 'Extract',
    rowCount: 2, totalRows: 10, error: null,
    req: { action_id: 'map.extract', scope: { kind: 'sheet_rows' as const, sheet_id: 7 },
      params: {}, output_names: {}, idempotency_key: 'sample' } };
  render(<ActionPreviewBanner view={view} onRun={vi.fn()} onClose={vi.fn()} />);
  expect(screen.getByTestId('preview-tab-stats')).toHaveTextContent(/^Preview · 2 of 10 rows$/);
});
