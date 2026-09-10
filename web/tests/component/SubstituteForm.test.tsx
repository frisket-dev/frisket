// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { createProjectApi } from '../../src/api/real';
import type { ColumnValuesPreview, SheetMeta } from '../../src/api/types';
import { SubstituteForm } from '../../src/components/resolve/SubstituteForm';
import type { SubstituteParams } from '../../src/generated/actionTypes';
import { ParamsBodyHarness } from '../support/generatedParamsBodyHarness';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const api = createProjectApi('test-project');
const { render } = createWorkspaceTestHarness({
  projectId: 'test-project', api: { projectApi: api },
});
const sheet: SheetMeta = {
  id: '7', name: 'People', rowCount: 1726,
  columns: [
    { id: '11', name: 'gender_code', type: 'text' },
    { id: '12', name: 'notes', type: 'text' },
    { id: '13', name: 'score', type: 'number' },
  ],
};
const preview = (overrides: Partial<ColumnValuesPreview> = {}): ColumnValuesPreview => ({
  sheetId: '7', columnId: '11', inputColumn: 'gender_code', totalRows: 1726,
  distinct: 5, missing: 9,
  values: [
    { value: '1', count: 842 }, { value: '2', count: 791 },
    { value: '0', count: 63 }, { value: 'M', count: 12 }, { value: 'F', count: 9 },
  ],
  offset: 0, limit: 500, truncated: false, valueHash: 'sha256:vh', search: null,
  ...overrides,
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function mount(initialParams: SubstituteParams = {
  source: 'gender_code', mapping: {}, unmatched: 'keep',
}) {
  const onParams = vi.fn();
  render(<ParamsBodyHarness Body={SubstituteForm} sheet={sheet}
    initialParams={initialParams} onParams={onParams} />);
  return onParams;
}

function latest(onParams: ReturnType<typeof vi.fn>): SubstituteParams {
  return onParams.mock.calls.at(-1)?.[0] as SubstituteParams;
}

describe('SubstituteForm Params body', () => {
  it('enumerates values and emits exact mappings and unmatched policy', async () => {
    vi.spyOn(api, 'columnValuesPreview').mockResolvedValue(preview());
    const onParams = mount();
    await screen.findByTestId('resolve-substitute-facts');
    const rows = screen.getAllByTestId('resolve-substitute-row');
    expect(rows).toHaveLength(5);
    expect(rows[0]).toHaveTextContent('842');
    expect(within(rows[0]).getByTestId('resolve-substitute-top-marker')).toBeInTheDocument();

    fireEvent.change(screen.getAllByTestId('resolve-substitute-target-input')[0], {
      target: { value: 'male' },
    });
    fireEvent.change(screen.getAllByTestId('resolve-substitute-target-input')[1], {
      target: { value: 'female' },
    });
    fireEvent.click(screen.getAllByTestId('resolve-substitute-null-toggle')[2]);
    fireEvent.change(screen.getByTestId('resolve-substitute-policy'), {
      target: { value: 'null' },
    });
    await waitFor(() => expect(latest(onParams)).toMatchObject({
      source: 'gender_code', mapping: { '1': 'male', '2': 'female', '0': null }, unmatched: 'null',
    }));
    expect(screen.getByTestId('resolve-footer-summary')).toHaveTextContent('3 of 5 values mapped');
  });

  it('parses pasted TSV, quoted commas, explicit nulls, and duplicate keys', async () => {
    const user = userEvent.setup();
    vi.spyOn(api, 'columnValuesPreview').mockResolvedValue(preview());
    const onParams = mount();
    await screen.findByTestId('resolve-substitute-facts');
    await user.click(screen.getByTestId('resolve-substitute-mode-paste'));
    await user.type(screen.getByTestId('resolve-substitute-paste'),
      '1\tmale\n"Acme, Inc.","Acme"\n2\t(null)\n__proto__\tsafe\n1\tman\nbad');
    await user.click(screen.getByTestId('resolve-substitute-paste-apply'));

    await waitFor(() => expect(latest(onParams).mapping).toEqual(Object.fromEntries([
      ['1', 'man'], ['Acme, Inc.', 'Acme'], ['2', null], ['__proto__', 'safe'],
    ])));
    expect(screen.getByTestId('resolve-substitute-paste-report'))
      .toHaveTextContent('1 duplicate key');
    expect(screen.getByTestId('resolve-substitute-paste-report')).toHaveTextContent('1 line skipped');
  });

  it('keeps paste authoring available above the enumeration threshold', async () => {
    vi.spyOn(api, 'columnValuesPreview').mockResolvedValue(preview({ distinct: 151 }));
    const onParams = mount();
    expect(await screen.findByTestId('resolve-substitute-threshold-notice'))
      .toHaveTextContent('151 distinct values');
    expect(screen.getByTestId('resolve-substitute-mode-values')).toBeDisabled();
    fireEvent.click(screen.getByTestId('resolve-substitute-mode-paste'));
    fireEvent.change(screen.getByTestId('resolve-substitute-paste'), {
      target: { value: 'unknown\tKnown' },
    });
    fireEvent.click(screen.getByTestId('resolve-substitute-paste-apply'));
    await waitFor(() => expect(latest(onParams).mapping).toEqual({ unknown: 'Known' }));
  });

  it('hydrates saved mappings and reloads enumeration when the host source changes', async () => {
    const spy = vi.spyOn(api, 'columnValuesPreview').mockImplementation(async ({ inputColumn }) => (
      preview({ inputColumn, columnId: inputColumn === 'notes' ? '12' : '11' })
    ));
    const onParams = mount({
      source: 'gender_code', mapping: { '1': 'male', '2': null }, unmatched: 'null',
    });
    await screen.findByTestId('resolve-substitute-facts');
    expect(screen.getAllByTestId('resolve-substitute-target-input')[0]).toHaveValue('male');
    expect(screen.getAllByTestId('resolve-substitute-null-chip')[0]).toBeInTheDocument();

    fireEvent.change(screen.getByTestId('resolve-substitute-column'), { target: { value: 'notes' } });
    await waitFor(() => expect(spy).toHaveBeenLastCalledWith({ sheetId: '7', inputColumn: 'notes' }));
    await waitFor(() => expect(latest(onParams)).toMatchObject({ source: 'notes', mapping: {} }));
  });
});
