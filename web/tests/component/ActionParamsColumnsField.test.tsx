// @vitest-environment jsdom
//
// The `columns` param input (Reduce's "Columns to feed each group",
// Agent's input columns — both routed through this SAME generic
// ParamInput) used to fall through into the default `.param-row` branch
// meant for compact single-line widgets: label in a 38%-wide left track,
// MultiColumnPicker's chip flow squeezed into the remaining ~62% track.
// Inside the action drawer's fixed 400px width that's roughly 235px for the
// chip box, so a sheet with 40+ columns wrapped near one chip per line and
// the whole panel scrolled to show them. The fix gives `columns` its own
// stacked early return (label on its own full-width line, control below),
// matching how DeriveJoinForm's/the semantic-join form's own column pickers
// were already hand-written outside `.param-row`.

import '@testing-library/jest-dom/vitest';
import { cleanup, screen, within, render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import { ActionParams } from '../../src/components/action-panel/ActionParams';
import type { ActionParam } from '../../src/api/types';
import { columnDef } from '../support/domainFixtures';
import { installPopoverPolyfill } from '../support/domPolyfills';



// MultiColumnPicker's dropdown is a native `popover="manual"` top-layer
// element (useNativePopover) — same jsdom gap-fill ModelPicker.test.tsx/
// RowField.test.tsx install.
beforeAll(() => {
  installPopoverPolyfill();
});

afterEach(cleanup);

const COLUMNS_PARAM: ActionParam = {
  name: 'input_columns',
  label: 'Columns to feed each group',
  input: 'columns',
  hint: 'Empty = all non-AI source columns (minus Group by).',
};

// A sheet wide enough to actually reproduce the "40+ chips" scenario the
// owner's screenshot showed, not just a couple of options.
function manyColumns(count: number) {
  return Array.from({ length: count }, (_, i) => columnDef({ id: `col-${i}`, name: `column_${i}` }));
}

function renderColumnsField(values: Record<string, string> = {}, columnCount = 44) {
  const onChange = vi.fn();
  render(
    <ActionParams
      params={[COLUMNS_PARAM]}
      values={values}
      columns={manyColumns(columnCount)}
      onChange={onChange}
    />,
  );
  return { onChange };
}

describe('columns param field layout', () => {
  it('renders the label on its own line, not squeezed into .param-row beside the chip list', () => {
    renderColumnsField();
    const picker = screen.getByTestId('field-input_columns');
    // The old layout wrapped label + MultiColumnPicker together inside a
    // `.param-row` (grid-template-columns: minmax(110px,38%) minmax(0,1fr)) —
    // the fixed-width left track is exactly what squeezed the chip flow.
    expect(picker.closest('.param-row')).toBeNull();
    const label = screen.getByText('Columns to feed each group');
    expect(label.tagName.toLowerCase()).toBe('span');
    expect(label).toHaveClass('form-label');
    expect(label.closest('.param-row')).toBeNull();
  });

  it('still renders every option as a selectable choice and reports typed picks', async () => {
    const user = userEvent.setup();
    const { onChange } = renderColumnsField({}, 44);
    const picker = screen.getByTestId('field-input_columns');
    await user.click(picker);
    const menu = screen.getByTestId('field-input_columns-menu');
    // All 44 columns are real, clickable options — the fix is layout-only,
    // not a change to which columns are offered. hidden:true is the
    // established workaround (DeriveJoinForm.test.tsx et al.) for querying
    // roles inside a native `popover="manual"` element: jsdom's default
    // stylesheet hides `[popover]` unconditionally (no real `:popover-open`
    // engine), so testing-library's accessibility-tree role filter would
    // otherwise see it as display:none.
    expect(within(menu).getAllByRole('option', { hidden: true })).toHaveLength(44);

    await user.click(within(menu).getByRole('option', { name: /column_0/, hidden: true }));
    expect(onChange).toHaveBeenCalledWith('input_columns', ['column_0']);
  });

  it('renders the hint below the picker, same as every other field', () => {
    renderColumnsField();
    expect(screen.getByText('Empty = all non-AI source columns (minus Group by).')).toBeInTheDocument();
  });
});
