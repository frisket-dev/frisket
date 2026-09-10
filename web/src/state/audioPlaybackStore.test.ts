import { describe, expect, it } from 'vitest';

import {
  createAudioPlaybackSource,
  createAudioPlaybackStore,
} from './audioPlaybackStore';

const FIRST = createAudioPlaybackSource({
  url: '/audio/first.mp3',
  label: 'first.mp3',
  sheetId: 'sheet-1',
  rowId: 'row-1',
  columnId: 'audio-1',
});

const SECOND = createAudioPlaybackSource({
  url: '/audio/second.mp3',
  label: 'second.mp3',
  sheetId: 'sheet-2',
  rowId: 'row-2',
  columnId: 'audio-2',
});

describe('audioPlaybackStore', () => {
  it('owns one source and toggles that source without creating another session', () => {
    const audio = createAudioPlaybackStore();
    audio.toggle(FIRST);
    expect(audio.store.get()).toEqual({
      source: FIRST,
      playing: true,
      error: null,
      intentVersion: 1,
    });

    audio.toggle(FIRST);
    expect(audio.store.get()).toEqual({
      source: FIRST,
      playing: false,
      error: null,
      intentVersion: 2,
    });
    audio.toggle(FIRST);
    expect(audio.store.get()).toEqual({
      source: FIRST,
      playing: true,
      error: null,
      intentVersion: 3,
    });

    audio.toggle(SECOND);
    expect(audio.store.get()).toEqual({
      source: SECOND,
      playing: true,
      error: null,
      intentVersion: 4,
    });
  });

  it('lets a run-active gate pause only the currently playing source', () => {
    const audio = createAudioPlaybackStore();
    audio.toggle(FIRST);

    audio.toggle(FIRST, { allowStart: false });
    expect(audio.store.get()).toEqual({
      source: FIRST,
      playing: false,
      error: null,
      intentVersion: 2,
    });

    audio.toggle(FIRST, { allowStart: false });
    audio.toggle(SECOND, { allowStart: false });
    expect(audio.store.get()).toEqual({
      source: FIRST,
      playing: false,
      error: null,
      intentVersion: 2,
    });
  });

  it('ignores stale element events after a source switch and dismisses the session', () => {
    const audio = createAudioPlaybackStore();
    audio.toggle(FIRST);
    audio.toggle(SECOND);

    audio.setError(FIRST.key, 'stale failure');
    audio.setPlaying(FIRST.key, false);
    expect(audio.store.get()).toEqual({
      source: SECOND,
      playing: true,
      error: null,
      intentVersion: 2,
    });

    audio.setError(SECOND.key, 'could not play');
    audio.setPlaying(SECOND.key, false);
    expect(audio.store.get()).toEqual({
      source: SECOND,
      playing: false,
      error: 'could not play',
      intentVersion: 3,
    });
    audio.dismiss();
    expect(audio.store.get()).toEqual({
      source: null,
      playing: false,
      error: null,
      intentVersion: 4,
    });
  });
});
