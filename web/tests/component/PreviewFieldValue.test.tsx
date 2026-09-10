// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { InspectDetailColumn } from '../../src/workbench/InspectDetailColumn';
import { createProjectApi } from '../../src/api/real';
import type { PreviewCellDetail, SheetMeta } from '../../src/api/types';
import { columnDef, row } from '../support/domainFixtures';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const { render } = createWorkspaceTestHarness({ projectId: 'preview-detail', api: { projectApi: createProjectApi('preview-detail') } });
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const column = columnDef({ id: 'existing', name: 'Cuts', type: 'text' });
const sheet = { id: '1', name: 'Videos', columns: [column], rowCount: 1 } as SheetMeta;
const sample: PreviewCellDetail = {
  column: { name: 'Cuts', columnType: 'timeline_points', format: null, hidden: false, overwritesColumnId: 'existing' },
  cell: {
    value: {
      schema_version: 'frisket.timeline_points.v1',
      timeline: { artifact_stable_id: 'source_artifact:never-persisted', fingerprint: `sha256:${'a'.repeat(64)}`, duration_ms: 30_000 },
      items: [{ id: 'cut', at_ms: 12_500 }],
    },
  },
};

function inspect(preview: PreviewCellDetail | null) {
  const edit = vi.fn();
  const retry = vi.fn();
  const fetch = vi.fn(() => { throw new Error('preview details must not resolve artifacts'); });
  vi.stubGlobal('fetch', fetch);
  const mounted = render(<InspectDetailColumn
    projectId="preview-detail" sheet={sheet} row={row({ existing: 'COMMITTED VALUE' })}
    preview={preview} onClose={vi.fn()} onEdit={edit} onRetryCell={retry}
    canPrev={false} canNext={false}
  />);
  return { ...mounted, edit, retry, fetch };
}

describe('sampled detail inspection', () => {
  it.each([null, 'existing'])('renders ephemeral timestamps for target %s without resolving an artifact', (overwritesColumnId) => {
    const { container, fetch, edit, retry } = inspect({ ...sample, column: { ...sample.column, overwritesColumnId } });
    expect(screen.getByTestId('preview-field-value')).toHaveTextContent('0:12.500');
    expect(screen.getByTestId('preview-field-value')).toHaveTextContent('0:30 total');
    expect(screen.getByTitle('source_artifact:never-persisted').tagName).toBe('SPAN');
    expect(container).not.toHaveTextContent('COMMITTED VALUE');
    expect(screen.queryByRole('button', { name: /edit|retry/i })).toBeNull();
    expect(container.querySelector('textarea, input, a')).toBeNull();
    expect(fetch).not.toHaveBeenCalled();
    expect(edit).not.toHaveBeenCalled();
    expect(retry).not.toHaveBeenCalled();
  });

  it.each([
    [{ value: null }, '(empty)'],
    [{ value: null, error: 'Detector failed' }, 'Detector failed'],
    [{ value: 'Sampled text' }, 'Sampled text'],
    [{ value: false }, 'false'],
    [{ value: 0 }, '0'],
  ])('does not fall back to committed values for %j', (cell, expected) => {
    const { container } = inspect({ column: { ...sample.column, columnType: 'text' }, cell });
    expect(screen.getByTestId('preview-field-value')).toHaveTextContent(expected);
    expect(container).not.toHaveTextContent('COMMITTED VALUE');
    expect(screen.queryByRole('button', { name: /edit|retry/i })).toBeNull();
  });

  it('retains ordinary committed details when there is no sample', () => {
    inspect(null);
    expect(screen.queryByTestId('preview-field-value')).toBeNull();
    expect(screen.getAllByText('COMMITTED VALUE').length).toBeGreaterThan(0);
  });
});
