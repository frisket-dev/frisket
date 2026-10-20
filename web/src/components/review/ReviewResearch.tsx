import { useEffect, useState } from 'react';
import type { ReviewBundle, ReviewBundleField, RunTraceRowEvidence } from '../../api/types';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import { Button } from '../Button';
import { ResearchActivitySummary, ResearchDetails, SourceLinks } from '../research/ResearchDetails';
import { ReviewExcerpt } from './ReviewExcerpt';

/** Keyed by run/row/column by its owner: no result can leak into another row. */
export function ReviewResearch({ bundle, field }: {
  bundle: ReviewBundle;
  field: ReviewBundleField;
}) {
  const { projectApi: api } = useWorkspaceStores();
  const [evidence, setEvidence] = useState<RunTraceRowEvidence | null>(null);
  const [failed, setFailed] = useState(false);
  const [open, setOpen] = useState(false);
  const companion = (bundle.items ?? [...bundle.fields, ...bundle.evidence])
    .find(item => item.outputKey === 'sources');

  useEffect(() => {
    let active = true;
    api.getRunTraceRow(bundle.runId, bundle.rowId, field.columnId)
      .then(result => { if (active) setEvidence(result); })
      .catch(() => { if (active) setFailed(true); });
    return () => { active = false; };
  }, [api, bundle.runId, bundle.rowId, field.columnId]);

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
    {open && evidence && <ResearchDetails evidence={evidence} sources={companion?.value}
      onClose={() => setOpen(false)} />}
  </section>;
}
