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

  it('keeps semantic media on the shared media renderer', () => {
    render(<ReviewValue field={field({
      columnName: 'frame', columnType: 'image', value: 'https://example.test/frame.jpg',
    })} />);

    expect(screen.getByRole('img', { name: 'frame' })).toHaveAttribute('src', 'https://example.test/frame.jpg');
  });
});
