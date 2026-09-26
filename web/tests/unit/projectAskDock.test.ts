import { describe, expect, it } from 'vitest';
import type { AskEvent } from '../../src/api/projectQA';
import { compactAskToolEvents } from '../../src/components/project-ask/activityEvents';

function event(turn_id: string, seq: number, kind: AskEvent['kind'], tool: string): AskEvent {
  return { thread_id: 'thread', turn_id, seq, created_at: '', kind, payload: { tool } };
}

describe('Ask tool activity pairing', () => {
  it('pairs only matching tool starts and completions within the same turn', () => {
    const olderStart = event('older', 1, 'tool_started', 'inspect_sheets');
    const currentStart = event('current', 2, 'tool_started', 'inspect_sheets');
    const currentComplete = event('current', 3, 'tool_completed', 'inspect_sheets');
    const nextStart = event('current', 4, 'tool_started', 'inspect_sheets');
    const visible = compactAskToolEvents([olderStart, currentStart, currentComplete, nextStart]);
    expect(visible).toEqual([olderStart, currentComplete, nextStart]);
  });
});
