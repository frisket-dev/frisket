// @vitest-environment jsdom

import '@testing-library/jest-dom/vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { AskEvent } from '../../src/api/projectQA';
import { AskWorkingActivity } from '../../src/components/project-ask/AskToolActivity';

const step = (seq: number): AskEvent => ({ thread_id: 'thread', turn_id: 'turn', seq, created_at: '', kind: 'tool_completed', payload: { tool: 'inspect_sheets' } });

describe('Ask working activity', () => {
  it('keeps every tool step in one disclosure, follows the active turn, then leaves it user-controlled', () => {
    const view = render(<AskWorkingActivity events={[step(1), step(2)]} active />);
    const disclosure = screen.getByText('Working').closest('details')!;
    expect(disclosure).toHaveAttribute('open');
    expect(disclosure.querySelectorAll('li')).toHaveLength(2);
    view.rerender(<AskWorkingActivity events={[step(1), step(2)]} active={false} />);
    expect(disclosure).not.toHaveAttribute('open');
    fireEvent.click(screen.getByText('Working'));
    expect(disclosure).toHaveAttribute('open');
    view.rerender(<AskWorkingActivity events={[step(1), step(2)]} active={false} />);
    expect(disclosure).toHaveAttribute('open');
  });
});
