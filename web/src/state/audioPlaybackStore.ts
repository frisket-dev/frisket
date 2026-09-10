// One in-memory audio session per project workspace. The app-level player is
// the only media-element owner; grid and row-detail surfaces are controls over
// this store, never independent players.

import { createStore } from '../core/store/createStore';
import type { Store } from '../core/store/types';

export interface AudioPlaybackSource {
  key: string;
  url: string;
  label: string;
  sheetId: string;
  rowId: string;
  columnId: string;
}

export function createAudioPlaybackSource(
  source: Omit<AudioPlaybackSource, 'key'>,
): AudioPlaybackSource {
  return {
    ...source,
    key: JSON.stringify([
      source.sheetId,
      source.rowId,
      source.columnId,
      source.url,
    ]),
  };
}

export interface AudioPlaybackState {
  source: AudioPlaybackSource | null;
  playing: boolean;
  error: string | null;
  intentVersion: number;
}

export function createAudioPlaybackState(): AudioPlaybackState {
  return { source: null, playing: false, error: null, intentVersion: 0 };
}

export function createAudioPlaybackStore(): {
  store: Store<AudioPlaybackState>;
  toggle(source: AudioPlaybackSource, options?: { allowStart?: boolean }): void;
  setPlaying(sourceKey: string, playing: boolean): void;
  setError(sourceKey: string, message: string): void;
  dismiss(): void;
} {
  const store = createStore<AudioPlaybackState>(createAudioPlaybackState());

  return {
    store,

    toggle(source, options) {
      store.set((state) => {
        const isActiveSource = state.source?.key === source.key && state.playing;
        if (!isActiveSource && options?.allowStart === false) return state;
        if (state.source?.key !== source.key) {
          return {
            source,
            playing: true,
            error: null,
            intentVersion: state.intentVersion + 1,
          };
        }
        return {
          ...state,
          source,
          playing: !state.playing,
          error: null,
          intentVersion: state.intentVersion + 1,
        };
      });
    },

    setPlaying(sourceKey, playing) {
      store.set((state) => (
        state.source?.key !== sourceKey
        || (state.playing === playing && (!playing || state.error === null))
          ? state
          : {
              ...state,
              playing,
              ...(playing ? { error: null } : {}),
              intentVersion: state.intentVersion + 1,
            }
      ));
    },

    setError(sourceKey, message) {
      store.set((state) => (
        state.source?.key !== sourceKey
          ? state
          : {
              ...state,
              playing: false,
              error: message,
              intentVersion: state.intentVersion + 1,
            }
      ));
    },

    dismiss() {
      store.set((state) => (
        state.source === null
          ? state
          : {
              source: null,
              playing: false,
              error: null,
              intentVersion: state.intentVersion + 1,
            }
      ));
    },
  };
}

export type AudioPlaybackStoreHandle = ReturnType<typeof createAudioPlaybackStore>;
