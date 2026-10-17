// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

const useSearchPreparationNotice = vi.hoisted(() => vi.fn());

vi.mock('../../src/components/useSearchPreparationNotice', () => ({
  useSearchPreparationNotice,
}));

import { StatusBar } from '../../src/components/StatusBar';
import { createProjectApi } from '../../src/api/real';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const { render } = createWorkspaceTestHarness({
  projectId: 'status-search-activity',
  api: { projectApi: createProjectApi('status-search-activity') },
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('StatusBar search activity', () => {
  it('uses the bottom-left activity control to dismiss and reopen queued indexing without calling it idle', () => {
    useSearchPreparationNotice.mockReturnValue({
      activityKey: 'index:41',
      stage: 'index',
      tone: 'active',
      title: 'Preparing search',
      detail: 'Search indexing queued',
      progress: null,
      details: [],
    });
    render(
      <StatusBar
        run={null}
        history={null}
        reviewCount={0}
        projectionStatus={null}
        onUndo={vi.fn()}
        onRedo={vi.fn()}
        onOpenReview={vi.fn()}
        onCancelRun={vi.fn()}
      />,
    );

    expect(screen.queryByText('idle')).not.toBeInTheDocument();
    const activity = screen.getByTestId('search-activity-toggle');
    expect(activity).toHaveTextContent('Preparing search · Search indexing queued');
    expect(activity).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByTestId('search-preparation-banner')).toBeVisible();

    fireEvent.click(screen.getByTestId('search-preparation-dismiss'));
    expect(screen.queryByTestId('search-preparation-banner')).not.toBeInTheDocument();
    expect(activity).toHaveAttribute('aria-expanded', 'false');

    fireEvent.click(activity);
    expect(screen.getByTestId('search-preparation-banner')).toBeVisible();
    expect(activity).toHaveAttribute('aria-expanded', 'true');
  });

});
