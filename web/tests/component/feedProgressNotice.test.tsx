// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createStore } from '../../src/core/store/createStore';
import { FeedProgressNotice } from '../../src/components/FeedProgressNotice';

const holder = vi.hoisted(() => ({ store: null as unknown }));
vi.mock('../../src/bind/useWorkspaceStores', () => ({
  useWorkspaceStores: () => ({ job: { store: holder.store }, chromePreferences: { projectId: 'project' } }),
}));
function jobs(status: string, summary: Record<string, unknown> = {}) {
  return { actionJobs: { jobs: [{ kind: 'source.poll', jobId: 7, status, resultSummary: summary, error: 'Provider unavailable' }] } };
}
beforeEach(() => { holder.store = createStore(jobs('queued')); });
afterEach(cleanup);

it('shows real discovery counts then the terminal import result', () => {
  const store = holder.store as ReturnType<typeof createStore<ReturnType<typeof jobs>>>;
  render(<FeedProgressNotice />);
  expect(screen.getByText('Waiting to start')).toBeInTheDocument();
  act(() => store.set(jobs('running', { completed_items: 8, total_items: 20 })));
  expect(screen.getByText('8 of 20 items found')).toBeInTheDocument();
  expect(screen.getByRole('progressbar')).toHaveAttribute('value', '8');
  expect(screen.getByRole('progressbar')).toHaveAttribute('max', '20');
  act(() => store.set(jobs('failed')));
  expect(screen.getByRole('alert')).toHaveTextContent('Provider unavailable');
});

it('does not reannounce historical completed imports on opening a project', () => {
  holder.store = createStore(jobs('done'));
  render(<FeedProgressNotice />);
  expect(screen.queryByTestId('feed-progress-notice')).not.toBeInTheDocument();
});
