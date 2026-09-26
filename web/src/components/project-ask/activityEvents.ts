import type { AskEvent } from '../../api/projectQA';

/** A completed tool replaces only its matching started event in the same turn. */
export function compactAskToolEvents(events: readonly AskEvent[]) {
  const pending = new Map<string, number[]>();
  const hidden = new Set<number>();
  events.forEach((event, index) => {
    const tool = typeof event.payload.tool === 'string' ? event.payload.tool : null;
    if (!tool) return;
    const key = `${event.turn_id}\u0000${tool}`;
    if (event.kind === 'tool_started') {
      pending.set(key, [...(pending.get(key) ?? []), index]);
    } else if (event.kind === 'tool_completed') {
      const starts = pending.get(key);
      const started = starts?.shift();
      if (started !== undefined) hidden.add(started);
      if (!starts?.length) pending.delete(key);
    }
  });
  return events.filter((_, index) => !hidden.has(index));
}

