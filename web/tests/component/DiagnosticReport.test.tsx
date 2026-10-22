// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { DiagnosticReportButton } from '../../src/workspace/DiagnosticReport';
import { StatusBar } from '../../src/components/StatusBar';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';
import { createProjectApi } from '../../src/api/real';

vi.mock('../../src/components/useSearchPreparationNotice', () => ({
  useSearchPreparationNotice: () => null,
}));

const { render } = createWorkspaceTestHarness({
  projectId: 'report-issue', api: { projectApi: createProjectApi('report-issue') },
});
afterEach(cleanup);

it('keeps the report control inside the footer beside the existing actions', () => {
  render(<StatusBar
    actions={<DiagnosticReportButton />}
    run={null} history={null} reviewCount={0} projectionStatus={null}
    onUndo={vi.fn()} onRedo={vi.fn()} onOpenReview={vi.fn()} onCancelRun={vi.fn()}
  />);
  const footer = within(screen.getByRole('contentinfo'));
  const trigger = footer.getByRole('button', { name: 'Report issue' });
  expect(trigger).toHaveClass('status-btn');
  expect(trigger).toHaveAttribute('aria-expanded', 'false');
  fireEvent.click(trigger);
  expect(trigger).toHaveAttribute('aria-expanded', 'true');
  expect(footer.getByTestId('diagnostic-popover')).toBeVisible();
  fireEvent.click(footer.getByRole('button', { name: 'Close report issue' }));
  expect(trigger).toHaveAttribute('aria-expanded', 'false');
  expect(footer.queryByTestId('diagnostic-popover')).not.toBeInTheDocument();
  expect(footer.getByTestId('review-queue-button')).toBeVisible();
});
