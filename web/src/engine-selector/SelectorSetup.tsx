import { useEffect, useRef, useState } from 'react';
import type { SelectorChoice } from '../api/selectorChoices';
import { createProjectProviderKeysApi } from '../api/projectProviderKeys';
import { createLocalProvidersApi } from '../api/localProviders';
import { createTeamLocalModelsApi } from '../api/teamLocalModels';
import { ModelSetupRequestError, modelSetupErrorFactory, startEngineSetup } from '../api/engineSetup';
import type { ModelPullDto } from '../api/types';
import { ModelPullProgress } from '../components/ModelPullProgress';
import { ProviderKeyForm } from './ProviderKeyForm';
import { ModelsGatewayForm } from './ModelsGatewayForm';
import type { ModelsGatewayScope } from '../api/modelsGateway';
import { useSetupRequests } from './setupLifetime';

export interface SelectorSetupProps {
  projectId: string; choice: SelectorChoice; onChanged(): void; onEditingChange(editing: boolean): void;
}
type Setup = NonNullable<SelectorChoice['setup']>;
type CredentialSetup = Extract<Setup, { kind: 'api_key' | 'models_gateway' }>;
type GatewayScope = CredentialSetup['scopes'][number] & { scope: ModelsGatewayScope };
type DownloadSetup = Extract<Setup, { kind: 'engine_setup' | 'artifact_download' }>;
const requestError = () => new Error('Setup request failed');
const workspace = createLocalProvidersApi(requestError);
const projectKeys = createProjectProviderKeysApi(requestError);
const downloads = createLocalProvidersApi(modelSetupErrorFactory);
const teamModels = createTeamLocalModelsApi(modelSetupErrorFactory);

function isMutableGatewayScope(scope: CredentialSetup['scopes'][number]): scope is GatewayScope {
  return scope.can_mutate && (scope.scope === 'workspace' || scope.scope === 'organization');
}

function CredentialSetupPanel({ setup, projectId, onChanged, onEditingChange }: {
  setup: CredentialSetup; projectId: string; onChanged(): void; onEditingChange(editing: boolean): void;
}) {
  const projectScope = setup.kind === 'api_key'
    ? setup.scopes.find((scope) => scope.scope === 'project')
    : undefined;
  const gatewayScope = setup.kind === 'models_gateway'
    ? setup.scopes.find(isMutableGatewayScope)
    : undefined;
  return <section className="selector-setup">
    {setup.kind === 'api_key' && projectScope?.can_mutate && <ProviderKeyForm provider={setup.provider} canMutate compact
      validate={(provider, key, signal) => projectKeys.validateProjectProviderKey(projectId, provider, key, { signal })}
      save={(provider, key, receipt, _cap, signal) => projectKeys.setProjectProviderKey(projectId, provider, key, null, receipt, { signal })}
      onSaved={onChanged} onEditingChange={onEditingChange} />}
    {setup.kind === 'models_gateway' && gatewayScope &&
      <ModelsGatewayForm key={gatewayScope.scope} scope={gatewayScope.scope} compact onSaved={onChanged} onEditingChange={onEditingChange} />}
    {((setup.kind === 'api_key' && !projectScope?.can_mutate) || (setup.kind === 'models_gateway' && !gatewayScope)) &&
      <p className="settings-help">You do not have permission to complete this setup.</p>}
  </section>;
}

