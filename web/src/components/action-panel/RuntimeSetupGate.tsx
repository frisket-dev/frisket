import { useEffect, useRef, useState } from 'react';
import type { ActionParamResolution } from '../../api/types';
import { ModelSetupRequestError, startEngineSetup } from '../../api/engineSetup';
import { readModelPull } from '../../api/modelPullOperations';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import { useSelector } from '../../bind/useSelector';
import { modelPullActive } from '../../hooks/useModelPullPolling';
import { Button } from '../Button';

type Setup = NonNullable<ActionParamResolution['runtime_setup']>;
export function RuntimeSetupGate({ setup, onChanged }: { setup: Setup; onChanged(): void }) {
  if (setup.kind === 'instructions') return <section className="selector-setup" data-testid="runtime-setup-gate">
    <strong>{setup.title}</strong>
    <ol>{setup.steps.map((step) => <li key={step}>{step}</li>)}</ol>
    {setup.url && <a href={setup.url} target="_blank" rel="noreferrer">Learn more</a>}
    <Button type="button" onClick={onChanged}>Recheck</Button>
  </section>;
  return <EngineRuntimeSetupGate setup={setup} onChanged={onChanged} />;
}
function EngineRuntimeSetupGate({ setup, onChanged }: {
  setup: Extract<Setup, { kind: 'engine_setup' }>; onChanged(): void;
}) {
  const { runtimeSetup } = useWorkspaceStores();
  const activity = useSelector(runtimeSetup.store, (state) => state);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const initialVersion = useRef(activity.completionVersion);
  useEffect(() => {
    if (initialVersion.current !== activity.completionVersion) {
      initialVersion.current = activity.completionVersion;
      onChanged();
    }
  }, [activity.completionVersion, onChanged]);
  useEffect(() => {
    if (setup.blocked_by_operation) runtimeSetup.observe(setup.blocked_by_operation, setup.scope, setup.can_mutate);
  }, [runtimeSetup, setup.blocked_by_operation, setup.scope, setup.can_mutate]);
  const active = modelPullActive(activity.pull);
  const matching = activity.pull?.model === setup.setup_ref;
  const start = async () => {
    if (starting || active || !setup.can_mutate || !setup.can_start) return;
    setStarting(true); setError(null);
    try {
      const result = await startEngineSetup(setup.scope, setup.setup_ref);
      runtimeSetup.observe(result.pull, setup.scope, setup.can_mutate);
    } catch (caught) {
      if (caught instanceof ModelSetupRequestError && caught.status === 409 && caught.activePullId !== null) {
        try {
          runtimeSetup.observe(await readModelPull(setup.scope, caught.activePullId), setup.scope, setup.can_mutate);
          return;
        } catch { /* Keep the original start failure actionable. */ }
      }
      setError('Could not start browser setup. Recheck and try again.');
    } finally { setStarting(false); }
  };
  return <section className="selector-setup" data-testid="runtime-setup-gate">
    <p className="form-hint">This action needs Chromium to load pages in a browser. Set up browser tools on this computer before running.</p>
    <div className="control-row">
      <Button type="button" className="btn-primary" disabled={starting || active || !setup.can_mutate || !setup.can_start}
        onClick={() => void start()}>{starting ? 'Starting…' : active ? matching ? 'Setting up browser tools…' : 'Another setup is running…' : 'Set up browser tools'}</Button>
      <Button type="button" onClick={onChanged}>Recheck</Button>
    </div>
    {!setup.can_mutate && <p className="form-hint">Only an authorized owner can set up browser tools.</p>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
