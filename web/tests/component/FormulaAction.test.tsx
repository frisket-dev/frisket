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
    const trigger = screen.getByRole('button', { name: 'Functions & examples' });
    expect(trigger).toHaveAttribute('data-button-variant', 'link');
    expect(trigger.querySelector('svg')).toHaveAttribute('aria-hidden', 'true');
    await user.click(trigger);
    const guide = screen.getByRole('region', { name: 'Common examples' });
    expect(guide).toBeVisible();
    expect(within(guide).queryByRole('textbox')).not.toBeInTheDocument();
    for (const [title, reference, linkLabel] of [
      ['Text', 'text', 'More text functions'], ['Numbers', 'math', 'More math functions'],
      ['Dates', 'datetime', 'More date and time functions'], ['Conditions', 'logical', 'More logical functions'],
    ]) {
      const section = within(guide).getByRole('region', { name: title });
      const table = within(section).getByRole('table', { name: `${title} formula examples` });
      const exampleCount = within(table).getAllByRole('row').length - 1;
      expect(exampleCount).toBeGreaterThanOrEqual(5);
      expect(exampleCount).toBeLessThanOrEqual(20);
      expect(within(table).getByRole('columnheader', { name: 'Formula' })).toBeVisible();
      expect(within(table).getByRole('columnheader', { name: 'What it does' })).toBeVisible();
      expect(within(section).getByRole('link', { name: `${linkLabel} ↗` })).toHaveAttribute(
        'href', `https://www.formualizer.dev/docs/reference/functions/${reference}`);
    }
    const numbers = within(guide).getByRole('table', { name: 'Numbers formula examples' });
    expect(within(numbers).queryByText(/^=(SUM|AVERAGE|MIN|MAX)\(/)).not.toBeInTheDocument();
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
});
