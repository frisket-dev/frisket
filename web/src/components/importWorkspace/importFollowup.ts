import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ActionTemplate } from '../../api/open';
import type { ProjectApi } from '../../api/ports';
import { MENTIONS_EXTRACT_ACTION_KIND } from '../mentionsPanelModel';
import { nerImportOffer, type NerImportOffer } from '../nerImportPromptModel';

/** The one next step a finished import offers, if any. */
export type ImportFollowup =
  | { kind: 'download'; columnName: string; launcherKind: string }
  | { kind: 'ner'; offer: NerImportOffer };

/** The entity-extraction template once the action catalog is ready. */
export function selectNerTemplate(state: {
  status: string;
  resolvedTemplates: ActionTemplate[];
}): ActionTemplate | undefined {
  return state.status === 'ready'
    ? state.resolvedTemplates.find((template) => template.kind === MENTIONS_EXTRACT_ACTION_KIND)
    : undefined;
}

/** Reads the imported sheet's first page and picks its follow-up. A failed
 *  lookup offers nothing: the prompt never blocks an import's own outcome. */
export async function classifyImportFollowup(
  projectApi: Pick<ProjectApi, 'getSheetData'>,
  sheetId: number,
  nerTemplate: ActionTemplate | undefined,
): Promise<ImportFollowup | null> {
  try {
    const page = await projectApi.getSheetData(String(sheetId), 0, 25);
    const candidate = page.columns.find((column) => column.mediaDownloadCandidate);
    if (candidate?.mediaDownloadCandidate) {
      return { kind: 'download', columnName: candidate.name, launcherKind: candidate.mediaDownloadCandidate };
    }
    // Media first when a sheet qualifies for both: undownloaded links are
    // the blocking next step, and there is no text to read until they land.
    const offer = nerImportOffer({ columns: page.columns, rowCount: page.total, nerTemplate });
    return offer ? { kind: 'ner', offer } : null;
  } catch {
    return null;
  }
}

interface PendingImportFollowup {
  sheetId: number;
  followup: ImportFollowup;
}

/** Holds the follow-up for imports that finish after the import dialog has
 *  handed them off. `offer` is called once per finished import; the latest
 *  finished import's follow-up replaces an unanswered earlier one, and a
 *  dismissal also drops a classification still in flight. */
export function useImportFollowup({ projectApi, nerTemplate }: {
  projectApi: Pick<ProjectApi, 'getSheetData'>;
  nerTemplate: ActionTemplate | undefined;
}): {
  pending: PendingImportFollowup | null;
  offer(sheetId: number): void;
  dismiss(): void;
} {
  const [pending, setPending] = useState<PendingImportFollowup | null>(null);
  const nerTemplateRef = useRef(nerTemplate);
  const generation = useRef(0);
  useEffect(() => {
    nerTemplateRef.current = nerTemplate;
  }, [nerTemplate]);
  const offer = useCallback((sheetId: number) => {
    const current = ++generation.current;
    void classifyImportFollowup(projectApi, sheetId, nerTemplateRef.current).then((followup) => {
      if (current !== generation.current || followup === null) return;
      setPending({ sheetId, followup });
    });
  }, [projectApi]);
  const dismiss = useCallback(() => {
    generation.current += 1;
    setPending(null);
  }, []);
  return useMemo(() => ({ pending, offer, dismiss }), [dismiss, offer, pending]);
}
