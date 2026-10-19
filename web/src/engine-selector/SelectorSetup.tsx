import { Button } from '../components/Button';
import { useCallback, useEffect, useId, useRef, useState } from 'react';
import type { HttpSelectorChoicesResponse, SelectorChoice } from '../api/selectorChoices';
import { createProjectProviderKeysApi } from '../api/projectProviderKeys';
import { createLocalProvidersApi } from '../api/localProviders';
import { createTeamLocalModelsApi } from '../api/teamLocalModels';
import { ModelSetupRequestError, modelSetupErrorFactory, startEngineSetup } from '../api/engineSetup';
import type { ModelPullDto } from '../api/types';
import { ModelPullProgress } from '../components/ModelPullProgress';
import { ProviderKeySetupCard } from './ProviderKeySetupCard';
import { providerName } from './providerKeyHelp';
import { ModelsGatewayForm } from './ModelsGatewayForm';
import type { ModelsGatewayScope } from '../api/modelsGateway';
import { cloudConnectionApi, projectCloudRoutingApi } from '../api/cloudConnection';
import { CloudConnectionPanel } from '../cloud/CloudConnectionPanel';
import { useSetupEditing, useSetupRequests } from './setupLifetime';

export interface SelectorSetupProps {
  projectId: string; choice: SelectorChoice; onChanged(): void; onEditingChange(editing: boolean): void;
  cloudSetup?: HttpSelectorChoicesResponse['cloud_setup'];
  /** SelectorField already owns the durable pull poll for its trigger and panel. */
  sharedProgress?: boolean;
  operationError?: boolean;
}
type Setup = NonNullable<SelectorChoice['setup']>;
type CredentialSetup = Extract<Setup, { kind: 'api_key' | 'models_gateway' }>;
type GatewayScope = CredentialSetup['scopes'][number] & { scope: ModelsGatewayScope };
type DownloadSetup = Extract<Setup, { kind: 'engine_setup' | 'artifact_download' }>;
type CloudSetup = Extract<Setup, { kind: 'cloud' }>;
type ConfigurationAction = SelectorChoice['configuration_actions'][number];
type EditingReporter = (owner: string, editing: boolean) => void;
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
  if (setup.kind === 'api_key' && projectScope?.can_mutate) {
    return <ProviderKeySetupCard provider={setup.provider} canMutate
      validate={(provider, key, signal) => projectKeys.validateProjectProviderKey(projectId, provider, key, { signal })}
      save={(provider, key, receipt, _cap, signal) => projectKeys.setProjectProviderKey(projectId, provider, key, null, receipt, { signal })}
      onSaved={onChanged} onEditingChange={onEditingChange} />;
  }
  return <section className="selector-setup">
    {setup.kind === 'models_gateway' && gatewayScope &&
      <ModelsGatewayForm key={gatewayScope.scope} scope={gatewayScope.scope} compact onSaved={onChanged} onEditingChange={onEditingChange} />}
    {((setup.kind === 'api_key' && !projectScope?.can_mutate) || (setup.kind === 'models_gateway' && !gatewayScope)) &&
      <p className="form-hint">You do not have permission to complete this setup.</p>}
  </section>;
}

