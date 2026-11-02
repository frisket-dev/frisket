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
  return <GuideSampleLaunchView key={`${sampleId}:${revisionText}`} sampleId={sampleId} revision={revision} />;
}

function GuideSampleLaunchView({ sampleId, revision }: { sampleId: string; revision: number }) {
  const [sample, setSample] = useState<GuideSampleMetadata | null>(null);
  const [error, setError] = useState<string | null>(() => Number.isSafeInteger(revision) ? null : 'Invalid sample revision.');
  const [busy, setBusy] = useState(false);
  const [canStartAgain, setCanStartAgain] = useState(false);
  const opening = useRef(false);
  const active = useRef(false);
  useEffect(() => {
    active.current = true;
    return () => { active.current = false; };
  }, []);
  const cancel = () => {
    window.history.replaceState(null, '', window.location.pathname);
    navigate({ kind: 'picker' });
  };

  useEffect(() => {
    const controller = new AbortController();
    if (!Number.isSafeInteger(revision)) return () => controller.abort();
    void guideSamplesApi.get(sampleId, revision, controller.signal).then(setSample, (problem: unknown) => {
      if (!controller.signal.aborted) setError(problem instanceof Error ? problem.message : String(problem));
    });
    return () => controller.abort();
  }, [sampleId, revision]);

  const open = async (startAgain = false) => {
    if (opening.current || !sample) return;
    opening.current = true;
    setBusy(true);
    setError(null);
    setCanStartAgain(false);
    const url = new URL(window.location.href);
    if (startAgain) url.searchParams.delete('operation_id');
    let operationId = url.searchParams.get('operation_id');
    if (!operationId || !/^[a-f0-9-]{36}$/i.test(operationId)) {
      operationId = crypto.randomUUID();
      url.searchParams.set('operation_id', operationId);
      window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
    }
    const attemptLocation = `${window.location.pathname}${window.location.search}${window.location.hash}`;
    const stillHere = () => active.current
      && `${window.location.pathname}${window.location.search}${window.location.hash}` === attemptLocation;
    try {
      const result = await guideSamplesApi.launch(sampleId, revision, operationId, sample.title);
      if (!stillHere()) return;
      const project = `/p/${encodeURIComponent(result.project_id)}`;
      const destination = result.destination;
      const path = destination.kind === 'pdf_import_draft'
        ? `${project}?guideDraft=${encodeURIComponent(destination.draft_id)}`
        : `${project}/s/${encodeURIComponent(String(destination.sheet_id))}`;
      window.history.pushState(null, '', path);
      window.dispatchEvent(new PopStateEvent('popstate'));
    } catch (problem) {
      if (!stillHere()) return;
      setError(problem instanceof Error ? problem.message : String(problem));
      setCanStartAgain(typeof problem === 'object' && problem !== null
        && 'status' in problem && problem.status === 409);
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
      <Button type="button" disabled={busy} onClick={() => void open(canStartAgain)}>{busy ? 'Opening…' : canStartAgain ? 'Start again' : 'Open in Frisket'}</Button>{' '}
      <Button type="button" disabled={busy} onClick={cancel}>Cancel</Button>
    </>}
    {error && <div className="panel-error" role="alert">{error}</div>}
    {!sample && error && <Button type="button" onClick={cancel}>Cancel</Button>}
  </div></div>;
}
