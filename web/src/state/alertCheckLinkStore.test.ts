import { describe, expect, it } from 'vitest';
import { createAlertCheckLinkStore } from './alertCheckLinkStore';

const draft = {
  name: 'Open rows watch',
  view_id: 9,
  sheet_id: 17,
};

describe('alert intent store', () => {
  it('retains notification handoff until its target acknowledges it', () => {
    const handle = createAlertCheckLinkStore();
    handle.setPendingAlertCheckLink({ alertId: 1, runId: 2, eventIds: [3] }, 'alerts');
    expect(handle.store.get().pendingAlertCheckLink?.link.runId).toBe(2);
    handle.ackPendingAlertCheckLink();
    expect(handle.store.get().pendingAlertCheckLink).toBeNull();
  });

  it('keeps one row action owned through settlement and compare-acks only that action', () => {
    const handle = createAlertCheckLinkStore();
    const alpha = handle.claimAlertAction(1, 'rename');
    const beta = handle.claimAlertAction(2, 'run');
    expect(alpha).not.toBeNull();
    expect(beta).not.toBeNull();
    expect(handle.claimAlertAction(1, 'delete')).toBeNull();
    handle.settleAlertAction(alpha!, {
      status: 'Renamed alert to Alpha 2.', error: null, errorCode: null, alert: {} as never, focusAlertId: 1,
    });
    expect(handle.store.get().alertActions[1]).toMatchObject({ phase: 'settled', intent: 'rename' });
    expect(handle.store.get().alertActions[2]).toMatchObject({ phase: 'pending', intent: 'run' });
    handle.ackAlertAction(alpha!);
    expect(handle.store.get().alertActions[1]).toBeUndefined();
    expect(handle.store.get().alertActions[2]).toBeDefined();
  });

  it('retains a failed create draft and clears it only after a successful outcome is acknowledged', () => {
    const handle = createAlertCheckLinkStore();
    handle.setPendingCreateDraft(draft);
    const failed = handle.claimCreateAlert();
    handle.settleCreateAlert(failed!, { status: null, error: 'create failed', errorCode: null, alert: null, focusAlertId: null });
    handle.ackCreateAlert(handle.store.get().pendingCreateAction!);
    expect(handle.store.get().pendingCreateDraft).toEqual(draft);
    const succeeded = handle.claimCreateAlert();
    handle.settleCreateAlert(succeeded!, { status: 'Created alert Open rows watch.', error: null, errorCode: null, alert: {} as never, focusAlertId: 3 });
    handle.ackCreateAlert(handle.store.get().pendingCreateAction!);
    expect(handle.store.get().pendingCreateDraft).toBeNull();
  });
});
