import { useCallback, type ReactNode } from 'react';
import type { TranscribeCompareEngineResult } from '../api/open';
import { MediaPlayerPeek } from './MediaPlayerPeek';
import { useCompareVariantConfigure } from './useCompareVariantConfigure';
import type { MediaCompareSession, MediaNavTarget, ScratchDoc } from './mediaCompareSession';

export function useTranscribeVariantConfigure(
  session: MediaCompareSession<TranscribeCompareEngineResult>,
) {
  const { updateColumnOptions } = session;
  const clearEngineOptions = useCallback((variantId: string) => {
    // updateColumnOptions merges: explicitly retire settings from the old engine.
    updateColumnOptions(variantId, {
      language: undefined,
      modelSize: undefined,
      vad: undefined,
      diarize: undefined,
      numSpeakers: undefined,
      minSpeakers: undefined,
      maxSpeakers: undefined,
    });
  }, [updateColumnOptions]);
  const editor = useCompareVariantConfigure(session, clearEngineOptions);
  const renderSourcePeek = useCallback(
    (doc: ScratchDoc<TranscribeCompareEngineResult>, nav: MediaNavTarget | null): ReactNode => {
      const seekSeconds = nav?.kind === 'seconds' ? nav.seconds : null;
      return (
        <MediaPlayerPeek
          url={doc.objectUrl}
          mediaKind={doc.mediaKind === 'video' ? 'video' : 'audio'}
          seekSeconds={seekSeconds}
          testidPrefix="transcribe-compare"
        />
      );
    },
    [],
  );

  return { ...editor, onConfigureChooseEngine: editor.onChooseEngine,
    onConfigureDuplicate: editor.onDuplicate, renderSourcePeek };
}
