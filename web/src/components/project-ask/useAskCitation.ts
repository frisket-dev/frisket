import { useEffect, useRef, useState } from 'react';
import type { AskCitation } from '../../api/projectQA';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';

export function hasCitationPreview(citation: AskCitation): boolean {
  return citation.target?.kind === 'web' || citation.status === 'unavailable' || citation.status === 'changed';
}

export function useAskCitation(threadId: string, onOpenSource: (citation: AskCitation) => void) {
  const { qa } = useWorkspaceStores();
  const [source, setSource] = useState<AskCitation | null>(null);
  const [sourceError, setSourceError] = useState<string | null>(null);
  const request = useRef(0);
  useEffect(() => () => { request.current += 1; }, []);
  const openSource = (id: string) => {
    const version = ++request.current;
    setSourceError(null);
    void qa.citation(threadId, id).then((citation) => {
      if (version !== request.current) return;
      setSource(hasCitationPreview(citation) ? citation : null); onOpenSource(citation);
    }).catch(() => { if (version === request.current) setSourceError('Could not open this source. Please try again.'); });
  };
  return { source, sourceError, openSource, closeSource: () => setSource(null) };
}
