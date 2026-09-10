// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError, createProjectApi } from '../../src/api/real';
import type { ReplaceRulesPreview, SheetMeta } from '../../src/api/types';
import { ReplaceForm } from '../../src/components/resolve/ReplaceForm';
import type { ReplaceParams } from '../../src/generated/actionTypes';
import { ParamsBodyHarness } from '../support/generatedParamsBodyHarness';
import { createWorkspaceTestHarness } from '../support/workspaceTestHarness';

const api = createProjectApi('test-project');
const { render } = createWorkspaceTestHarness({
  projectId: 'test-project', api: { projectApi: api },
});
const sheet: SheetMeta = {
  id: '7', name: 'People', rowCount: 2542,
  columns: [
    { id: '11', name: 'employer', type: 'text' },
    { id: '12', name: 'notes', type: 'text' },
    { id: '13', name: 'score', type: 'number' },
  ],
};
type PreviewInput = Parameters<typeof api.replaceRulesPreview>[0];
const previewFor = (input: PreviewInput,
  overrides: Partial<ReplaceRulesPreview> = {}): ReplaceRulesPreview => ({
  sheetId: input.sheetId, columnId: '11', totalRows: 2542,
  ruleCounts: input.rules.map((_, index) => ({
    index, matchedRows: (index + 1) * 100, matchedValues: index + 1,
  })),
  unmatchedRows: 638, testResult: null, valueHash: 'sha256:v1',
  ...overrides,
});

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function mount(initialParams: ReplaceParams = {
  source: 'employer', rules: [], unmatched: 'keep',
}) {
  const onParams = vi.fn();
  render(<ParamsBodyHarness Body={ReplaceForm} sheet={sheet}
    initialParams={initialParams} onParams={onParams} />);
  return onParams;
}
async function flushPreview() {
  await act(async () => { await vi.advanceTimersByTimeAsync(400); });
}
const ruleAt = (index: number) => screen.getAllByTestId('resolve-replace-rule')[index];
const setPattern = (index: number, value: string) => fireEvent.change(
  within(ruleAt(index)).getByTestId('resolve-replace-rule-pattern'), { target: { value } },
);
const setTarget = (index: number, value: string) => fireEvent.change(
  within(ruleAt(index)).getByTestId('resolve-replace-rule-target'), { target: { value } },
);
const latest = (spy: ReturnType<typeof vi.fn>) => spy.mock.calls.at(-1)?.[0] as ReplaceParams;

describe('ReplaceForm Params body', () => {
  it('authors ordered first-match rules and reports server counts', async () => {
    const preview = vi.spyOn(api, 'replaceRulesPreview')
      .mockImplementation(async (input) => previewFor(input));
    const onParams = mount();
    setPattern(0, 'acme');
    setTarget(0, 'Acme Corporation');
    fireEvent.click(screen.getByTestId('resolve-replace-add-rule'));
    setPattern(1, 'globex');
    setTarget(1, 'Globex Ltd');
    await flushPreview();
    expect(screen.getAllByTestId('resolve-replace-rule-count')[1]).toHaveTextContent('200 rows');

    fireEvent.click(within(ruleAt(1)).getByTestId('resolve-replace-rule-up'));
    await flushPreview();
    expect(preview.mock.lastCall?.[0].rules.map((rule) => rule.pattern))
      .toEqual(['globex', 'acme']);
    expect((latest(onParams).rules as Array<{ pattern: string }>).map((rule) => rule.pattern))
      .toEqual(['globex', 'acme']);
    expect(screen.getByTestId('resolve-footer-summary')).toHaveTextContent('2 rules match 300');
  });

  it('dims stale downstream counts until the server refresh lands', async () => {
    vi.spyOn(api, 'replaceRulesPreview').mockImplementation(async (input) => previewFor(input));
    mount();
    setPattern(0, 'acme');
    setTarget(0, 'Acme');
    fireEvent.click(screen.getByTestId('resolve-replace-add-rule'));
    setPattern(1, 'globex');
    setTarget(1, 'Globex');
    await flushPreview();
    setPattern(0, 'acme corp');
    expect(screen.getAllByTestId('resolve-replace-rule-count')
      .every((node) => node.classList.contains('resolve-replace-stale'))).toBe(true);
    await flushPreview();
    expect(screen.getAllByTestId('resolve-replace-rule-count')
      .every((node) => !node.classList.contains('resolve-replace-stale'))).toBe(true);
  });

  it('discards an older preview response that lands after the latest one', async () => {
    const pending: Array<{
      input: PreviewInput;
      resolve(value: ReplaceRulesPreview): void;
    }> = [];
    vi.spyOn(api, 'replaceRulesPreview').mockImplementation((input) => (
      new Promise((resolve) => pending.push({ input, resolve }))
    ));
    mount();
    setPattern(0, 'acm');
    setTarget(0, 'Acme');
    await flushPreview();
    setPattern(0, 'acme');
    await flushPreview();
    expect(pending).toHaveLength(2);

    await act(async () => pending[1].resolve(previewFor(pending[1].input, {
      ruleCounts: [{ index: 0, matchedRows: 222, matchedValues: 2 }],
    })));
    expect(screen.getByTestId('resolve-replace-rule-count')).toHaveTextContent('222 rows');
    await act(async () => pending[0].resolve(previewFor(pending[0].input, {
      ruleCounts: [{ index: 0, matchedRows: 111, matchedValues: 1 }],
    })));
    expect(screen.getByTestId('resolve-replace-rule-count')).toHaveTextContent('222 rows');
  });

  it('attributes invalid regex to its row and excludes it until fixed', async () => {
    const preview = vi.spyOn(api, 'replaceRulesPreview').mockImplementation(async (input) => {
      if (input.rules.some((rule) => rule.match === 'regex' && rule.pattern === '(')) {
        throw new ApiError(400, 'replace rule regex pattern does not compile',
          'invalid_regex', { pattern: '(' });
      }
      return previewFor(input);
    });
    mount();
    fireEvent.change(within(ruleAt(0)).getByTestId('resolve-replace-rule-match'), {
      target: { value: 'regex' },
    });
    setPattern(0, '(');
    setTarget(0, 'Broken');
    await flushPreview();
    expect(screen.getByTestId('resolve-replace-rule-error')).toHaveTextContent('does not compile');
    await flushPreview();
    expect(preview.mock.lastCall?.[0].rules).toEqual([]);
    setPattern(0, 'glob(ex|al)');
    await flushPreview();
    expect(preview.mock.lastCall?.[0].rules[0].pattern).toBe('glob(ex|al)');
  });

  it('authors null targets, no-match policy, and saved rules as Params', async () => {
    vi.spyOn(api, 'replaceRulesPreview').mockImplementation(async (input) => previewFor(input));
    const onParams = mount({
      source: 'notes',
      rules: [{ match: 'exact', pattern: 'n/a', target: null, case_sensitive: true }],
      unmatched: 'null',
    });
    expect(screen.getByTestId('resolve-replace-column')).toHaveValue('notes');
    expect(screen.getByTestId('resolve-replace-rule-target-null')).toHaveTextContent('(null)');
    expect(screen.getByTestId('resolve-replace-policy')).toHaveValue('null');
    await flushPreview();
    expect(latest(onParams)).toMatchObject({
      source: 'notes',
      rules: [{ match: 'exact', pattern: 'n/a', target: null, case_sensitive: true }],
      unmatched: 'null',
    });
  });
});
