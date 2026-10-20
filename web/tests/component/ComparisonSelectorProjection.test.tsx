// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { useMemo } from 'react';
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll, expect, it, vi } from 'vitest';
import { selectorChoicesApi, type HttpSelectorChoicesResponse } from '../../src/api/selectorChoices';
import { SelectorField } from '../../src/engine-selector/SelectorField';
import { useCompareSelectorReadiness } from '../../src/workbench/useCompareSelectorReadiness';
import type { CompareColumn } from '../../src/workbench/mediaCompareSession';
import { installDialogPolyfill } from '../support/domPolyfills';
import { mediaChoices } from '../support/mediaSelectorFixtures';

beforeAll(installDialogPolyfill);
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

function Harness({ column, projectId = 'project-a' }: { column: CompareColumn; projectId?: string }) {
  const columns = useMemo(() => [column], [column]);
  const { selectorOwners, selectorProjection, isRunnable } = useCompareSelectorReadiness(projectId, 'media.ocr', columns);
  const projection = selectorProjection(column);
  return <>{selectorOwners}
    <output data-testid="ready">{String(isRunnable(column))}</output>
    <button onClick={() => { void projection.refresh(); }}>Refresh</button>
    <SelectorField projectId={projectId} label="Engine" recentNamespace="projection-test"
      query={{ schema_version: 'frisket.selector_choices_query.v1', subject: { kind: 'project_ask' } }}
      projection={projection} onSelect={() => undefined} />
  </>;
}
const column: CompareColumn = { id: 'one', engineId: 'local', options: { dpi: 200 } };

it('shares one request and refresh failure/recovery between the open picker and Run', async () => {
  const user = userEvent.setup();
  const load = vi.spyOn(selectorChoicesApi, 'getSelectorChoices')
    .mockImplementation(async (_pid, query) => mediaChoices(query, ['local']));
  render(<Harness column={column} />);
  await waitFor(() => expect(screen.getByTestId('ready')).toHaveTextContent('true'));
  await user.click(screen.getByRole('button', { name: /local/ }));
  expect(await screen.findByTestId('engine-selector-dialog')).toBeVisible();
  expect(load).toHaveBeenCalledTimes(1);
  load.mockRejectedValueOnce(new Error('Refresh unavailable'));
  await user.click(screen.getByRole('button', { name: 'Refresh', hidden: true }));
  await waitFor(() => expect(screen.getByTestId('ready')).toHaveTextContent('false'));
  expect(screen.getByTestId('engine-selector-dialog')).toBeVisible();
  expect(screen.getByRole('alert')).toHaveTextContent('Refresh unavailable');
  await user.click(within(screen.getByRole('alert')).getByRole('button', { name: 'Retry' }));
  await waitFor(() => expect(screen.getByTestId('ready')).toHaveTextContent('true'));
  expect(load).toHaveBeenCalledTimes(3);
});

it('invalidates readiness on every comparison option, retaining only stale same-subject presentation', async () => {
  let finish!: (response: HttpSelectorChoicesResponse) => void;
  const load = vi.spyOn(selectorChoicesApi, 'getSelectorChoices')
    .mockImplementation(async (_pid, query) => mediaChoices(query, ['local']));
  const view = render(<Harness column={column} />);
  await waitFor(() => expect(screen.getByTestId('ready')).toHaveTextContent('true'));
  load.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
  view.rerender(<Harness column={{ ...column, options: { dpi: 300 } }} />);
  expect(screen.getByTestId('ready')).toHaveTextContent('false');
  expect(screen.getByRole('button', { name: /local/ })).toBeVisible();
  await waitFor(() => expect(load).toHaveBeenCalledTimes(2));
  const query = load.mock.calls[1][1];
  expect(query.subject).toMatchObject({ params: { engine: 'local', dpi: 300 } });
  await act(async () => { finish(mediaChoices(query, ['local'])); });
  await waitFor(() => expect(screen.getByTestId('ready')).toHaveTextContent('true'));
});

it('honors an orphaned current choice instead of a ready default and never crosses projects', async () => {
  const load = vi.spyOn(selectorChoicesApi, 'getSelectorChoices').mockImplementation(async (_pid, query) => {
    const response = mediaChoices(query, ['local']);
    response.current_choice_id = 'saved';
    response.orphaned_current = { ...response.groups[0].choices[0], choice_id: 'saved', label: 'Saved engine',
      authored_selection: { kind: 'engine', engine: 'saved' }, can_run: false, is_current: true };
    return response;
  });
  const view = render(<Harness column={{ ...column, engineId: 'saved' }} />);
  expect(await screen.findByRole('button', { name: /Saved engine/ })).toBeVisible();
  expect(screen.getByTestId('ready')).toHaveTextContent('false');
  load.mockImplementation(() => new Promise(() => undefined));
  view.rerender(<Harness projectId="project-b" column={column} />);
  expect(screen.queryByRole('button', { name: /Saved engine/ })).not.toBeInTheDocument();
  expect(screen.getByTestId('ready')).toHaveTextContent('false');
  await waitFor(() => expect(load).toHaveBeenCalledTimes(2));
  const signal = load.mock.calls[1][2]?.signal;
  view.unmount();
  expect(signal?.aborted).toBe(true);
});
