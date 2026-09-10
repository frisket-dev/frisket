import { describe, expect, it } from 'vitest';
import { createWatchRunLinkStore } from './watchRunLinkStore';

const draft = {
  name: 'Open rows watch',
  query: { kind: 'filter', sheet_id: 17, filter: { status: { eq: 'open' } } },
  scope: { kind: 'sheet' as const, sheet_id: 17 },
};

describe('watch intent store', () => {
  it('retains notification handoff until its target acknowledges it', () => {
    const handle = createWatchRunLinkStore();
    handle.setPendingWatchRunLink({ watchId: 1, runId: 2, eventIds: [3] }, 'watches');
    expect(handle.store.get().pendingWatchRunLink?.link.runId).toBe(2);
    handle.ackPendingWatchRunLink();
    expect(handle.store.get().pendingWatchRunLink).toBeNull();
  });

  it('keeps one row action owned through settlement and compare-acks only that action', () => {
    const handle = createWatchRunLinkStore();
    const alpha = handle.claimWatchAction(1, 'rename');
    const beta = handle.claimWatchAction(2, 'run');
    expect(alpha).not.toBeNull();
    expect(beta).not.toBeNull();
    expect(handle.claimWatchAction(1, 'delete')).toBeNull();
    handle.settleWatchAction(alpha!, {
      status: 'Renamed Watch to Alpha 2.', error: null, errorCode: null, watch: {} as never, focusWatchId: 1,
    });
    expect(handle.store.get().watchActions[1]).toMatchObject({ phase: 'settled', intent: 'rename' });
    expect(handle.store.get().watchActions[2]).toMatchObject({ phase: 'pending', intent: 'run' });
    handle.ackWatchAction(alpha!);
    expect(handle.store.get().watchActions[1]).toBeUndefined();
    expect(handle.store.get().watchActions[2]).toBeDefined();
  });

  it('retains a failed create draft and clears it only after a successful outcome is acknowledged', () => {
    const handle = createWatchRunLinkStore();
    handle.setPendingCreateDraft(draft);
    const failed = handle.claimCreateWatch();
    handle.settleCreateWatch(failed!, { status: null, error: 'create failed', errorCode: null, watch: null, focusWatchId: null });
    handle.ackCreateWatch(handle.store.get().pendingCreateAction!);
    expect(handle.store.get().pendingCreateDraft).toEqual(draft);
    const succeeded = handle.claimCreateWatch();
    handle.settleCreateWatch(succeeded!, { status: 'Created Watch Open rows watch.', error: null, errorCode: null, watch: {} as never, focusWatchId: 3 });
    handle.ackCreateWatch(handle.store.get().pendingCreateAction!);
    expect(handle.store.get().pendingCreateDraft).toBeNull();
  });
});
