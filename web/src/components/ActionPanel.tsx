import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { AlertTriangle, Eye, Play } from 'lucide-react';
import { PanelHeader, PanelLoading } from './PanelPrimitives';
import {
  ActionPresentationCatalogError,
  deriveActionPresentationCatalog,
  resolveActionPresentation,
  type ActionPresentationResolution,
} from './action-panel/actionPresentation';
import { extractFieldFromQuestion } from './action-panel/outputFieldModel';
import type {
  ActionTemplate,
  GeneratedActionCatalogEntry,
  GeneratedActionDraft,
  GeneratedActionRequest,
  PreviewSampleResult,
  ProjectInfo,
  ActionExecutionRequest,
  SheetMeta,
} from '../api/open';
import { isGeneratedActionCatalogEntry } from '../api/open';
import {
  decodeSavedActionSpec,
  SavedActionSpecError,
} from '../actions/savedActionSpec';
import type { ActionLaunchPrefill } from '../actions/actionFormInitial';
import {
  generatedActionTemplateFromCatalogEntry,
  isProjectScopedAction,
} from '../actions/model';
import { useActionCatalogHandle } from '../bind/useActionCatalogHandle';
import { useChromeHandle } from '../bind/useChromeHandle';
import { useRowCacheHandle } from '../bind/useRowCacheHandle';
import { useSelector } from '../bind/useSelector';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { sendProductTelemetry, telemetryActionKind } from '../telemetry/productTelemetry';
import { DeriveActionForm } from './action-panel/DeriveActionForm';
import { pdfTablesMaterializeRequest } from '../actions/pdfTablesMaterialize';
import { columnTablesExportRequest } from '../actions/columnTablesExport';
import type { OcrCompareTarget } from '../actions/ocrCompare';

const EMPTY_SELECTED_ROW_IDS: string[] = [];

interface OpenActionState {
  launchId: number;
  actionTemplate: ActionTemplate;
  initial?: ActionLaunchPrefill;
  generatedDraft?: GeneratedActionDraft;
  generatedCatalogEntry?: GeneratedActionCatalogEntry;
  title?: string;
}

export type ActionFormFrame = ({ actionKind, title, children }: {
  actionKind: string;
  title: string;
  children: ReactNode;
}) => ReactNode;

type ActionOpenIntent =
  | {
      kind: 'proposal';
      key: string;
      proposal: NonNullable<ActionPanelProps['inspectProposal']>;
    }
  | {
      kind: 'route';
      key: string;
      actionKind: string;
      initial?: ActionLaunchPrefill;
      launchId: number;
    };

function actionForRoute(availableActions: ActionTemplate[], routeKind: string): ActionTemplate | undefined {
  return availableActions.find((action) => action.kind === routeKind);
}

export interface ActionPanelProps {
  sheet: SheetMeta | null;
  /** The in-scope project — threaded through purely so onOpenDiagnose below
   *  can seed the Settings back-link's project context; optional/undefined
   *  degrades to no context write, never blocks Diagnose. */
  project?: ProjectInfo | null;
  running: boolean;
  runningLabel?: string;
  selectedRowIds?: string[];
  hasExactRowScopeInitializer?: boolean;
  rowScopeInitializerKey?: string;
  /** Active Detail row used by literal temporal drafts when row markers are
   * not selected. Temporal actions never guess an all-row scope. */
  activeRowId?: string;
  inspectProposal?: { seq: number; title: string; spec: Record<string, unknown> } | null;
  routeActionKind?: string;
  /** Configure-first browser hints carried by a ribbon/menu launch. */
  routeActionInitial?: ActionLaunchPrefill;
  /** Explicit launcher identity from actSurface. Equal visible launches still
   * receive distinct ids; sheet/catalog refreshes retain the current id. */
  routeActionLaunchId?: number;
  actionFormFrame?: ActionFormFrame;
  onActionRouteClose?: () => void;
  onRun(req: ActionExecutionRequest): void;
  onExecuteRegisteredAction: (
    req: GeneratedActionRequest,
    intent: 'preview' | 'run',
    onPreviewComplete?: (result: PreviewSampleResult) => void,
  ) => void;
  /** Drawer × — closes the drawer and returns focus to the grid. */
  onClose?: () => void;
  /** Re-run failed/missing rows (run.backfill path) — enabled iff the resolved
   *  target column currently has failures/gaps; undefined = not offered. */
  onBackfill?: (targetColumnName: string) => void;
  /** The shared configure-first launch seam (runActionFromSurface), threaded
   *  down so an in-form deep link (the "Ask every row" button; the geocode
   *  empty-state's "Run Geocode first") can open a DIFFERENT action prefilled,
   *  without a bespoke navigation mechanism. `initial` carries the
   *  generalized ActionLaunch prefill — additive, optional, unused by most
   *  callers. */
  onNavigateToAction?: (
    actionKind: string,
    sourceColumn?: string,
    initial?: ActionLaunchPrefill,
  ) => void;
  onOpenOcrCompare?(target: OcrCompareTarget): void;
}

