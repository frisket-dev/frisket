// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { GuideSampleLaunch } from '../../src/components/GuideSampleLaunch';
import { parsePathname, routePath } from '../../src/routes';

const api = vi.hoisted(() => ({ get: vi.fn(), launch: vi.fn() }));
vi.mock('../../src/api/guideSamples', () => ({ guideSamplesApi: api }));

beforeEach(() => {
  history.replaceState(null, '', '/learn/court-filings?revision=1');
  api.get.mockReset().mockResolvedValue({ id: 'court-filings', revision: 1, title: 'Court filings', description: 'Sample PDFs', file_count: 2, total_bytes: 10, files: [] });
  api.launch.mockReset();
});
afterEach(cleanup);

it('parses the launch route without changing its revision query', () => {
  expect(parsePathname('/learn/court-filings')).toEqual({ kind: 'learn', sampleId: 'court-filings' });
  expect(routePath({ kind: 'learn', sampleId: 'court-filings' })).toBe('/learn/court-filings');
});

it('only launches after Open and retries with the same operation', async () => {
  api.launch.mockRejectedValueOnce(new Error('Temporary failure')).mockResolvedValueOnce({ project_id: 'project-1', destination: { kind: 'pdf_import_draft', draft_id: 'draft-1', stage: 'check_text', state: 'pending', guide_id: 'court-filings-to-table', step_id: 'check-text' } });
  render(<GuideSampleLaunch sampleId="court-filings" />);
  const open = await screen.findByRole('button', { name: 'Open in Frisket' });
  expect(api.launch).not.toHaveBeenCalled();
  fireEvent.click(open);
  await screen.findByRole('alert');
  expect(api.launch).toHaveBeenCalledTimes(1);
  const operation = api.launch.mock.calls[0][2];
  fireEvent.click(screen.getByRole('button', { name: 'Open in Frisket' }));
  await waitFor(() => expect(location.pathname).toBe('/p/project-1'));
  expect(api.launch.mock.calls[1][2]).toBe(operation);
  expect(location.search).toBe('?guideDraft=draft-1');
});

it('opens a raw sheet normally', async () => {
  api.launch.mockResolvedValue({ project_id: 'p', destination: { kind: 'sheet', sheet_id: 12, guide_id: 'council-recordings-to-transcripts', step_id: 'transcribe' } });
  render(<GuideSampleLaunch sampleId="council-recordings" />);
  await screen.findByText('Sample PDFs');
  expect(api.launch).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'Open in Frisket' }));
  await waitFor(() => expect(location.pathname).toBe('/p/p/s/12'));
  expect(location.search).toBe('');
});

it('cancels without starting a project', async () => {
  render(<GuideSampleLaunch sampleId="court-filings" />);
  fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }));
  expect(location.pathname).toBe('/');
  expect(location.search).toBe('');
  expect(api.launch).not.toHaveBeenCalled();
});

it('retains the operation after a reload before retry', async () => {
  api.launch.mockRejectedValue(new Error('Temporary failure'));
  const first = render(<GuideSampleLaunch sampleId="court-filings" />);
  fireEvent.click(await screen.findByRole('button', { name: 'Open in Frisket' }));
  await screen.findByRole('alert');
  const operation = api.launch.mock.calls[0][2];
  first.unmount();
  render(<GuideSampleLaunch sampleId="court-filings" />);
  fireEvent.click(await screen.findByRole('button', { name: 'Open in Frisket' }));
  await waitFor(() => expect(api.launch).toHaveBeenCalledTimes(2));
  expect(api.launch.mock.calls[1][2]).toBe(operation);
});


it('does not navigate when a launch finishes after the user leaves', async () => {
  let finish!: (value: unknown) => void;
  api.launch.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
  const view = render(<GuideSampleLaunch sampleId="court-filings" />);
  fireEvent.click(await screen.findByRole('button', { name: 'Open in Frisket' }));
  view.unmount();
  history.pushState(null, '', '/settings/personal/profile');
  await act(async () => {
    finish({ project_id: 'project-1', destination: { kind: 'sheet', sheet_id: 1 } });
  });
  expect(location.pathname).toBe('/settings/personal/profile');
});

it('does not navigate if the location changes before the component unmounts', async () => {
  let finish!: (value: unknown) => void;
  api.launch.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
  render(<GuideSampleLaunch sampleId="court-filings" />);
  fireEvent.click(await screen.findByRole('button', { name: 'Open in Frisket' }));
  history.pushState(null, '', '/settings/personal/profile');
  await act(async () => {
    finish({ project_id: 'project-1', destination: { kind: 'sheet', sheet_id: 1 } });
  });
  expect(location.pathname).toBe('/settings/personal/profile');
});


it('offers an explicit fresh start after a conflict', async () => {
  const conflict = Object.assign(new Error('The sample import is no longer open.'), { status: 409 });
  api.launch.mockRejectedValueOnce(conflict).mockResolvedValueOnce({
    project_id: 'new-project', destination: { kind: 'sheet', sheet_id: 1 },
  });
  render(<GuideSampleLaunch sampleId="court-filings" />);
  fireEvent.click(await screen.findByRole('button', { name: 'Open in Frisket' }));
  await screen.findByRole('alert');
  const previous = api.launch.mock.calls[0][2];
  expect(new URLSearchParams(location.search).get('operation_id')).toBe(previous);
  fireEvent.click(screen.getByRole('button', { name: 'Start again' }));
  await waitFor(() => expect(location.pathname).toBe('/p/new-project/s/1'));
  expect(api.launch.mock.calls[1][2]).not.toBe(previous);
});
