// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { CellValue, ReviewBundleField } from '../../src/api/types';
import { createProjectApi } from '../../src/api/real';
import { ReviewValue } from '../../src/components/review/ReviewValue';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const projectApi = createProjectApi('review-value');
const { render } = createWorkspaceTestHarness({
  projectId: 'review-value',
  api: { projectApi },
});

afterEach(cleanup);

function field(update: Partial<ReviewBundleField> = {}): ReviewBundleField {
  return {
    id: '9:3:4', runId: '9', sheetId: '2', rowId: '3', columnId: '4',
    columnName: 'result', columnType: 'json', value: null, confidence: null,
    justification: '', reviewDecision: null, reviewState: 'unreviewed',
    role: 'field', chore: true, ...update,
  };
}

describe('ReviewValue', () => {
  it('uses the shared entity presentation instead of exposing model plumbing', () => {
    const value = JSON.stringify([
      { text: 'Ada Lovelace', type: 'PERSON', start: 0, end: 12, score: 0.98, fingerprint: 'person:ada' },
      { text: 'London', type: 'GPE', start: 20, end: 26, score: 0.91, fingerprint: 'gpe:london' },
    ]);
    render(<ReviewValue field={field({ columnName: 'entities', value })} />);

    const table = screen.getByTestId('entity-mini-table');
    expect([...table.querySelectorAll('th')].map((cell) => cell.textContent)).toEqual(['text', 'type']);
    expect(table).toHaveTextContent('Ada Lovelace');
    expect(table).not.toHaveTextContent('fingerprint');
    expect(table).not.toHaveTextContent('0.98');
  });

  it('renders objects and mixed nested lists as labeled content rather than raw JSON', () => {
    const value = {
      query: 'climate pledge',
      occurrences: [
        { excerpt: 'We will halve emissions.', page: 4 },
        'Mentioned again in the appendix',
        true,
      ],
      detection: { label: 'logo', box: [12, 18, 40, 28] },
    } as unknown as CellValue;
    const { container } = render(<ReviewValue field={field({ columnName: 'matches', value })} />);

    const object = screen.getByTestId('json-object');
    expect(object).toHaveTextContent('query');
    expect(object).toHaveTextContent('climate pledge');
    expect(object).toHaveTextContent('occurrences');
    expect(object).toHaveTextContent('We will halve emissions.');
    expect(object).toHaveTextContent('Mentioned again in the appendix');
    expect(object).toHaveTextContent('detection');
    expect(object).toHaveTextContent('12');
    expect(container.querySelector('pre.row-field-json')).toBeNull();
  });

  it('does not silently drop fields after the sixth key of a result object', () => {
    const value = JSON.stringify([{ one: 1, two: 2, three: 3, four: 4, five: 5, six: 6, seventh: 'Still visible' }]);
    render(<ReviewValue field={field({ value })} />);
    expect(screen.getByText('seventh')).toBeInTheDocument();
    expect(screen.getByText('Still visible')).toBeInTheDocument();
  });

  it('only activates top-level result items whose grounded indices were supplied', () => {
    const select = vi.fn();
    const value = JSON.stringify([
      { text: 'Ada Lovelace', type: 'PERSON', start: 0 },
      { text: 'London', type: 'GPE', start: 20 },
    ]);
    render(<ReviewValue field={field({ columnName: 'entities', value })}
      onSelectItem={select} selectableItemIndices={new Set([1])} />);

    const rows = within(screen.getByTestId('entity-mini-table')).getAllByRole('row').slice(1);
    expect(rows[0]).not.toHaveAttribute('data-review-result-selectable');
    expect(rows[1]).toHaveAttribute('data-review-result-selectable', 'true');
    fireEvent.click(rows[0]);
    fireEvent.click(rows[1]);
    expect(select).toHaveBeenCalledOnce();
    expect(select).toHaveBeenCalledWith(1);
  });

  it('renders frame images and face crops without hiding face coordinates', () => {
    const image = { blob: 'a'.repeat(64), mime: 'image/png', filename: 'crop.png' };
    const { rerender } = render(<ReviewValue field={field({ value: JSON.stringify([{ t: 2, image }]) })} />);
    expect(screen.getByTestId('json-blob-thumb')).toHaveAttribute('src', expect.stringContaining(image.blob));
    rerender(<ReviewValue field={field({ value: JSON.stringify([{ x: 12, y: 18, w: 40, h: 28, face: image }]) })} />);
    expect(screen.getByTestId('json-blob-thumb')).toHaveAttribute('src', expect.stringContaining(image.blob));
    expect(screen.getByText('12')).toBeInTheDocument();
    expect(screen.getByText('40')).toBeInTheDocument();
  });

  it('keeps semantic media on the shared media renderer', () => {
    render(<ReviewValue field={field({
      columnName: 'frame', columnType: 'image', value: 'https://example.test/frame.jpg',
    })} />);

    expect(screen.getByRole('img', { name: 'frame' })).toHaveAttribute('src', 'https://example.test/frame.jpg');
  });
});
