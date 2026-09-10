import { useLayoutEffect, useRef } from 'react';
import { ExternalLink, X } from 'lucide-react';

import { useSelector } from '../bind/useSelector';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';

export function NowPlayingWidget() {
  const { audioPlayback, route, selection } = useWorkspaceStores();
  const state = useSelector(
    audioPlayback.store,
    (snapshot) => snapshot,
    (left, right) => (
      left.source === right.source
      && left.playing === right.playing
      && left.error === right.error
      && left.intentVersion === right.intentVersion
    ),
  );
  const mediaRef = useRef<HTMLAudioElement | null>(null);

  useLayoutEffect(() => {
    const media = mediaRef.current;
    const source = state.source;
    if (!media || !source) return;
    if (!state.playing) {
      media.pause();
      return;
    }
    try {
      const request = media.play();
      void request?.catch(() => {
        const current = audioPlayback.store.get();
        if (
          current.intentVersion !== state.intentVersion
          || current.source?.key !== source.key
          || !current.playing
        ) return;
        audioPlayback.setError(source.key, 'Audio playback could not start.');
      });
    } catch {
      audioPlayback.setError(source.key, 'Audio playback could not start.');
    }
  }, [audioPlayback, state.intentVersion, state.playing, state.source]);

  const source = state.source;
  if (!source) return null;

  const openSource = () => {
    const current = route.store.get();
    route.navigate({
      ...current,
      sheetId: source.sheetId,
      actionKind: null,
      review: false,
      panel: {
        kind: 'row',
        rowId: source.rowId,
        columnId: source.columnId,
      },
    });
    selection.setSelectedRows({
      sheetId: source.sheetId,
      rowIds: [source.rowId],
      rowIndexes: [],
    });
  };

  return (
    <section
      className="now-playing-widget"
      data-testid="now-playing-widget"
      aria-label="Now playing"
    >
      <div className="now-playing-heading">
        <button
          type="button"
          className="now-playing-source"
          title="Open source row"
          onClick={openSource}
        >
          <span>{source.label || 'Audio'}</span>
          <ExternalLink size={12} aria-hidden />
        </button>
        <button
          type="button"
          className="icon-btn now-playing-dismiss"
          aria-label="Stop and dismiss audio"
          title="Stop and dismiss"
          onClick={() => {
            mediaRef.current?.pause();
            audioPlayback.dismiss();
          }}
        >
          <X size={14} aria-hidden />
        </button>
      </div>
      <audio
        key={source.key}
        ref={mediaRef}
        className="now-playing-audio"
        src={source.url}
        controls
        preload="metadata"
        data-testid="now-playing-audio"
        aria-label={`Now playing ${source.label || 'audio'}`}
        onPlay={() => audioPlayback.setPlaying(source.key, true)}
        onPause={() => audioPlayback.setPlaying(source.key, false)}
        onEnded={() => audioPlayback.setPlaying(source.key, false)}
        onError={() => audioPlayback.setError(source.key, 'Audio playback failed.')}
      >
        <track kind="captions" />
      </audio>
      {state.error && <div className="now-playing-error" role="alert">{state.error}</div>}
    </section>
  );
}
