import { useCallback, type ReactNode } from 'react';
import type { OcrComparePreviewPageResult } from '../api/open';
import { DocumentReader } from './DocumentReader';
import { useCompareVariantConfigure } from './useCompareVariantConfigure';
import type { MediaCompareSession, MediaNavTarget, ScratchDoc } from './mediaCompareSession';

export function useOcrVariantConfigure(
  session: MediaCompareSession<OcrComparePreviewPageResult[]>,
) {
  const editor = useCompareVariantConfigure(session);
  const renderSourcePeek = useCallback(
    (doc: ScratchDoc<OcrComparePreviewPageResult[]>, navTarget: MediaNavTarget | null): ReactNode => {
      const page = navTarget?.kind === 'page' ? navTarget.page : 1;
      return (
        <DocumentReader
          key={`${doc.id}:${page}`}
          media={session.peekMedia}
          mediaKind={doc.mediaKind === 'image' ? 'image' : 'pdf'}
          title={doc.filename}
          layout="continuous"
          fit="width"
          videoFit="full"
          onVideoFitChange={() => undefined}
          textLayer
          onPageCount={() => undefined}
          rowKey={doc.id}
          onOpenDetail={() => undefined}
          canOpenDetail={false}
          optionsOpen={false}
          onToggleOptions={() => undefined}
          optionsPopover={null}
          selectionCount={0}
          initialPage={page}
        />
      );
    },
    [session.peekMedia],
  );

  return { ...editor, addEngineFlow: editor.addVariant, renderSourcePeek };
}
