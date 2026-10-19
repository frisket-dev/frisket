import '../components/engine-selector/EngineSelector.css';
import { useState } from 'react';

import type { SelectorChoice } from '../api/selectorChoices';
import { SegmentedToggle, type SegmentedOption } from '../components/PanelPrimitives';
import { useSetupEditing, useSetupRequests } from './setupLifetime';

export type ModelRouteSelection = NonNullable<SelectorChoice['route_selection']>;
export type ModelRouteId = ModelRouteSelection['selected'];

export interface ModelRouteControlProps {
  projectId: string;
  selection: ModelRouteSelection;
  setRoute(
    projectId: string,
    modelId: string,
    route: ModelRouteId,
    signal: AbortSignal,
  ): Promise<unknown>;
  onChanged(): Promise<boolean>;
  onBusyChange?(busy: boolean): void;
}

function routeOption(option: ModelRouteSelection['options'][number]): SegmentedOption {
  return {
    value: option.id,
    label: option.label,
    title: option.label,
    disabledReason: option.disabled_reason ?? undefined,
    indicator: <span
      className={`engine-selector__route-dot${option.status === 'ready' ? ' is-ready' : ''}`}
      aria-hidden
    />,
  };
}

/** Renders the server-owned route choices for the one model in the detail pane. */
export function ModelRouteControl({
  projectId,
  selection,
  setRoute,
  onChanged,
  onBusyChange,
}: ModelRouteControlProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const { begin } = useSetupRequests();
  const reportBusy = useSetupEditing(onBusyChange);

  const changeRoute = async (route: string) => {
    if (busy || !selection.can_mutate || route === selection.selected) return;
    const next = selection.options.find((option) => option.id === route);
    if (!next || next.disabled_reason) return;
    const request = begin();
    setBusy(true);
    setError(null);
    reportBusy(true);
    try {
      await setRoute(projectId, selection.model_id, next.id, request.signal);
      if (!request.isCurrent()) return;
      await onChanged();
    } catch {
      if (request.isCurrent()) setError('Could not change how this model runs. Try again.');
    } finally {
      if (request.isCurrent()) {
        setBusy(false);
        reportBusy(false);
      }
    }
  };

  return <section className="engine-selector__route-control" data-testid="model-route-control">
    <span className="engine-selector__route-control-label">Use through</span>
    <div className="engine-selector__route-control-body">
      <SegmentedToggle
        ariaLabel="Use model through"
        fullWidth={false}
        className="engine-selector__route-toggle"
        disabled={busy || !selection.can_mutate}
        options={selection.options.map(routeOption)}
        value={selection.selected}
        onValueChange={(value) => void changeRoute(value)}
        buttonTestId={(value) => `model-route-${value}`}
      />
      {!selection.can_mutate && <p className="form-hint">You do not have permission to change this route.</p>}
      {error && <p className="engine-selector__route-error" role="alert">{error}</p>}
    </div>
  </section>;
}
