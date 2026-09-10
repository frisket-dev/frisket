import { useEffect, useRef } from 'react';

// Source-peek player for the Transcription Compare bake-off
// (workbench-transcribe-compare-bakeoff-v1): a native <audio>/<video> element
// with controls — the analog of OCR's pdf.js peek pane. `seekSeconds` is the
// diff-token seek target: setting it seeks the element via currentTime (the
// page-scroll analog); the applied target is witnessed on data-seek-seconds.
//
// Standalone by design: the compare shell extraction is HELD pending OCR
// Compare v2, and this component is the source-peek parameter that instance
// will plug in.

export interface MediaPlayerPeekProps {
  url: string;
  mediaKind: 'audio' | 'video';
  /** Start immediately when this short-lived player mounts. */
  autoPlay?: boolean;
  /** Seek target in seconds; null leaves playback position alone. */
  seekSeconds: number | null;
  /** Testid namespace, e.g. 'transcribe-compare' → `${prefix}-player`. */
  testidPrefix: string;
}

export function MediaPlayerPeek({ url, mediaKind, autoPlay = false, seekSeconds, testidPrefix }: MediaPlayerPeekProps) {
  const mediaRef = useRef<HTMLMediaElement | null>(null);

  useEffect(() => {
    if (seekSeconds === null || !Number.isFinite(seekSeconds)) return;
    const el = mediaRef.current;
    if (!el) return;
    const apply = () => {
      try {
        el.currentTime = seekSeconds;
      } catch {
        // Metadata not ready / unseekable — data-seek-seconds still records
        // the intent; currentTime lands once the element can seek.
      }
    };
    // readyState >= HAVE_METADATA means the element can seek now.
    if (el.readyState >= 1) {
      apply();
      return;
    }
    el.addEventListener('loadedmetadata', apply, { once: true });
    // Without this, a seekSeconds change before metadata loads stacks a second
    // 'loadedmetadata' listener on the same element (React reuses the DOM node
    // across re-renders) — both fire once metadata arrives, applying a stale
    // seek target after the current one.
    return () => el.removeEventListener('loadedmetadata', apply);
  }, [seekSeconds]);

  const setRef = (el: HTMLMediaElement | null) => {
    mediaRef.current = el;
  };
  const shared = {
    src: url,
    controls: true,
    autoPlay,
    className: 'media-compare-player',
    'data-testid': `${testidPrefix}-player`,
    'data-media-kind': mediaKind,
    'data-seek-seconds':
      seekSeconds === null || !Number.isFinite(seekSeconds)
        ? undefined
        : String(Math.round(seekSeconds)),
  } as const;

  if (mediaKind === 'video') {
    return (
      <video ref={setRef} {...shared}>
        <track kind="captions" />
      </video>
    );
  }
  return (
    <audio ref={setRef} {...shared}>
      <track kind="captions" />
    </audio>
  );
}
