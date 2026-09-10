// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { PdfTablesPickerProps } from '../../src/components/PdfTablesPicker';
import { PdfTablesReview } from '../../src/components/action-panel/PdfTablesReview';

const projectApi = vi.hoisted(() => ({ getColumnRuns: vi.fn() }));
const { getColumnRuns } = projectApi;
vi.mock('../../src/bind/useWorkspaceStores', () => ({
  useWorkspaceStores: () => ({ projectApi }),
}));
beforeEach(() => {
  getColumnRuns.mockReset().mockResolvedValue({
    currentRun: { actionKind: 'media.extract_pdf_tables' }, latestRun: null,
  });
});

vi.mock('../../src/components/PdfTablesPicker', () => ({
  PdfTablesPicker: ({
    column,
    defaultTargetName,
    onMaterialize,
    onExportTables,
    onClose,
  }: PdfTablesPickerProps) => (
    <div
      data-testid="pdf-tables-picker"
      data-column={`${column.id}:${column.name}`}
      data-default-target={defaultTargetName}
    >
      <button
        type="button"
        data-testid="picker-materialize"
        onClick={() => onMaterialize({
          columnName: column.name,
          includeColumns: ['vendor', 'amount'],
          targetName: 'Bid tables',
        })}
      >
        Materialize
      </button>
      {onExportTables && (
        <button
          type="button"
          data-testid="picker-export"
          onClick={() => onExportTables({
            columnName: column.name,
            groupBy: 'table_index',
            excludeColumns: ['source_row_id'],
          })}
        >
          Export
        </button>
      )}
      <button type="button" data-testid="picker-close" onClick={onClose}>Close</button>
    </div>
  ),
}));

afterEach(cleanup);

const SHEET = {
  id: '7',
  columns: [
    { id: '1', name: 'source_pdf', type: 'file' as const },
    { id: '2', name: 'first_tables', type: 'json' as const },
    { id: '3', name: 'preferred_tables', type: 'json' as const },
  ],
};

describe('PdfTablesReview', () => {
  it('shows only for the exact PDF output with host-reported extraction provenance', async () => {
    const props = {
      sheet: SHEET,
      outputName: 'preferred_tables',
      onMaterialize: vi.fn(),
    };
    const { rerender } = render(<PdfTablesReview actionKind="ocr" {...props} />);
    expect(screen.queryByTestId('pdf-tables-review-banner')).not.toBeInTheDocument();

    rerender(
      <PdfTablesReview
        actionKind="media.extract_pdf_tables"
        {...props}
        sheet={{ id: '7', columns: [SHEET.columns[0]] }}
      />,
    );
    expect(screen.queryByTestId('pdf-tables-review-banner')).not.toBeInTheDocument();

    expect(getColumnRuns).not.toHaveBeenCalled();
    rerender(<PdfTablesReview actionKind="media.extract_pdf_tables" {...props} />);
    expect(await screen.findByTestId('pdf-tables-review-banner')).toBeInTheDocument();
    expect(getColumnRuns).toHaveBeenCalledWith('3', 0, 1);
  });

  it('opens the exact result column and closes after materializing', async () => {
    const onMaterialize = vi.fn();
    render(
      <PdfTablesReview
        actionKind="media.extract_pdf_tables"
        sheet={SHEET}
        outputName=" preferred_tables "
        onMaterialize={onMaterialize}
      />,
    );

    fireEvent.click(await screen.findByTestId('review-tables'));
    const picker = screen.getByTestId('pdf-tables-picker');
    expect(picker).toHaveAttribute('data-column', '3:preferred_tables');
    expect(picker).toHaveAttribute('data-default-target', 'preferred_tables');

    fireEvent.click(screen.getByTestId('picker-materialize'));
    expect(onMaterialize).toHaveBeenCalledWith({
      columnId: '3',
      columnName: 'preferred_tables',
      includeColumns: ['vendor', 'amount'],
      targetName: 'Bid tables',
    });
    expect(screen.queryByTestId('pdf-tables-picker')).not.toBeInTheDocument();
  });

  it('adds the selected column identity to an export intent and closes', async () => {
    const onExport = vi.fn();
    render(
      <PdfTablesReview
        actionKind="media.extract_pdf_tables"
        sheet={SHEET}
        outputName="first_tables"
        onMaterialize={vi.fn()}
        onExport={onExport}
      />,
    );

    fireEvent.click(await screen.findByTestId('review-tables'));
    expect(screen.getByTestId('pdf-tables-picker')).toHaveAttribute(
      'data-column',
      '2:first_tables',
    );
    fireEvent.click(screen.getByTestId('picker-export'));

    expect(onExport).toHaveBeenCalledWith({
      column: { id: '2', name: 'first_tables' },
      groupBy: 'table_index',
      excludeColumns: ['source_row_id'],
    });
    expect(screen.queryByTestId('pdf-tables-picker')).not.toBeInTheDocument();
  });

  it('does not expose export when no export operation is available', async () => {
    render(
      <PdfTablesReview
        actionKind="media.extract_pdf_tables"
        sheet={SHEET}
        outputName="first_tables"
        onMaterialize={vi.fn()}
      />,
    );

    fireEvent.click(await screen.findByTestId('review-tables'));
    expect(screen.queryByTestId('picker-export')).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId('picker-close'));
    expect(screen.queryByTestId('pdf-tables-picker')).not.toBeInTheDocument();
  });

  it('does not claim extraction for a missing output or unrelated current producer', async () => {
    getColumnRuns.mockResolvedValue({ currentRun: { actionKind: 'map.python' },
      latestRun: { actionKind: 'media.extract_pdf_tables' } });
    const props = { actionKind: 'media.extract_pdf_tables', sheet: SHEET, onMaterialize: vi.fn() };
    const { rerender } = render(<PdfTablesReview {...props} outputName="missing" />);
    expect(getColumnRuns).not.toHaveBeenCalled();
    expect(screen.queryByTestId('review-tables')).not.toBeInTheDocument();
    rerender(<PdfTablesReview {...props} outputName="first_tables" />);
    await waitFor(() => expect(getColumnRuns).toHaveBeenCalledWith('2', 0, 1));
    expect(screen.queryByTestId('review-tables')).not.toBeInTheDocument();
  });

  it('keeps mixed-origin extraction review advisory, without claiming completion or feedability', async () => {
    getColumnRuns.mockResolvedValue({ currentRun: null, mixedOrigins: true,
      latestRun: { actionKind: 'media.extract_pdf_tables' } });
    render(<PdfTablesReview actionKind="media.extract_pdf_tables" sheet={SHEET}
      outputName="first_tables" onMaterialize={vi.fn()} />);
    expect(await screen.findByTestId('review-tables')).toBeInTheDocument();
    expect(screen.getByTestId('pdf-tables-review-banner')).toHaveTextContent('Current values may include edits or multiple runs.');
    expect(screen.getByTestId('pdf-tables-review-banner')).not.toHaveTextContent('Extraction complete');
  });
});
