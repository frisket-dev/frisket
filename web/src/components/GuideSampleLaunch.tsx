import { useEffect, useRef, useState } from 'react';
import { guideSamplesApi, type GuideSampleMetadata } from '../api/guideSamples';
import { navigate } from '../routes';
import { Button } from './Button';
import { PanelLoading } from './PanelPrimitives';

/** A docs link identifies a first-party sample; it never performs a mutation on arrival. */
export function GuideSampleLaunch({ sampleId }: { sampleId: string }) {
  const query = new URLSearchParams(window.location.search);
  const revisionText = query.get('revision') ?? '1';
  const revision = /^[1-9]\d*$/.test(revisionText) ? Number(revisionText) : NaN;
  const [sample, setSample] = useState<GuideSampleMetadata | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const opening = useRef(false);
  const cancel = () => {
    window.history.replaceState(null, '', window.location.pathname);
    navigate({ kind: 'picker' });
  };

  useEffect(() => {
    const controller = new AbortController();
    if (!Number.isSafeInteger(revision)) {
      setError('Invalid sample revision.');
      return () => controller.abort();
    }
    void guideSamplesApi.get(sampleId, revision, controller.signal).then(setSample, (problem: unknown) => {
      if (!controller.signal.aborted) setError(problem instanceof Error ? problem.message : String(problem));
    });
    return () => controller.abort();
  }, [sampleId, revision]);

  const open = async () => {
    if (opening.current || !sample) return;
    opening.current = true;
    setBusy(true);
    setError(null);
    const url = new URL(window.location.href);
    let operationId = url.searchParams.get('operation_id');
    if (!operationId || !/^[a-f0-9-]{36}$/i.test(operationId)) {
      operationId = crypto.randomUUID();
      url.searchParams.set('operation_id', operationId);
      window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
    }
    try {
      const result = await guideSamplesApi.launch(sampleId, revision, operationId, sample.title);
      const project = `/p/${encodeURIComponent(result.project_id)}`;
      const destination = result.destination;
      const path = destination.kind === 'pdf_import_draft'
        ? `${project}?guideDraft=${encodeURIComponent(destination.draft_id)}`
        : `${project}/s/${encodeURIComponent(String(destination.sheet_id))}`;
      window.history.pushState(null, '', path);
      window.dispatchEvent(new PopStateEvent('popstate'));
    } catch (problem) {
      setError(problem instanceof Error ? problem.message : String(problem));
      opening.current = false;
      setBusy(false);
    }
  };

  return <div className="picker-screen"><div className="picker-card">
    {!sample && !error && <PanelLoading label="Loading sample…" />}
    {sample && <>
      <h1>Open {sample.title}?</h1>
      <p>{sample.description}</p>
      <p>This creates a new project with {sample.file_count} sample files ready to use.</p>
      <Button type="button" disabled={busy} onClick={() => void open()}>{busy ? 'Opening…' : 'Open in Frisket'}</Button>{' '}
      <Button type="button" disabled={busy} onClick={cancel}>Cancel</Button>
    </>}
    {error && <div className="panel-error" role="alert">{error}</div>}
    {!sample && error && <Button type="button" onClick={cancel}>Cancel</Button>}
  </div></div>;
}
