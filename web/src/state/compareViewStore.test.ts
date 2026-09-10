import { describe, expect, it } from 'vitest';
import {
  createCompareViewState,
  createCompareViewStore,
  type CompareKind,
} from './compareViewStore';

const KINDS = [
  'ocr',
  'transcribe',
  'translate',
  'topic',
] as const satisfies readonly CompareKind[];
const EMPTY_TAB = {
  open: false,
  active: false,
  session: { hasData: false, verdict: '' },
  closeWarn: false,
};

describe('createCompareViewStore', () => {
  it('starts with one canonical empty tab for every closed CompareKind', () => {
    const handle = createCompareViewStore();
    expect(handle.store.get()).toEqual(createCompareViewState());
    expect(Object.keys(handle.store.get())).toEqual(KINDS);
    for (const kind of KINDS) expect(handle.store.get()[kind]).toEqual(EMPTY_TAB);
  });

  it.each(KINDS)(
    'open(%s) opens and activates only the target while preserving every peer session/open record',
    (kind) => {
      const handle = createCompareViewStore();
      const peer = KINDS.find((candidate) => candidate !== kind)!;
      handle.open(peer);
      handle.setSession(peer, { hasData: true, verdict: `${peer} verdict` });
      handle.setCloseWarn(peer, true);
      handle.setSession(kind, { hasData: true, verdict: `${kind} draft` });

      handle.open(kind);

      expect(handle.store.get()[kind]).toEqual({
        open: true,
        active: true,
        session: { hasData: true, verdict: `${kind} draft` },
        closeWarn: false,
      });
      expect(handle.store.get()[peer]).toEqual({
        open: true,
        active: false,
        session: { hasData: true, verdict: `${peer} verdict` },
        closeWarn: true,
      });
      for (const other of KINDS.filter(
        (candidate) => candidate !== kind && candidate !== peer,
      )) {
        expect(handle.store.get()[other].active).toBe(false);
      }
    },
  );

  it('focus activates one target and stands down peers without reopening or discarding any tab', () => {
    const handle = createCompareViewStore();
    handle.open('transcribe');
    handle.setSession('transcribe', { hasData: true, verdict: 'kept' });

    handle.focus('ocr');

    expect(handle.store.get().ocr).toEqual({ ...EMPTY_TAB, active: true });
    expect(handle.store.get().transcribe).toEqual({
      open: true,
      active: false,
      session: { hasData: true, verdict: 'kept' },
      closeWarn: false,
    });
  });

  it('dirty request-close warns; cancel dismisses; clean request-close resets only the selected tab', () => {
    const handle = createCompareViewStore();
    handle.open('translate');
    handle.open('topic');
    handle.setSession('translate', { hasData: true, verdict: 'translation' });

    handle.requestClose('translate');
    expect(handle.store.get().translate).toMatchObject({ open: true, closeWarn: true });
    handle.setCloseWarn('translate', false);
    expect(handle.store.get().translate.closeWarn).toBe(false);

    handle.requestClose('topic');
    expect(handle.store.get().topic).toEqual(EMPTY_TAB);
    expect(handle.store.get().translate).toMatchObject({
      open: true,
      session: { hasData: true, verdict: 'translation' },
    });
  });

  it('confirmed close resets only the selected tab', () => {
    const handle = createCompareViewStore();
    handle.open('ocr');
    handle.setSession('ocr', { hasData: true, verdict: 'ocr' });
    handle.open('transcribe');
    handle.setSession('transcribe', { hasData: true, verdict: 'transcribe' });

    handle.close('ocr');

    expect(handle.store.get().ocr).toEqual(EMPTY_TAB);
    expect(handle.store.get().transcribe).toMatchObject({
      open: true,
      active: true,
      session: { hasData: true, verdict: 'transcribe' },
    });
  });

  it('standDown deactivates every tab without closing or discarding any record', () => {
    const handle = createCompareViewStore();
    for (const kind of KINDS) {
      handle.open(kind);
      handle.setSession(kind, { hasData: true, verdict: kind });
      handle.setCloseWarn(kind, true);
    }
    const before = handle.store.get();

    handle.standDown();

    for (const kind of KINDS) {
      expect(handle.store.get()[kind]).toEqual({ ...before[kind], active: false });
    }
  });

});
