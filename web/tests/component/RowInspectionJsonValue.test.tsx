// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { JsonValue } from '../../src/components/rowInspection/JsonValue';
import { createProjectApi } from '../../src/api/real';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const { render } = createWorkspaceTestHarness({
  projectId: 'row-json', api: { projectApi: createProjectApi('row-json') },
});
afterEach(cleanup);

describe('row inspection structured results', () => {
  it.each([
    {
      label: 'entities',
      item: { text: 'Ada', type: 'PERSON', start: 0, end: 3, score: 0.98, url: 'https://example.test/source' },
      tableId: 'entity-mini-table',
      itemId: 'review-entity-item',
      headers: ['text', 'type', 'url'],
    },
    {
      label: 'transcript segments',
      item: { segment_index: 0, start: 1, end: 2, text: 'Hello', speaker: 'Ada', url: 'https://example.test/source' },
      tableId: 'transcript-segment-mini-table',
      itemId: 'review-result-item',
      headers: ['start', 'end', 'text', 'speaker', 'url'],
    },
    {
      label: 'ordinary records',
      item: { title: 'Source', url: 'https://example.test/source' },
      tableId: 'json-mini-table',
      itemId: 'review-result-item',
      headers: ['title', 'url'],
    },
  ])('retains source selection and link interaction for $label', ({ item, tableId, itemId, headers }) => {
    const select = vi.fn();
    render(<JsonValue value={[item, { ...item, url: 'https://example.test/other' }]}
      onSelectItem={select} selectableItemIndices={new Set([0])} />);
    const table = screen.getByTestId(tableId);
    expect(within(table).getAllByRole('columnheader').map((header) => header.textContent)).toEqual(headers);
    const [selectable, ordinary] = within(table).getAllByTestId(itemId);
    expect(selectable).toHaveAttribute('tabindex', '0');
    expect(ordinary).not.toHaveAttribute('tabindex');
    fireEvent.click(within(selectable).getByRole('link'));
    fireEvent.click(ordinary);
    expect(select).not.toHaveBeenCalled();
    fireEvent.keyDown(selectable, { key: 'Enter' });
    fireEvent.keyDown(selectable, { key: ' ' });
    fireEvent.click(selectable);
    expect(select.mock.calls).toEqual([[0], [0], [0]]);
  });

  it('keeps all content columns when entity semantics take precedence over transcript shape', () => {
    render(<JsonValue semanticType="entity_mentions" value={[{
      text: 'Ada', type: 'PERSON', segment_index: 0, start: 0, end: 3, score: 1,
      a: 'A', b: 'B', c: 'C', d: 'D', e: 'E',
    }]} />);
    const table = screen.getByTestId('entity-mini-table');
    expect(within(table).getAllByRole('columnheader').map((header) => header.textContent)).toEqual([
      'text', 'type', 'segment_index', 'a', 'b', 'c', 'd', 'e',
    ]);
    expect(screen.queryByTestId('transcript-segment-mini-table')).not.toBeInTheDocument();
  });

  it('keeps nested records readable and source-selectable outside compact tables', () => {
    const select = vi.fn();
    render(<JsonValue value={[{ title: 'Source', metadata: { page: 4, url: 'https://example.test/source' } }]}
      onSelectItem={select} selectableItemIndices={new Set([0])} />);
    expect(screen.queryByTestId('json-mini-table')).not.toBeInTheDocument();
    const item = screen.getByTestId('review-result-item');
    expect(item).toHaveTextContent('page4');
    fireEvent.keyDown(within(item).getByRole('link'), { key: 'Enter' });
    expect(select).not.toHaveBeenCalled();
    fireEvent.keyDown(item, { key: 'Enter' });
    expect(select).toHaveBeenCalledWith(0);
  });
});
