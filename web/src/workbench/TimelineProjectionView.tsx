import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { RefreshCw, RotateCcw } from 'lucide-react';
import type {
  RuntimeProjectionArtifactRef,
  RuntimeProjectionBuildPlan,
  RuntimeProjectionStatus,
  TimelineProjectionArtifact,
} from '../api/types';
import type { PluginProjectionViewContext } from './pluginProjectionViewContext';

interface TimelineProjectionViewProps {
  ctx: PluginProjectionViewContext;
}

type TimelineProjectionState =
  | { phase: 'loading' | 'building'; status?: RuntimeProjectionStatus; artifact?: undefined; buildPlan?: RuntimeProjectionBuildPlan; error?: undefined }
  | { phase: 'ready'; status: RuntimeProjectionStatus; artifact: TimelineProjectionArtifact; buildPlan?: RuntimeProjectionBuildPlan; error?: undefined }
  | { phase: 'empty'; status: RuntimeProjectionStatus; artifact?: undefined; buildPlan?: RuntimeProjectionBuildPlan; error?: undefined }
  | { phase: 'error'; status?: RuntimeProjectionStatus; artifact?: undefined; buildPlan?: RuntimeProjectionBuildPlan; error: string };

function firstArtifactRef(
  result: RuntimeProjectionStatus | RuntimeProjectionBuildPlan | undefined,
): RuntimeProjectionArtifactRef | null {
  return result?.outputs?.artifactRefs?.find((ref) => ref.kind === 'projection_artifact') ?? null;
}

function statusNeedsBuild(status: RuntimeProjectionStatus): boolean {
  return (
    status.status !== 'ready' ||
    status.freshness.state !== 'fresh' ||
    firstArtifactRef(status) === null
  );
}

function messageFromError(error: unknown): string {
  if (error instanceof Error && error.message.trim()) return error.message;
  return 'Timeline projection failed';
}

export function TimelineProjectionView({ ctx }: TimelineProjectionViewProps) {
  const [state, setState] = useState<TimelineProjectionState>({ phase: 'loading' });
  const autoLoadKeyRef = useRef<string | null>(null);
  const projectionRequestKey = useMemo(
    () =>
      JSON.stringify({
        projectionKind: ctx.projection.kind,
        target: ctx.projection.target,
        params: ctx.projection.params,
      }),
    [ctx.projection.kind, ctx.projection.params, ctx.projection.target],
  );

  const loadProjection = useCallback(
    async (mode: 'read' | 'refresh' | 'rebuild' = 'read') => {
      setState((previous) => ({
        phase: mode === 'read' ? 'loading' : 'building',
        status: previous.status,
        artifact: undefined,
        buildPlan: previous.buildPlan,
      }));
      try {
        const initialStatus = await ctx.projection.status();
        let currentStatus = initialStatus;
        let buildPlan: RuntimeProjectionBuildPlan | undefined;
        let ref = firstArtifactRef(initialStatus);
        const requestedBuildMode = mode === 'rebuild' ? 'rebuild' : 'refresh';
        if (mode !== 'read' || statusNeedsBuild(initialStatus)) {
          buildPlan = await ctx.projection.build({ mode: requestedBuildMode });
          ref = firstArtifactRef(buildPlan) ?? ref;
          currentStatus = await ctx.projection.status();
          ref = firstArtifactRef(currentStatus) ?? ref;
        }
        if (!ref) {
          setState({ phase: 'empty', status: currentStatus, buildPlan });
          return;
        }
        const artifact = await ctx.projection.readArtifact(ref);
        setState({ phase: 'ready', status: currentStatus, artifact, buildPlan });
      } catch (error) {
        setState((previous) => ({
          phase: 'error',
          status: previous.status,
          buildPlan: previous.buildPlan,
          error: messageFromError(error),
        }));
      }
    },
    [ctx],
  );

  useEffect(() => {
    if (autoLoadKeyRef.current === projectionRequestKey) return;
    autoLoadKeyRef.current = projectionRequestKey;
    void loadProjection('read');
  }, [loadProjection, projectionRequestKey]);

  const artifact = state.artifact;
  const status = state.status;
  const itemCount = artifact?.metrics.timelineItemCount ?? artifact?.items.length ?? 0;
  const phaseLabel =
    state.phase === 'building'
      ? 'building'
      : status?.status ?? state.phase;
  const freshnessLabel = status?.freshness.state ?? 'unknown';
  const disabled = state.phase === 'loading' || state.phase === 'building';

  return (
    <section
      className="plugin-timeline-projection"
      data-testid="plugin-timeline-projection-view"
      data-projection-kind={ctx.projection.kind}
      data-status={status?.status ?? state.phase}
      data-freshness-state={status?.freshness.state ?? ''}
      data-artifact-id={artifact?.artifactId ?? ''}
      data-generation={artifact?.generation ?? status?.freshness.generation ?? ''}
      data-timeline-item-count={String(itemCount)}
      data-date-column-id={ctx.projection.target.dateColumnId ?? ''}
      data-title-column-id={ctx.projection.target.titleColumnId ?? ''}
    >
      <header className="plugin-timeline-header">
        <div>
          <h2>Timeline</h2>
          <div className="plugin-timeline-status" data-testid="timeline-projection-status">
            <span>{phaseLabel}</span>
            <span>{freshnessLabel}</span>
            <span>{itemCount} items</span>
          </div>
        </div>
        <div className="plugin-timeline-actions">
          <button
            type="button"
            className="icon-btn"
            title="Refresh timeline"
            aria-label="Refresh timeline"
            disabled={disabled}
            onClick={() => void loadProjection('refresh')}
            data-testid="timeline-projection-refresh"
          >
            <RefreshCw size={14} className={disabled ? 'spin' : undefined} />
          </button>
          <button
            type="button"
            className="icon-btn"
            title="Rebuild timeline"
            aria-label="Rebuild timeline"
            disabled={disabled}
            onClick={() => void loadProjection('rebuild')}
            data-testid="timeline-projection-rebuild"
          >
            <RotateCcw size={14} />
          </button>
        </div>
      </header>

      {state.phase === 'error' ? (
        <div className="plugin-timeline-message" data-testid="timeline-projection-error">
          {state.error}
        </div>
      ) : state.phase === 'loading' || state.phase === 'building' ? (
        <div className="plugin-timeline-message" data-testid="timeline-projection-loading">
          Loading timeline...
        </div>
      ) : artifact && artifact.items.length > 0 ? (
        <div className="plugin-timeline-list" data-testid="timeline-projection-items">
          {artifact.items.map((item) => {
            const rowId = String(item.sourceRowId);
            return (
              <button
                type="button"
                key={`${rowId}:${item.date}:${item.title}`}
                className="plugin-timeline-item"
                data-testid="timeline-projection-item"
                data-source-row-id={rowId}
                onClick={() => ctx.navigation.openRow(rowId)}
              >
                <span className="plugin-timeline-item-date">{item.date}</span>
                <span className="plugin-timeline-item-title">{item.title}</span>
                {item.caseId ? (
                  <span className="plugin-timeline-item-case">{item.caseId}</span>
                ) : null}
              </button>
            );
          })}
        </div>
      ) : (
        <div className="plugin-timeline-message" data-testid="timeline-projection-empty">
          No timeline items
        </div>
      )}
    </section>
  );
}
