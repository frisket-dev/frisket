// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { PdfImportSnapshot } from '../../src/api/pdfImports';
import { PdfBatchDrafts } from '../../src/components/importWorkspace/pdfBatch';

function draft(state: PdfImportSnapshot['state'] = 'open'): PdfImportSnapshot {
  return {
    draft_id: `draft-${state}`,
    state,
    source_label: 'Research folder',
    paused: true,
    upload: { files_done: 12, files_total: 40, bytes_done: 120, bytes_total: 400 },
    cleanup: { bytes_done: 0, bytes_total: 120 },
  } as PdfImportSnapshot;
}

describe('PdfBatchDrafts', () => {
  it('offers bounded active drafts for resume and confirms discard', async () => {
    const open = draft();
    const onResume = vi.fn();
    const api = {
      list: vi.fn(async () => [open, draft('committed')]),
      discard: vi.fn(async () => ({ ...open, state: 'cancelled' as const })),
    };

    render(<PdfBatchDrafts projectId="project-1" api={api} onResume={onResume} />);

    expect(await screen.findByText('Research folder')).toBeInTheDocument();
    expect(screen.getAllByText('Research folder')).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }));
    expect(onResume).toHaveBeenCalledWith('draft-open');

    fireEvent.click(screen.getByRole('button', { name: 'Discard Research folder' }));
    expect(screen.getByText('Discard this import?')).toBeInTheDocument();
    expect(api.discard).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Discard draft' }));

    await waitFor(() => expect(api.discard).toHaveBeenCalledWith('project-1', 'draft-open'));
    await waitFor(() => expect(screen.queryByText('Research folder')).not.toBeInTheDocument());
  });
});
