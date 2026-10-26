// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll, describe, expect, it } from 'vitest';
import { FunctionSquare } from 'lucide-react';
import { ACTION_ICON_BY_KIND } from '../../src/workbench/actSurface';
import { ACTION_PLACEMENTS } from '../../src/actions/model';
import { columnDef } from '../support/domainFixtures';
import { sheetMeta } from '../support/actionFormFixtures';
import { installPopoverPolyfill } from '../support/domPolyfills';
import { mountTypedForm, servedTypedEntry } from './typedFormCutoverF2';

beforeAll(installPopoverPolyfill);
afterEach(cleanup);

function mountFormula() {
  return mountTypedForm({
    entry: servedTypedEntry('map.formula'),
    sheet: sheetMeta([
      columnDef({ id: '1', name: 'amount', type: 'number' }),
      columnDef({ id: '2', name: 'exchange rate', type: 'number' }),
      columnDef({ id: '3', name: 'payload', type: 'json' }),
    ], { id: '7' }),
    resolveParams: async () => ({ diagnostics: {}, logical_outputs: [{ key: 'result', column_type: 'number' }] }),
  });
}

describe('Formula action', () => {
  it('appears beside Template with a dedicated formula icon', () => {
    expect(ACTION_PLACEMENTS['map.formula']).toEqual({ tab: 'transform', group: 'TRANSFORM', order: 3 });
    expect(ACTION_PLACEMENTS['map.template']?.order).toBe(2);
    expect(ACTION_ICON_BY_KIND['map.formula']).toBe(FunctionSquare);
    expect(Object.entries(ACTION_ICON_BY_KIND).filter(([, icon]) => icon === FunctionSquare)).toHaveLength(1);
  });

  it.each(['=ROUND({{amount}},2)', 'ROUND({{amount}},2)'])('sends the normal run request for %s', async (formula) => {
    const user = userEvent.setup();
    const form = mountFormula();
    const input = screen.getByRole('textbox', { name: 'Formula', exact: true });
    fireEvent.change(input, { target: { value: formula } });
    expect(screen.queryByRole('combobox', { name: /output type/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Column', exact: true })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId('generated-action-run')).toBeEnabled());
    await user.click(screen.getByTestId('generated-action-run'));
    expect(form.lastRequest()).toMatchObject({
      action_id: 'map.formula', params: { formula: { text: formula } },
      scope: { kind: 'sheet_rows', sheet_id: 7 },
      output_names: { result: 'result' },
    });
    expect(form.onExecute.mock.lastCall?.[1]).toBe('run');
  });

  it('inserts a named column at the cursor without changing formula spacing', async () => {
    const user = userEvent.setup();
    mountFormula();
    const input = screen.getByRole('textbox', { name: 'Formula', exact: true }) as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: '=ROUND(,2)' } });
    input.focus();
    input.setSelectionRange(7, 7);
    await user.click(screen.getByRole('button', { name: 'amount', exact: true }));
    expect(input).toHaveValue('=ROUND({{amount}},2)');
    const insert = screen.getByRole('combobox', { name: 'Insert column in formula' });
    expect(insert).toBeVisible();
    expect(insert).not.toHaveTextContent('payload');
  });
});

describe('Formula token highlighting', () => {
  it('keeps quoted token text literal, including doubled quotes', () => {
    const { container } = mountFormula();
    fireEvent.change(screen.getByRole('textbox', { name: 'Formula', exact: true }), {
      target: { value: '=IF(TRUE,"say ""{{amount}}""",{{amount}})' },
    });
    expect(container.querySelectorAll('.template-token.known')).toHaveLength(1);
  });
});

describe('Formula function guide', () => {
  it('stays open while typing and closes without changing the formula', async () => {
    const user = userEvent.setup();
    mountFormula();
    const input = screen.getByRole('textbox', { name: 'Formula', exact: true });
    await user.click(screen.getByRole('button', { name: 'Functions & examples' }));
    const guide = screen.getByRole('region', { name: 'Common examples' });
    expect(guide).toBeVisible();
    expect(within(guide).getByText('ROUND(number, digits)')).toBeVisible();
    expect(within(guide).getByRole('link', { name: /Full Formualizer/ })).toHaveAttribute(
      'href', 'https://www.formualizer.dev/docs/reference/functions');
    expect(screen.queryByRole('dialog', { name: 'Functions & examples' })).not.toBeInTheDocument();
    await user.click(input);
    await user.type(input, '=ROUND(');
    expect(input).toHaveValue('=ROUND(');
    expect(input).toHaveFocus();
    expect(guide).toBeVisible();
    await user.click(within(guide).getByRole('button', { name: 'Close common examples' }));
    expect(screen.queryByRole('region', { name: 'Common examples' })).not.toBeInTheDocument();
    expect(input).toHaveValue('=ROUND(');
    expect(screen.getByRole('button', { name: 'Functions & examples' })).toHaveFocus();
  });

  it('filters the small example guide without replacing the editor text', async () => {
    const user = userEvent.setup();
    const form = mountFormula();
    const input = screen.getByRole('textbox', { name: 'Formula', exact: true });
    fireEvent.change(input, { target: { value: '=1+2' } });
    await user.click(screen.getByRole('button', { name: 'Functions & examples' }));
    const guide = screen.getByRole('region', { name: 'Common examples' });
    await user.type(within(guide).getByRole('textbox', { name: 'Filter formula examples' }), 'TRIM{Enter}');
    expect(form.onExecute).not.toHaveBeenCalled();
    expect(within(guide).getByText('TRIM(text)')).toBeVisible();
    expect(within(guide).queryByText('ROUND(number, digits)')).not.toBeInTheDocument();
    expect(input).toHaveValue('=1+2');
  });
});