function DownloadSetupPanel({ setup, activeOperation, onChanged }: {
  setup: DownloadSetup; activeOperation: ModelPullDto | null; onChanged(): void;
}) {
  const [pull, setPull] = useState(activeOperation ?? setup.blocked_by_operation);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { begin } = useSetupRequests();
  const mounted = useRef(true);
  const pollRequests = useRef(new Set<AbortController>());
  useEffect(() => {
    mounted.current = true;
    const requests = pollRequests.current;
    return () => { mounted.current = false; requests.forEach((controller) => controller.abort()); };
  }, []);
  const org = setup.scope === 'organization';
  const fetchPull = async (id: number): Promise<ModelPullDto> => {
    const controller = new AbortController(); pollRequests.current.add(controller);
    try {
      const fresh = org ? await teamModels.orgGetModelPull(id, { signal: controller.signal })
        : await workspace.getModelPull(id, { signal: controller.signal });
      if (mounted.current) setError(null);
      return fresh;
    } finally { pollRequests.current.delete(controller); }
  };
  const cancelPull = async (id: number) => {
    const controller = new AbortController(); pollRequests.current.add(controller);
    try {
      return org ? await teamModels.orgCancelModelPull(id, { signal: controller.signal })
        : await workspace.cancelModelPull(id, { signal: controller.signal });
    } finally { pollRequests.current.delete(controller); }
  };
  const start = async () => {
    if (!setup.can_mutate || !setup.can_start || starting) return;
    const request = begin(); setStarting(true); setError(null);
    try {
      const result = setup.kind === 'engine_setup'
        ? await startEngineSetup(setup.scope, setup.setup_ref, request.signal)
        : org ? await teamModels.orgStartArtifactPull(setup.setup_ref, false, { signal: request.signal })
          : await downloads.startArtifactPull(setup.setup_ref, false, { signal: request.signal });
      if (!request.isCurrent()) return;
      setPull(result.pull); onChanged();
    } catch (caught) {
      if (!request.isCurrent()) return;
      const busy = caught instanceof ModelSetupRequestError && caught.status === 409;
      if (busy && caught.activePullId !== null) {
        try {
          const operation = org ? await teamModels.orgGetModelPull(caught.activePullId, { signal: request.signal })
            : await downloads.getModelPull(caught.activePullId, { signal: request.signal });
          if (!request.isCurrent()) return;
          setPull(operation); onChanged(); return;
        } catch { if (!request.isCurrent()) return; }
      }
      setError(busy ? 'Another setup operation is active. Recheck to view its progress.' : 'Could not start setup. Recheck and try again.');
      onChanged();
    } finally { if (request.isCurrent()) setStarting(false); }
  };
  const matching = pull?.model === setup.setup_ref;
  const active = pull?.status === 'pending' || pull?.status === 'running';
  return <section className="selector-setup">
    {!setup.can_mutate && <p className="settings-help">Only an authorized owner can start or cancel this setup operation.</p>}
    {pull && !matching && <p className="settings-help">Another setup operation is active.</p>}
    {pull && <ModelPullProgress key={`${pull.id}:${pull.status}`} pull={pull} readOnly={!setup.can_mutate} fetchPull={fetchPull} cancelPull={cancelPull}
      onDone={(fresh) => { if (mounted.current) { setPull(fresh); onChanged(); } }} onFailed={(fresh) => { if (mounted.current) { setPull(fresh); onChanged(); } }} />}
    {(!pull || (matching && pull.capabilities.retry && !active)) && <button className="btn btn-primary" type="button"
      disabled={!setup.can_mutate || !setup.can_start || starting} onClick={() => void start()}>
      {starting ? 'Starting…' : pull ? 'Retry setup' : setup.kind === 'artifact_download' ? 'Download model' : 'Download and set up'}
    </button>}
    {!setup.can_start && !pull && <p className="settings-help">Setup cannot start yet. Recheck the available operations.</p>}
    <button className="btn" type="button" onClick={onChanged}>Recheck</button>
    {error && <p role="alert">{error}</p>}
  </section>;
}

export function SelectorSetup(props: SelectorSetupProps) {
  const setup = props.choice.setup;
  if (!setup) return null;
  const key = `${props.projectId}:${props.choice.choice_id}:${JSON.stringify(setup)}`;
  switch (setup.kind) {
    case 'api_key': case 'models_gateway':
      return <CredentialSetupPanel key={key} {...props} setup={setup} />;
    case 'artifact_download': case 'engine_setup':
      return <DownloadSetupPanel key={key} setup={setup} activeOperation={props.choice.active_operation} onChanged={props.onChanged} />;
    case 'first_use_download': return <p className="settings-help">{setup.disclosure}</p>;
    case 'instructions': return <section className="selector-setup"><strong>{setup.title}</strong><ol>{setup.steps.map((step) => <li key={step}>{step}</li>)}</ol>{setup.url && <a href={setup.url} target="_blank" rel="noreferrer">Learn more</a>}</section>;
  }
}
