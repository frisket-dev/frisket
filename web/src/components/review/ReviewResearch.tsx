import { useState } from 'react';
import type { ReviewBundle, ReviewBundleField } from '../../api/types';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import { Button } from '../Button';
import { ResearchActivitySummary, ResearchDetails, SourceLinks } from '../research/ResearchDetails';
import { ReviewExcerpt } from './ReviewExcerpt';
import { useLoader } from '../../hooks/useLoader';

/** Keyed by run/row/column by its owner: no result can leak into another row. */
export function ReviewResearch({ bundle, field }: {
  bundle: ReviewBundle;
  field: ReviewBundleField;
}) {
  const { projectApi: api } = useWorkspaceStores();
  const [open, setOpen] = useState(false);
  const companion = (bundle.items ?? [...bundle.fields, ...bundle.supportingColumns])
    .find(item => item.outputKey === 'sources');

  const research = useLoader(
    () => api.getRunTraceRow(bundle.runId!, bundle.rowId, field.columnId),
    [api, bundle.runId, bundle.rowId, field.columnId],
    { keepPreviousData: true, enabled: Boolean(bundle.runId) },
  );
  const evidence = research.data;
  const failed = research.error !== null;

  return <section aria-label="Agent Research activity" data-testid="review-research">
    {companion && <>
      <h4>Sources encountered</h4>
      {companion.boundedValue
        ? <ReviewExcerpt excerpt={companion.boundedValue} />
        : <SourceLinks value={companion.value} />}
    </>}
    {evidence ? <ResearchActivitySummary evidence={evidence} />
      : <p>{failed ? 'Research activity could not be loaded.' : 'Loading research activity…'}</p>}
    {evidence && <Button onClick={() => setOpen(true)}>Research details</Button>}
    {open && evidence && <ResearchDetails evidence={evidence}
      answer={field.boundedValue ? undefined : field.value}
      sources={companion?.boundedValue ? undefined : companion?.value}
      onClose={() => setOpen(false)} />}
  </section>;
}
