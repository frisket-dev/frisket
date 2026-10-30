// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeAll, expect, it, vi } from 'vitest';
import { ExpectedColumnMenu } from '../../src/workspace/ExpectedColumnMenu';
import { installDialogPolyfill } from '../support/domPolyfills';
beforeAll(installDialogPolyfill);
afterEach(cleanup);
it('updates the rule through its own authority instead of rewriting link metadata', async () => {
  const onRuleChange = vi.fn().mockResolvedValue(undefined);
  const onChange = vi.fn();
  const column = { id: 'expected:1:2', link_id: 1, expected_column_id: 2, output_column_id: 3, logical_key: 'a', name: 'a', rule: 'exact' as const, hidden: false, source: 'import' as const };
  const link = { id: 1, target_sheet_id: 1, expected_sheet_id: 2, action_id: 'map.ask', keys: [], fields: [], hidden: false, expected_sheet_name: 'Expected', target_sheet_name: 'Target' };
  render(<ExpectedColumnMenu column={column} link={link} onClose={vi.fn()} onChange={onChange} onRuleChange={onRuleChange} onEditJoin={vi.fn()} onCompare={vi.fn()} />);
  fireEvent.change(screen.getByLabelText('Match rule'), { target: { value: 'number' } });
  await waitFor(() => expect(onRuleChange).toHaveBeenCalledWith('number'));
  expect(onChange).not.toHaveBeenCalled();
});
