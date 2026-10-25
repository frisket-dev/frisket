// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { useState } from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { within } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import { ListTableParamsBody } from '../../src/components/action-panel/ListTableParamsBody';
import type { GeneratedActionParams } from '../../src/generated/actionTypes';
import { sheetMeta } from '../support/actionFormFixtures';
import { columnDef } from '../support/domainFixtures';

type ListParams = GeneratedActionParams['derive.table_from_list'];
const sheet = sheetMeta([
  columnDef({ id: '43', name: 'items', type: 'json' }),
  columnDef({ id: '44', name: 'articles', type: 'json' }),
  columnDef({ id: '45', name: 'case title', type: 'text' }),
], { id: '7', name: 'Current', rowCount: 3 });
const saved: ListParams = {
  source: { kind: 'column', sheet_id: 7, column_id: 43, include_columns: ['name'] },
};

afterEach(cleanup);

function Harness() {
  const [params, setParams] = useState<ListParams>(saved);
  return <>
    <ListTableParamsBody sheet={sheet} params={params} setParams={setParams}
      errors={{}} Field={() => null} />
    <output data-testid="canonical-params">{JSON.stringify(params)}</output>
    <button onClick={() => setParams(saved)}>Reset saved parameters</button>
    <button onClick={() => setParams({ source: {
      kind: 'column', sheet_id: 7, column_id: 44, include_columns: ['title', 'url'],
    } })}>Load another source</button>
  </>;
}

function canonicalParams(): ListParams {
  return JSON.parse(screen.getByTestId('canonical-params').textContent!);
}

it('preserves separators while typing and publishes normalized included properties', () => {
  render(<Harness />);
  const input = screen.getByLabelText('Include properties');
  expect(input).toHaveValue('name');
  fireEvent.change(input, { target: { value: 'name,' } });
  expect(input).toHaveValue('name,');
  fireEvent.change(input, { target: { value: 'name, ' } });
  expect(input).toHaveValue('name, ');
  fireEvent.change(input, { target: { value: 'name, role ' } });
  expect(input).toHaveValue('name, role ');
  expect(canonicalParams().source).toEqual({
    kind: 'column', sheet_id: 7, column_id: 43, include_columns: ['name', 'role'],
  });
  fireEvent.change(input, { target: { value: ' ' } });
  expect(input).toHaveValue(' ');
  expect(canonicalParams().source).toHaveProperty('include_columns', null);
});

it('hydrates saved properties and replaces raw text when the parent resets the source', () => {
  render(<Harness />);
  const input = screen.getByLabelText('Include properties');
  expect(input).toHaveValue('name');
  fireEvent.change(input, { target: { value: 'name,' } });
  fireEvent.click(screen.getByText('Reset saved parameters'));
  expect(input).toHaveValue('name');
  fireEvent.click(screen.getByText('Load another source'));
  expect(input).toHaveValue('title, url');
  expect(screen.getByLabelText('List')).toHaveValue('44');
});

it('stores selected parent columns as stable ids with explicit aliases', async () => {
  const user = userEvent.setup();
  render(<Harness />);
  await user.click(screen.getByTestId('field-list_parent_columns'));
  const menu = within(screen.getByTestId('field-list_parent_columns-menu'));
  await user.click(menu.getByRole('option', { name: /case title/, hidden: true }));
  expect(canonicalParams().parent_columns).toEqual([
    { column_id: 45, output_name: 'case title' },
  ]);
  expect(menu.queryByRole('option', { name: /^items/, hidden: true })).not.toBeInTheDocument();
});
