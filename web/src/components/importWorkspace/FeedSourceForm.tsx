import '../PanelPrimitives.css';
import '../Modal.css';
import './ImportWorkspaceDialog.css';
import './ImportModeRail.css';
import '../SourcesPanel.css';
import { useRef, useState } from 'react';
import { Globe2, ListVideo, RefreshCw, Rss, Scale } from 'lucide-react';
import type { SourceInfo, SourceInterval } from '../../api/open';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import { durationBucket, failureCategory, sendProductTelemetry, type SourceKind } from '../../telemetry/productTelemetry';
import { Button } from '../Button';
import { FormField, TextInput } from '../FormControls';
import { PanelSelect } from '../PanelSelect';
import { PanelFrame, PanelBody, PanelFooter } from '../PanelLayout';

import { SourceConfigFields } from '../sources/SourceConfigFields';
import { buildSourceInput, emptySourceDefinition, INTERVALS, SOURCE_KINDS,
  selectedKind, sourceDefinitionError, sourceKindLabel, type KnownSourceKind as FeedSourceKind } from '../sources/sourceConfig';

function telemetrySourceKind(kind: FeedSourceKind): SourceKind {
  if (kind === 'tiktok_profile') return 'other';
  if (kind === 'api_list_dicts') return 'api';
  if (kind === 'courtlistener_docket') return 'courtlistener';
  return kind;
}

function feedKindLabel(kind: FeedSourceKind): string {
  return kind === 'rss' ? 'RSS' : sourceKindLabel(kind);
}

function feedKindIcon(kind: FeedSourceKind) {
  switch (kind) {
    case 'youtube_playlist':
    case 'youtube_channel':
    case 'tiktok_profile':
      return <ListVideo size={14} aria-hidden />;
    case 'api_list_dicts':
      return <Globe2 size={14} aria-hidden />;
    case 'courtlistener_docket':
      return <Scale size={14} aria-hidden />;
    default:
      return <Rss size={14} aria-hidden />;
  }
}

function detectFeedSourceKind(raw: string): FeedSourceKind {
  const value = raw.trim();
  if (!value) return 'rss';
  if (/^[1-9][0-9]*$/.test(value)) return 'courtlistener_docket';
  if (/^@[\w.-]+$/.test(value)) return 'youtube_channel';
  try {
    const parsed = new URL(value);
    const host = parsed.hostname.toLowerCase().replace(/^www\./, '');
    const path = parsed.pathname.toLowerCase();
    if (host === 'tiktok.com' || host === 'm.tiktok.com') return 'tiktok_profile';
    if (host === 'youtube.com' || host === 'youtu.be' || host.endsWith('.youtube.com')) {
      if (parsed.searchParams.has('list') || path.includes('/playlist')) return 'youtube_playlist';
      if (
        host === 'youtu.be' ||
        path.startsWith('/@') ||
        path.startsWith('/channel/') ||
        path.startsWith('/c/') ||
        path.startsWith('/user/')
      ) {
        return 'youtube_channel';
      }
      return 'youtube_channel';
    }
    if (host === 'courtlistener.com' || host === 'www.courtlistener.com') {
      if (path.startsWith('/docket/')) return 'courtlistener_docket';
    }
    if (path.endsWith('.json') || path.includes('/api/') || path.endsWith('/api')) {
      return 'api_list_dicts';
    }
    if (
      path.endsWith('.xml') ||
      path.endsWith('.rss') ||
      path.endsWith('.atom') ||
      path.includes('/feed') ||
      path.includes('/rss') ||
      path.includes('/atom')
    ) {
      return 'rss';
    }
  } catch {
    // Free-form references such as YouTube handles and CourtListener ids are handled above.
  }
  return 'rss';
}