function DownloadSetupPanel({ setup, activeOperation, onChanged, sharedProgress = false, operationError = false }: {
  setup: DownloadSetup; activeOperation: ModelPullDto | null; onChanged(): void; sharedProgress?: boolean; operationError?: boolean;
}) {
  const [pull, setPull] = useState(activeOperation ?? setup.blocked_by_operation);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { begin } = useSetupRequests();
  const mounted = useRef(true);
  const pollRequests = useRef(new Set<AbortController>());
  const projectedPull = activeOperation ?? setup.blocked_by_operation;
  const [lastProjectedPull, setLastProjectedPull] = useState(projectedPull);
  if (projectedPull !== lastProjectedPull) {
    setLastProjectedPull(projectedPull);
    setPull(projectedPull);
  }
  useEffect(() => {
    mounted.current = true;
    const requests = pollRequests.current;
    return () => { mounted.current = false; requests.forEach((controller) => controller.abort()); };
  }, []);
  const org = setup.scope === 'organization';
  const fetchPull = async (id: number): Promise<ModelPullDto> => {
    const controller = new AbortController(); pollRequests.current.add(controller);
    try {
      return org ? await teamModels.orgGetModelPull(id, { signal: controller.signal })
        : await workspace.getModelPull(id, { signal: controller.signal });
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
  const onTerminal = (fresh: ModelPullDto) => {
    if (mounted.current) {
      setPull(fresh);
      onChanged();
    }
  };
  return <section className="selector-setup">
    {!setup.can_mutate && <p className="form-hint">Only an authorized owner can start or cancel this setup operation.</p>}
    {pull && !matching && <p className="form-hint">Another setup operation is active.</p>}
    {pull && <ModelPullProgress key={`${pull.id}:${pull.status}`} pull={pull} readOnly={!setup.can_mutate} fetchPull={fetchPull} cancelPull={cancelPull}
      poll={!sharedProgress} externalPollError={operationError}
      onDone={sharedProgress ? undefined : onTerminal}
      onFailed={sharedProgress ? undefined : onTerminal} />}
    {!setup.can_start && !pull && <p className="form-hint">Setup cannot start yet. Recheck the available operations.</p>}
    <div className="settings-actions">
      {(!pull || (matching && pull.capabilities.retry && !active)) && <Button className="btn-primary" type="button"
        disabled={!setup.can_mutate || !setup.can_start || starting} onClick={() => void start()}>
        {starting ? 'Starting…' : pull ? 'Retry setup' : setup.kind === 'artifact_download' ? 'Download model' : 'Download and set up'}
      </Button>}
      <Button type="button" onClick={onChanged}>Recheck</Button>
    </div>
    {error && <p role="alert">{error}</p>}
  </section>;
}

function CloudSetupPanel({ setup, projectId, onChanged }: {
  setup: CloudSetup;
  projectId: string;
  onChanged(): void;
}) {
  const [checking, setChecking] = useState(false);
  const [checkError, setCheckError] = useState(false);
  const { begin } = useSetupRequests();
  const recheck = async () => {
    if (checking) return;
    const request = begin();
    setChecking(true); setCheckError(false);
    try {
      await projectCloudRoutingApi.get(projectId, request.signal);
      if (request.isCurrent()) onChanged();
    } catch {
      if (request.isCurrent()) setCheckError(true);
    } finally {
      if (request.isCurrent()) setChecking(false);
    }
  };
  if (setup.state === 'out_of_credits') {
    return <section className="selector-setup cloud-credit-blocker">
      <strong>{setup.account_name ?? 'This account'} is out of Frisket credits</strong>
      <p>Runs that use Frisket credits stop until credits are added. Frisket won’t switch to another key or account on its own.</p>
      <div className="cloud-connection-actions">
        {setup.add_credits_url && <a className="btn btn-primary" href={setup.add_credits_url}
          target="_blank" rel="noreferrer">Add credits ↗</a>}
        {setup.project_settings_url && <a className="btn" href={setup.project_settings_url}>Project settings</a>}
        <Button type="button" disabled={checking} onClick={() => void recheck()}>
          {checking ? 'Checking…' : 'Recheck'}
        </Button>
      </div>
      {checkError && <p role="alert">Could not refresh Cloud account details. Try again.</p>}
    </section>;
  }

  return <div className="cloud-setup-options">
    <CloudConnectionPanel client={cloudConnectionApi} variant="compact"
      intent={setup.state === 'reconnect' ? 'reconnect' : 'connect'}
      promptForAccount={setup.state === 'account_required'}
      onSelectAccount={setup.state === 'reconnect' ? undefined : (selectedAccountId) => (
        projectCloudRoutingApi.patch(projectId, { selectedAccountId }).then(() => undefined)
      )} onChanged={onChanged} />
  </div>;
}

function setupKey(setup: Setup): string {
  switch (setup.kind) {
    case 'api_key':
      return `api-key:${setup.provider}:${setup.scopes.map((scope) => scope.scope).sort().join(',')}`;
    case 'models_gateway':
      return `models-gateway:${setup.scopes.map((scope) => scope.scope).sort().join(',')}`;
    case 'artifact_download': case 'engine_setup':
      return `${setup.kind}:${setup.scope}:${setup.setup_ref}`;
    case 'cloud':
      return `cloud:${setup.state}:${setup.account_id ?? 'none'}`;
    case 'first_use_download':
      return 'first-use-download';
    case 'instructions':
      return `instructions:${setup.title}`;
  }
}

function SetupPanel({ setup, ...props }: SelectorSetupProps & { setup: Setup }) {
  const activeOperation = (setup.kind === 'artifact_download' || setup.kind === 'engine_setup')
    && props.choice.active_operation?.model === setup.setup_ref
    ? props.choice.active_operation
    : null;
  switch (setup.kind) {
    case 'api_key': case 'models_gateway':
      return <CredentialSetupPanel {...props} setup={setup} />;
    case 'cloud':
      return <CloudSetupPanel {...props} setup={setup} />;
    case 'artifact_download': case 'engine_setup':
      return <DownloadSetupPanel setup={setup} activeOperation={activeOperation} onChanged={props.onChanged}
        sharedProgress={props.sharedProgress} operationError={props.operationError} />;
    case 'first_use_download': return <p className="form-hint">{setup.disclosure}</p>;
    case 'instructions': {
      const steps = setup.steps.filter((step) => step !== props.choice.blocker?.message);
      if (!steps.length && !setup.url) return null;
      return <section className="selector-setup"><strong>{setup.title}</strong><ol>{steps.map((step) => <li key={step}>{step}</li>)}</ol>{setup.url && <a href={setup.url} target="_blank" rel="noreferrer">Learn more</a>}</section>;
    }
  }
}

function useOwnedEditing(editingOwner: string, reportEditing: EditingReporter) {
  return useSetupEditing(useCallback(
    (editing: boolean) => reportEditing(editingOwner, editing),
    [editingOwner, reportEditing],
  ));
}

function OwnedSetupPanel({ editingOwner, reportEditing, ...props }:
  SelectorSetupProps & { setup: Setup; editingOwner: string; reportEditing: EditingReporter }) {
  const editing = useOwnedEditing(editingOwner, reportEditing);
  return <SetupPanel {...props} onEditingChange={editing} />;
}

function configurationLabel(action: ConfigurationAction): string {
  switch (action.kind) {
    case 'api_key': return `Use your own ${providerName(action.provider)} API key`;
    case 'models_gateway': return 'Configure models server';
    case 'engine_setup': return 'Set up on this computer';
    case 'artifact_download': return 'Download for local use';
  }
}

function ConfigurationActionPanel({ action, initiallyExpanded, editingOwner, reportEditing, ...props }:
  SelectorSetupProps & { action: ConfigurationAction; initiallyExpanded: boolean;
    editingOwner: string; reportEditing: EditingReporter }) {
  const [expanded, setExpanded] = useState(initiallyExpanded);
  const [hasExpanded, setHasExpanded] = useState(initiallyExpanded);
  const panelId = useId();
  const label = configurationLabel(action);
  const panelEditing = useOwnedEditing(`${editingOwner}:panel`, reportEditing);
  const formEditing = useOwnedEditing(`${editingOwner}:form`, reportEditing);
  const panel = <SetupPanel {...props} onEditingChange={formEditing} setup={action} />;
  const toggle = () => {
    const next = !expanded;
    setExpanded(next);
    if (next) setHasExpanded(true);
    panelEditing(next);
  };
  if (initiallyExpanded) {
    return <section className="selector-configuration-action is-expanded">
      <h3>{label}</h3>
      {panel}
    </section>;
  }
  return <section className={`selector-configuration-action${expanded ? ' is-expanded' : ''}`}>
    <button type="button" className="selector-configuration-toggle" aria-expanded={expanded}
      aria-controls={panelId}
      onClick={toggle}>{label}<span aria-hidden>{expanded ? '−' : '+'}</span></button>
    <div id={panelId} hidden={!expanded}>{hasExpanded && panel}</div>
  </section>;
}

function useEditingOwners(onEditingChange: (editing: boolean) => void): EditingReporter {
  const owners = useRef(new Set<string>());
  const callback = useRef(onEditingChange);
  useEffect(() => { callback.current = onEditingChange; });
  useEffect(() => () => {
    const wasEditing = owners.current.size > 0;
    owners.current.clear();
    if (wasEditing) callback.current(false);
  }, []);
  return useCallback((owner: string, editing: boolean) => {
    const wasEditing = owners.current.size > 0;
    if (editing) owners.current.add(owner);
    else owners.current.delete(owner);
    const isEditing = owners.current.size > 0;
    if (isEditing !== wasEditing) callback.current(isEditing);
  }, []);
}

export function SelectorSetup(props: SelectorSetupProps) {
  const reportEditing = useEditingOwners(props.onEditingChange);
  const activeSetup = props.choice.setup;
  const configurationActions = props.choice.route_selection
    ? []
    : props.choice.configuration_actions.filter((action) => !(
        action.kind === 'api_key'
        && props.choice.status === 'ready'
        && props.choice.execution_route?.kind === 'local_key'
      ));
  const genericCloudSetup = activeSetup?.kind === 'cloud' ? null : props.cloudSetup;
  const activeOwner = activeSetup
    ? `${props.projectId}:${props.choice.choice_id}:active:${setupKey(activeSetup)}`
    : null;
  if (!activeSetup && configurationActions.length === 0 && !genericCloudSetup) return null;
  return <div className="selector-setup-stack">
    {activeSetup && activeOwner && <OwnedSetupPanel key={activeOwner} {...props} setup={activeSetup}
      editingOwner={activeOwner} reportEditing={reportEditing} />}
    {configurationActions.map((action) => {
      const owner = `${props.projectId}:${props.choice.choice_id}:configuration:${setupKey(action)}`;
      return <ConfigurationActionPanel
        key={owner}
        {...props} action={action}
        editingOwner={owner} reportEditing={reportEditing}
        initiallyExpanded={props.choice.status !== 'ready'} />
    })}
    {genericCloudSetup && <section key={`${props.projectId}:generic-cloud`}
      className="selector-generic-cloud-setup">
      {genericCloudSetup.state === 'connect' &&
        <p className="form-hint">Connect Frisket Cloud to see available models.</p>}
      <SetupPanel key={`${props.projectId}:${setupKey(genericCloudSetup)}`}
        {...props} setup={genericCloudSetup} />
    </section>}
  </div>;
}
