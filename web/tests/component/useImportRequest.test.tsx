// @vitest-environment jsdom
import { renderHook } from '@testing-library/react';
import { expect, it } from 'vitest';
import { useImportRequest } from '../../src/components/importWorkspace/useImportRequest';

it('retires replaced requests and aborts the active request on project change and unmount', () => {
  const view = renderHook(({ projectId }) => useImportRequest(projectId), {
    initialProps: { projectId: 'first-project' },
  });
  const first = view.result.current.begin();
  const second = view.result.current.begin();
  expect(first.signal.aborted).toBe(true);
  expect(second.signal.aborted).toBe(false);
  const commands = view.result.current;

  view.rerender({ projectId: 'first-project' });
  expect(view.result.current).toBe(commands);
  expect(second.signal.aborted).toBe(false);
  view.rerender({ projectId: 'second-project' });
  expect(second.signal.aborted).toBe(true);
  const current = view.result.current.begin();
  expect(current.signal.aborted).toBe(false);
  view.unmount();
  expect(current.signal.aborted).toBe(true);
});