function defaultFeedName(kind: FeedSourceKind, reference: string): string {
  const label = feedKindLabel(kind);
  const compact = reference
    .trim()
    .replace(/^https?:\/\//i, '')
    .replace(/^www\./i, '')
    .slice(0, 72);
  return compact ? `${label}: ${compact}` : label;
}

interface FeedPopulateState {
  source: SourceInfo;
  busy: boolean;
  error: string | null;
}

export function FeedSourceForm({
  onCreated,
  onImported,
  onError,
  onCancel,
}: {
  onCreated?(): void;
  onImported(sheetId: number): void;
  onError(message: string): void;
  onCancel(): void;
}) {
  const { projectApi: api, job, chromePreferences: { projectId } } = useWorkspaceStores();
  const [state, setState] = useState(() => ({
    ...emptySourceDefinition(''), busy: false, error: null as string | null,
  }));
  const updateForm = (patch: Partial<typeof state>) => setState((current) => ({
    ...current, ...patch,
    ...(patch.url !== undefined || patch.kind !== undefined || patch.apiListPath !== undefined ? { error: null } : {}),
  }));
  const kindTouchedRef = useRef(false);
  // A successful
  // create does NOT close the workspace — it swaps to a "Populate feed?"
  // prompt offering the same run this source's Sources-panel row would (Poll
  // now -> api.fetchSource). No watchRunLinkStore intent slot needed here: unlike
  // the notification -> WatchesPanel handoff (pendingWatchRunLink), this
  // prompt and its onImported target live in the same always-mounted
  // component tree for the lifetime of the dialog — a direct prop (already
  // threaded through ImportWorkspaceDialog) reaches the right place
  // synchronously, so there is no unmounted-target race to buffer against.
  const [populate, setPopulate] = useState<FeedPopulateState | null>(null);
  const {
    name,
    url,
    kind,
    cadence,
    busy,
    error,
  } = state;
  const kindDefinition = selectedKind(state);
  const validationError = sourceDefinitionError(state);

  const create = async () => {
    if (!url.trim() || busy || validationError) return;
    updateForm({ busy: true, error: null });
    const startedAt = performance.now();
    try {
      const created = await api.createSource(buildSourceInput({
        ...state, name: name.trim() || defaultFeedName(kind, url),
      }));
      window.dispatchEvent(new CustomEvent('frisket:sources-changed'));
      // Don't close yet -- surface the populate prompt instead (see the
      // FeedPopulateState comment above).
      setPopulate({ source: created, busy: false, error: null });
      sendProductTelemetry({
        type: 'Source.created',
        properties: {
          sourceKind: telemetrySourceKind(kind), requestDuration: durationBucket(performance.now() - startedAt),
          result: 'success', failureCategory: 'none',
        },
      }, projectId);
    } catch (e) {
      sendProductTelemetry({
        type: 'Source.created',
        properties: {
          sourceKind: telemetrySourceKind(kind), requestDuration: durationBucket(performance.now() - startedAt),
          result: 'failed', failureCategory: failureCategory(e),
        },
      }, projectId);
      const message = `Feed source failed: ${e instanceof Error ? e.message : String(e)}`;
      updateForm({ busy: false, error: message });
      onError(message);
    } finally {
      updateForm({ busy: false });
    }
  };

  const runPopulate = async () => {
    if (!populate || populate.busy) return;
    setPopulate({ ...populate, busy: true, error: null });
    try {
      // The exact fetch a Sources-panel "Poll now" click makes
      // (SourcesPanel.tsx's fetchNow -> sourceApi.fetchSource).
      const result = await api.fetchSource(populate.source.id);
      void job.refresh();
      if (result.sheetId != null) onImported(result.sheetId);
      onCreated?.();
    } catch (e) {
      const message = `Populate failed: ${e instanceof Error ? e.message : String(e)}`;
      setPopulate({ ...populate, busy: false, error: message });
    }
  };

  const dismissPopulate = () => {
    onCreated?.();
  };

  if (populate) {
    return (
      <PanelFrame fill>
      <PanelBody className="content-stack" data-testid="feed-populate-prompt">
        <div className="import-mode-summary">
          {feedKindIcon(kind)}
          <strong>Populate feed?</strong>
        </div>
        <p className="modal-body-text">
          Run <strong>{populate.source.name}</strong> now to pull in its rows
          — the same fetch as Poll now in Sources. You can run it again
          later either way.
        </p>
        {populate.error && (
          <div className="panel-error" role="alert">{populate.error}</div>
        )}
      </PanelBody>
      <PanelFooter>
          <Button
            type="button"

            data-testid="feed-populate-dismiss"
            disabled={populate.busy}
            onClick={dismissPopulate}
          >
            Not now
          </Button>
          <span>The feed is saved. Fetch its rows now or later.</span>
          <Button
            type="button"
            className="btn-primary import-primary"
            data-testid="feed-populate-run"
            disabled={populate.busy}
            onClick={() => void runPopulate()}
          >
            <RefreshCw size={13} className={populate.busy ? 'spin' : undefined} />
            {populate.busy ? 'Starting...' : 'Import'}
          </Button>
      </PanelFooter>
      </PanelFrame>
    );
  }

  return (
    <PanelFrame fill>
    <PanelBody>
    <div className="content-stack" data-testid="source-form">
      <div className="import-mode-summary">
        {feedKindIcon(kind)}
        <strong>Feed</strong>
        <span>{feedKindLabel(kind)}</span>
      </div>
      <FormField label="URL or reference" htmlFor="source-url-input">
        <TextInput
          id="source-url-input"

          placeholder={kindDefinition.placeholder}
          aria-label="Feed URL or reference"
          data-testid="source-url"
          value={url}
          onChange={(e) => {
            const next = e.target.value;
            updateForm({ url: next, kind: kindTouchedRef.current ? kind : detectFeedSourceKind(next) });
          }}
        />
      </FormField>
      <FormField label="Type" htmlFor="source-kind-input">
        <PanelSelect
          id="source-kind-input"
          className="form-input"
          aria-label="Source type"
          data-testid="source-kind"
          value={kind}
          onChange={(e) => {
            kindTouchedRef.current = true;
            updateForm({ kind: e.target.value as FeedSourceKind });
          }}
        >
          {SOURCE_KINDS.map((option) => (
            <option key={option.value} value={option.value}>{feedKindLabel(option.value)}</option>
          ))}
        </PanelSelect>
      </FormField>
      <FormField label="Name" htmlFor="source-name-input">
        <TextInput
          id="source-name-input"

          placeholder={defaultFeedName(kind, url)}
          aria-label="Source name"
          data-testid="source-name"
          value={name}
          onChange={(e) => updateForm({ name: e.target.value })}
        />
      </FormField>

      <SourceConfigFields form={state} onChange={updateForm} />

      <FormField label="Update frequency" htmlFor="source-interval-input">
        <PanelSelect
          id="source-interval-input"
          className="form-input"
          aria-label="Update frequency"
          data-testid="source-interval"
          value={cadence}
          onChange={(e) => updateForm({ cadence: e.target.value as SourceInterval })}
        >
          {INTERVALS.map((i) => (
            <option key={i.value} value={i.value}>{i.label}</option>
          ))}
        </PanelSelect>
      </FormField>
      {(validationError || error) && (
        <div className="panel-error" role="alert">
          {validationError ?? error}
        </div>
      )}
    </div>
    </PanelBody>
    <PanelFooter>
      <Button type="button" disabled={busy} onClick={onCancel}>Cancel</Button>
      <span>Add a feed to keep its source available for updates.</span>
      <Button
        type="button"
        className="btn-primary import-primary"
        data-testid="source-create"
        disabled={busy || !url.trim() || Boolean(validationError)}
        onClick={() => void create()}
      >
        {busy ? 'Adding...' : 'Add feed'}
      </Button>
    </PanelFooter>
    </PanelFrame>
  );
}