function frameActionForm(
  frame: ActionFormFrame | undefined,
  actionKind: string,
  title: string,
  children: ReactNode,
): ReactNode {
  return frame ? frame({ actionKind, title, children }) : children;
}

export function ActionPanel({
  sheet,
  project,
  running,
  runningLabel = 'Running…',
  selectedRowIds = EMPTY_SELECTED_ROW_IDS,
  hasExactRowScopeInitializer = false,
  rowScopeInitializerKey = 'all',
  activeRowId,
  inspectProposal,
  routeActionKind,
  routeActionInitial,
  routeActionLaunchId = 0,
  actionFormFrame,
  onRun,
  onExecuteRegisteredAction,
  onClose,
  onBackfill,
  onNavigateToAction,
  onOpenOcrCompare,
}: ActionPanelProps) {
  const actionCatalog = useActionCatalogHandle();
  const { chromePreferences, projectApi } = useWorkspaceStores();
  const resolveGeneratedActionParams = useCallback(
    (request: Pick<GeneratedActionRequest, 'action_id' | 'scope' | 'params'>) => (
      projectApi.resolveActionParams(request)
    ),
    [projectApi],
  );
  const estimateGeneratedAction = useCallback(
    (request: GeneratedActionRequest) => projectApi.estimateAction(request),
    [projectApi],
  );
  // Engine-availability disclosure: a single store action threaded down as
  // one prop — chromeStore's openDiagnosePanel(), which navigates to the
  // Settings > Diagnostics section. `project` (above) rides along so the
  // Settings back-link returns to this project.
  const chrome = useChromeHandle();
  // Routing chips: a best-effort client-side value sample — reads whatever
  // page 0 of the SAME row cache the grid itself populates already has
  // loaded for this sheet (no fetch triggered here; an unloaded page just
  // means no sample yet, which ActionForm treats as "no chip"). rowCache's
  // handle reference is stable for the project's lifetime (bind/
  // useRowCacheHandle.ts), so it's a safe useCallback dep alongside sheet.id.
  const rowCache = useRowCacheHandle();
  const sampleColumnValues = useCallback(
    (columnId: string): string[] => {
      if (!sheet) return [];
      const page = rowCache.getSlot(sheet.id).getPage(0);
      if (!page || page === 'loading') return [];
      const values: string[] = [];
      for (const row of page) {
        const value = row.cells[columnId];
        if (typeof value === 'string' && value.trim()) values.push(value.trim());
        if (values.length >= 10) break;
      }
      return values;
    },
    [rowCache, sheet],
  );
  const catalogSnapshot = useSelector(actionCatalog.store, (state) => state);
  const availableActions = catalogSnapshot.resolvedTemplates;
  const drawerActions = availableActions;
  const catalogLoaded = catalogSnapshot.status === 'ready';
  const presentationCatalog = useMemo(() => {
    if (!catalogLoaded || !catalogSnapshot.catalog) {
      return { dispositions: null, error: null };
    }
    try {
      return {
        dispositions: deriveActionPresentationCatalog(
          catalogSnapshot.catalog,
          catalogSnapshot.resolvedTemplates,
        ),
        error: null,
      };
    } catch (error) {
      return {
        dispositions: null,
        error: error instanceof ActionPresentationCatalogError
          ? error.message
          : 'The action presentation catalog is invalid.',
      };
    }
  }, [catalogLoaded, catalogSnapshot.catalog, catalogSnapshot.resolvedTemplates]);
  const [openAction, setOpenAction] = useState<OpenActionState | null>(null);
  const [openActionError, setOpenActionError] = useState<string | null>(null);
  const lastOpenKeyRef = useRef<string | null>(null);
  const lastTelemetryOpenRef = useRef<number | null>(null);

  useEffect(() => {
    if (!openAction || lastTelemetryOpenRef.current === openAction.launchId) return;
    lastTelemetryOpenRef.current = openAction.launchId;
    sendProductTelemetry({
      type: 'Action.opened',
      properties: {
        action: telemetryActionKind(openAction.actionTemplate.actionKind ?? openAction.actionTemplate.kind),
      },
    }, chromePreferences.projectId);
  }, [chromePreferences.projectId, openAction]);

  const openIntent = useMemo((): ActionOpenIntent | null => {
    // A direct launcher route is the visible drawer target when both inputs
    // briefly coexist during navigation; proposals are otherwise inspected.
    if (routeActionKind) {
      return {
        kind: 'route',
        key: `route:${routeActionLaunchId}|${routeActionKind}|${routeActionInitial?.sourceColumn ?? ''}|${routeActionInitial?.prompt ?? ''}`,
        actionKind: routeActionKind,
        initial: routeActionInitial,
        launchId: routeActionLaunchId,
      };
    }
    if (inspectProposal) {
      return {
        kind: 'proposal',
        key: `proposal:${inspectProposal.seq}:${sheet?.id ?? 'project'}`,
        proposal: inspectProposal,
      };
    }
    return null;
  }, [
    inspectProposal,
    routeActionInitial,
    routeActionKind,
    routeActionLaunchId,
    sheet?.id,
  ]);

  useEffect(() => {
    if (!openIntent) {
      lastOpenKeyRef.current = null;
      return;
    }
    // Wait for the catalog to settle so the form initialises from the merged
    // template (source_requirements/defaults), not the static shell — the effect
    // re-runs when catalogLoaded flips.
    if (!catalogLoaded) return;
    if (lastOpenKeyRef.current === openIntent.key) return;
    const catalog = catalogSnapshot.catalog;
    if (!catalog) return;
    let alive = true;
    let nextAction: OpenActionState | null = null;
    let nextError: string | null = null;
    const commit = () => {
      // Advance the last-opened key ONLY when the dispatch actually lands. Setting
      // it synchronously here loses the open under StrictMode's dev mount double-
      // invoke: effect#1 sets the ref + queues the microtask, its cleanup flips
      // alive=false, then effect#2 early-returns on the matching ref and the
      // surviving microtask no-ops — leaving the drawer stuck on "Loading action…"
      // when the panel mounts fresh (launch-driven overlay). Guarding the ref
      // behind the alive check makes effect#2 re-queue and open the form.
      queueMicrotask(() => {
        if (!alive) return;
        lastOpenKeyRef.current = openIntent.key;
        setOpenAction(nextAction);
        setOpenActionError(nextError);
      });
    };

    if (openIntent.kind === 'proposal') {
      const { proposal } = openIntent;
      const openProposal = async () => {
        try {
          const decoded = decodeSavedActionSpec(catalog, proposal.spec);
          const resolution = await resolveGeneratedActionParams({
            action_id: decoded.registeredDraft.action_id,
            scope: decoded.registeredDraft.scope,
            params: decoded.registeredDraft.params,
          });
          if (!alive) return;
          if (Object.values(resolution.diagnostics).some((diagnostic) => !diagnostic.ok)) {
            throw new SavedActionSpecError();
          }
          const disposition = presentationCatalog.dispositions?.get(decoded.entry.kind);
          const hidden = disposition?.kind === 'hidden';
          const actionTemplate = availableActions.find((candidate) => (
            (candidate.actionKind ?? candidate.kind) === decoded.entry.kind
          ));
          const schemaProperties = decoded.entry.input_schema.properties ?? {};
          const nestedSource = decoded.params.source;
          const savedSheetId = (decoded.registeredDraft.scope.kind === 'sheet_rows'
            ? decoded.registeredDraft.scope.sheet_id : undefined)
            ?? (Object.prototype.hasOwnProperty.call(schemaProperties, 'sheet_id')
            && typeof decoded.params.sheet_id === 'number'
            ? decoded.params.sheet_id
            : Object.prototype.hasOwnProperty.call(schemaProperties, 'source_sheet_id')
              && typeof decoded.params.source_sheet_id === 'number'
              ? decoded.params.source_sheet_id
              : Object.prototype.hasOwnProperty.call(schemaProperties, 'source')
                && nestedSource !== null
                && typeof nestedSource === 'object'
                && !Array.isArray(nestedSource)
                && typeof (nestedSource as Record<string, unknown>).sheet_id === 'number'
                ? (nestedSource as Record<string, number>).sheet_id
                : undefined);
          const mountedSheetId = sheet ? Number(sheet.id) : undefined;

          if (hidden) {
            nextError = `Action unavailable: ${decoded.entry.kind} has no action drawer launcher.`;
          } else if (
            sheet && typeof savedSheetId === 'number'
            && Number.isSafeInteger(mountedSheetId)
            && savedSheetId !== mountedSheetId
          ) {
            nextError = `This saved action reads from sheet ${savedSheetId}, but this drawer is open on sheet ${sheet.id}. Open sheet ${savedSheetId} to Inspect it; the proposal can still be run directly.`;
          } else if (!actionTemplate) {
            nextError = `Action unavailable: ${decoded.entry.kind} is not in this catalog.`;
          } else {
            nextAction = {
              launchId: proposal.seq,
              actionTemplate: generatedActionTemplateFromCatalogEntry(decoded.entry) ?? actionTemplate,
              generatedDraft: decoded.registeredDraft,
              generatedCatalogEntry: decoded.entry,
              title: proposal.title,
            };
          }
        } catch (error) {
          if (!alive) return;
          nextError = error instanceof SavedActionSpecError
            ? error.message
            : 'Saved action validation is temporarily unavailable. Try again.';
        }
        commit();
      };
      void openProposal();
      return () => {
        alive = false;
      };
    } else {
      const actionTemplate = actionForRoute(availableActions, openIntent.actionKind);
      if (!actionTemplate) {
        nextError = `Action unavailable: ${openIntent.actionKind} is not in this catalog.`;
      } else {
        const initial = openIntent.initial;
        const boundColumn = initial?.sourceColumn
          && sheet?.columns.some((column) => column.name === initial.sourceColumn)
          ? initial.sourceColumn
          : null;
        const redirectOutputFields = actionTemplate.kind === 'map.extract' && initial?.prompt
          ? [extractFieldFromQuestion(initial.prompt)]
          : null;
        const catalogEntry = catalog.actions.find((entry) => (
          entry.kind === (actionTemplate.actionKind ?? actionTemplate.kind)
        ));
        const generatedCatalogEntry = catalogEntry
          && isGeneratedActionCatalogEntry(catalogEntry)
          ? catalogEntry
          : undefined;
        nextAction = {
          launchId: openIntent.launchId,
          actionTemplate,
          generatedCatalogEntry,
          ...(initial?.actionDraft && initial.actionDraft.action_id === generatedCatalogEntry?.kind
            ? { generatedDraft: initial.actionDraft } : {}),
          ...((boundColumn || initial?.prompt) ? {
            initial: {
              ...(boundColumn ? { sourceColumn: boundColumn } : {}),
              ...(initial?.prompt ? { prompt: initial.prompt } : {}),
              ...(redirectOutputFields ? { outputFields: redirectOutputFields } : {}),
            },
          } : {}),
        };
      }
    }

    commit();
    return () => {
      alive = false;
    };
  }, [
    availableActions,
    catalogLoaded,
    catalogSnapshot.catalog,
    openIntent,
    presentationCatalog.dispositions,
    resolveGeneratedActionParams,
    sheet,
  ]);
  const openActionCatalogKind = openAction
    ? openAction.actionTemplate.actionKind
    : undefined;
  const openActionCatalogMatches = openActionCatalogKind
    ? catalogSnapshot.catalog?.actions.filter((entry) => entry.kind === openActionCatalogKind) ?? []
    : [];
  const openCatalogEntry = openActionCatalogMatches.length === 1
    ? openActionCatalogMatches[0]
    : undefined;
  const currentGeneratedCatalogEntry = openCatalogEntry
    && isGeneratedActionCatalogEntry(openCatalogEntry)
    ? openCatalogEntry
    : undefined;
  const generatedCatalogEntry = currentGeneratedCatalogEntry
    ?? (!catalogLoaded ? openAction?.generatedCatalogEntry : undefined);
  const pendingOpenPresentation = useMemo((): {
    disposition: ActionPresentationResolution | null;
    error: string | null;
  } => {
    if (catalogLoaded || !openAction) return { disposition: null, error: null };
    try {
      return {
        // The template crossed the complete-catalog gate when this drawer
        // opened. During refresh only, preserve that accepted renderer so
        // authored local state remains mounted; the loading snapshot still
        // cannot authorize Run.
        disposition: resolveActionPresentation(openAction.actionTemplate),
        error: null,
      };
    } catch (error) {
      return {
        disposition: null,
        error: error instanceof ActionPresentationCatalogError
          ? error.message
          : 'The accepted action presentation is invalid.',
      };
    }
  }, [catalogLoaded, openAction]);
  const openDisposition = catalogLoaded
    ? openActionCatalogKind
      ? presentationCatalog.dispositions?.get(openActionCatalogKind)
      : undefined
    : pendingOpenPresentation.disposition ?? undefined;
  const presentationDispositionError = catalogLoaded && presentationCatalog.error
    ? `Action unavailable: ${presentationCatalog.error}`
    : pendingOpenPresentation.error
      ? `Action unavailable: ${pendingOpenPresentation.error}`
      : catalogLoaded && openAction && !openDisposition
      ? `Action unavailable: ${openActionCatalogKind ?? openAction.actionTemplate.kind} has no presentation disposition.`
      : openDisposition?.kind === 'hidden'
        ? `Action unavailable: ${openActionCatalogKind} has no action drawer launcher.`
      : openDisposition?.kind === 'generated' && !generatedCatalogEntry
        ? `Action unavailable: ${openActionCatalogKind} has no generated form contract.`
        : null;
  const unavailableActionError = openActionError
    ?? presentationDispositionError
    ?? (openAction && !sheet
      && (!generatedCatalogEntry || !isProjectScopedAction(generatedCatalogEntry))
      ? 'Choose a sheet before opening this action.' : null)
    ?? (
    catalogLoaded && routeActionKind && !actionForRoute(availableActions, routeActionKind)
      ? `Action unavailable: ${routeActionKind} is not in this catalog.`
      : null
  );
  const drawerError = catalogSnapshot.status === 'error'
    ? {
        summary: 'Action catalog unavailable.',
        detail: catalogSnapshot.error ?? 'The catalog failed or was invalid.',
      }
    : unavailableActionError
      ? { summary: 'Action unavailable.', detail: unavailableActionError }
      : null;

  const actionSelectedRowIds = useMemo(() => {
    const draft = openAction?.generatedDraft;
    if (!draft) return selectedRowIds;
    return draft.scope.kind === 'sheet_rows'
      ? draft.scope.row_ids?.map(String) ?? []
      : [];
  }, [openAction, selectedRowIds]);
  // Temporal literal launches keep the existing active-row fallback. Saved
  // requests and explicit row selections remain authoritative.
  const generatedSelectedRowIds = useMemo(() => ['derive.transcript_segments',
    'derive.temporal_segments', 'temporal.extract_range'].includes(generatedCatalogEntry?.kind ?? '')
    && !openAction?.generatedDraft && !hasExactRowScopeInitializer
    && actionSelectedRowIds.length === 0 && activeRowId
    ? [activeRowId] : actionSelectedRowIds, [generatedCatalogEntry?.kind,
    openAction?.generatedDraft, hasExactRowScopeInitializer, actionSelectedRowIds, activeRowId]);

  // Drawer variant: the FORM only, re-hosted in
  // the fixed-400px overlay. No discovery list, no resize handle — the drawer
  // opens exclusively pre-targeted, so openAction is (transiently) null only
  // until the route effect dispatches; render a light placeholder meanwhile
  // rather than the retired browse list.
  return (
    <div className="action-panel action-drawer-form-host" data-testid="action-panel" data-variant="drawer">
      {drawerError ? (
        <div className="action-form action-catalog-error" role="alert">
          <PanelHeader
            className="panel-header form-header action-drawer-header"
            title="Action unavailable"
            onClose={onClose}
            closeTestId="action-drawer-close"
            closeClassName="icon-btn panel-frame-close action-drawer-close"
            closeAriaLabel="Close action drawer"
            closeIconSize={16}
          />
          <div className="action-credential-gate">
            <AlertTriangle size={14} aria-hidden />
            <div className="action-credential-gate-copy">
              <strong>{drawerError.summary}</strong>
              <span> {drawerError.detail}</span>
            </div>
          </div>
          <div className="form-actions action-run-actions">
            <div className="run-actions-row">
              <button type="button" className="btn" data-testid="preview-button" disabled>
                <Eye size={13} /> Preview
              </button>
              <button type="button" className="btn btn-primary" data-testid="run-button" disabled>
                <Play size={13} /> Run
              </button>
            </div>
          </div>
        </div>
      ) : openAction === null ? (
        <PanelLoading testId="action-drawer-loading" label="Loading action…" />
      ) : openDisposition?.kind === 'generated' && generatedCatalogEntry ? (
        frameActionForm(
          actionFormFrame,
          openAction.actionTemplate.kind,
          openAction.title ?? openAction.actionTemplate.name,
          <DeriveActionForm
            projectId={chromePreferences.projectId}
            extractEntry={catalogSnapshot.catalog?.actions.find((entry) => entry.kind === 'map.extract' && isGeneratedActionCatalogEntry(entry)) as GeneratedActionCatalogEntry | undefined}
            onCompositeRun={onRun}
            key={`${sheet?.id ?? 'project'}:${generatedCatalogEntry.kind}:${openAction.launchId}:${rowScopeInitializerKey}`}
            catalogEntry={generatedCatalogEntry}
            actionTemplate={openAction.actionTemplate}
            title={openAction.title}
            sheet={sheet}
            selectedRowIds={generatedSelectedRowIds}
            hasExactRowScopeInitializer={hasExactRowScopeInitializer}
            initialSourceColumn={openAction.initial?.sourceColumn}
            initialPrompt={openAction.initial?.prompt}
            initialDraft={openAction.generatedDraft}
            catalogAccepted={catalogLoaded && currentGeneratedCatalogEntry !== undefined}
            running={running}
            runningLabel={runningLabel}
            resolveParams={resolveGeneratedActionParams}
            estimateAction={estimateGeneratedAction}
            onExecute={onExecuteRegisteredAction}
            switchActions={drawerActions}
            onSwitchAction={(nextTemplate) => onNavigateToAction?.(nextTemplate.kind)}
            onNavigateToAction={onNavigateToAction}
            onOpenOcrCompare={onOpenOcrCompare}
            sampleColumnValues={sampleColumnValues}
            onOpenDiagnose={() => chrome.openDiagnosePanel(project)}
            onBackfill={onBackfill}
            onMaterializePdfTables={sheet ? (intent) => onRun(pdfTablesMaterializeRequest({
              sheetId: sheet.id, ...intent,
            })) : undefined}
            onExportPdfTables={sheet && catalogSnapshot.catalog?.actions.some((entry) => entry.kind === 'export.column_tables')
              ? (intent) => onRun(columnTablesExportRequest({ sheetId: sheet.id,
                columnId: intent.column.id, groupBy: intent.groupBy, excludeColumns: intent.excludeColumns }))
              : undefined}
            onClose={() => onClose?.()}
          />,
        )
      ) : null}
    </div>
  );
}
