// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../src/media/pdfjsSetup', () => ({ pdfjsLib: { getDocument: vi.fn() } }));

import type { PreviewSampleResult } from '../../src/api/types';
import { createProjectApi } from '../../src/api/real';
import { OcrCompareTab } from '../../src/workbench/OcrCompareTab';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const api = createProjectApi('test-project');
const { render } = createWorkspaceTestHarness({ projectId: 'test-project', api: { projectApi: api } });

function completed(previewId: string): PreviewSampleResult {
  return { previewId, status: 'done', progress: { done: 1, total: 1 }, columns: [], sampled: 1,
    total: 1, error: null, kind: 'table', warnings: [], rows: [{
      page: { value: 1 }, text: { value: `text from ${previewId}` },
      blocks: { value: JSON.stringify([{ text: 'block', score: 0.9 }]) },
    }] };
}

beforeEach(() => {
  vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:ocr-upload');
  vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
  vi.spyOn(api, 'listActionCatalog').mockResolvedValue({ actions: [{ kind: 'media.ocr', ui_hints: {
    engines: [
      { id: 'rapidocr', label: 'RapidOCR', tier: 'local', available: true },
      { id: 'dots.mocr', label: 'dots.mocr', tier: 'sidecar', available: true },
    ],
  } }] } as never);
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe('OcrCompareTab upload comparison', () => {
  it('accepts an arbitrary image without a project target and reads standard preview tables', async () => {
    const estimates = vi.spyOn(api, 'estimateOcrScratch').mockResolvedValue({ cost: 0.125, rows: 1,
      billed_cost: 125_000, policy_id: 'test-policy', requires_confirmation: false });
    const starts = vi.spyOn(api, 'compareOcrScratch')
      .mockResolvedValueOnce({ previewId: 'rapid', total: 1 })
      .mockResolvedValueOnce({ previewId: 'dots', total: 1 });
    vi.spyOn(api, 'getPreview').mockImplementation(async (previewId) => completed(previewId));
    const user = userEvent.setup();
    render(<OcrCompareTab onSessionChange={() => undefined} />);

    await user.upload(screen.getByTestId('ocr-compare-file-input'),
      new File(['pixels'], 'receipt.png', { type: 'image/png' }));
    expect(screen.getByText('receipt.png')).toBeVisible();
    await waitFor(() => expect(screen.getByTestId('ocr-compare-run')).toBeEnabled());
    await user.click(screen.getByTestId('ocr-compare-run'));

    await waitFor(() => expect(starts).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId('media-compare-cost-estimate')).toHaveTextContent('$0.25');
    expect(estimates.mock.calls.map(([, input]) => input)).toEqual([
      expect.objectContaining({ engine: 'rapidocr', pages: [1], dpi: 200 }),
      expect.objectContaining({ engine: 'dots.mocr', pages: [1], dpi: 200 }),
    ]);
    await waitFor(() => {
      const text = screen.getAllByTestId('ocr-compare-engine-column')
        .map((column) => column.textContent).join(' ');
      expect(text).toContain('rapid');
      expect(text).toContain('dots');
    });
  });

  it('keeps page, language, DPI, and searchable-PDF options on each quoted variant', async () => {
    const estimates = vi.spyOn(api, 'estimateOcrScratch').mockResolvedValue({ cost: null, rows: 1,
      requires_confirmation: false });
    vi.spyOn(api, 'compareOcrScratch').mockResolvedValue({ previewId: 'configured', total: 1 });
    vi.spyOn(api, 'getPreview').mockResolvedValue(completed('configured'));
    const user = userEvent.setup();
    render(<OcrCompareTab onSessionChange={() => undefined} />);
    await user.upload(screen.getByTestId('ocr-compare-file-input'),
      new File(['pdf'], 'sample.pdf', { type: 'application/pdf' }));
    await waitFor(() => expect(screen.getByTestId('ocr-compare-run')).toBeEnabled());

    await user.click(screen.getAllByTestId('ocr-compare-variant-gear')[0]);
    expect(screen.getByTestId('ocr-compare-dpi-stepper')).toHaveAttribute('min', '50');
    await user.click(screen.getByTestId('ocr-compare-dpi-pill-300'));
    await user.selectOptions(screen.getByTestId('ocr-compare-configure-language'), 'eng');
    const pages = screen.getByTestId('ocr-compare-configure-pages');
    await user.clear(pages);
    await user.type(pages, '1, 2');
    await user.click(screen.getByTestId('ocr-compare-configure-searchable-pdf'));
    await user.keyboard('{Escape}');
    await user.click(screen.getByTestId('ocr-compare-run'));

    await waitFor(() => expect(estimates).toHaveBeenCalledTimes(2));
    expect(estimates.mock.calls[0][1]).toMatchObject({ engine: 'rapidocr', dpi: 300,
      language: 'eng', pages: [1, 2], searchable_pdf: true });
  });

  it('rejects non-positive and more than ten page numbers without stale fallback input', async () => {
    const estimates = vi.spyOn(api, 'estimateOcrScratch').mockResolvedValue({ cost: null, rows: 1,
      requires_confirmation: false });
    vi.spyOn(api, 'compareOcrScratch').mockResolvedValue({ previewId: 'other', total: 1 });
    vi.spyOn(api, 'getPreview').mockResolvedValue(completed('other'));
    const user = userEvent.setup();
    render(<OcrCompareTab onSessionChange={() => undefined} />);
    await user.upload(screen.getByTestId('ocr-compare-file-input'),
      new File(['pdf'], 'sample.pdf', { type: 'application/pdf' }));
    await waitFor(() => expect(screen.getByTestId('ocr-compare-run')).toBeEnabled());
    await user.click(screen.getAllByTestId('ocr-compare-variant-gear')[0]);
    const pages = screen.getByTestId('ocr-compare-configure-pages');

    await user.clear(pages);
    await user.type(pages, '0');
    expect(screen.getByRole('alert')).toHaveTextContent('positive page numbers');
    await user.clear(pages);
    await user.type(pages, '3abc');
    expect(screen.getByRole('alert')).toHaveTextContent('positive page numbers');
    await user.clear(pages);
    await user.type(pages, '1,2,3,4,5,6,7,8,9,10,11');
    expect(screen.getByRole('alert')).toHaveTextContent('at most 10 pages');
    await user.keyboard('{Escape}');
    await user.click(screen.getByTestId('ocr-compare-run'));

    await waitFor(() => expect(estimates).toHaveBeenCalled());
    expect(estimates.mock.calls.map(([, input]) => input.engine)).toEqual(['dots.mocr']);
  });
});
