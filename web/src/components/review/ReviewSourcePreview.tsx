import { lazy, Suspense, useEffect, useMemo, useRef, useState, type RefObject } from 'react';
import { X } from 'lucide-react';
import { type CellEvidencePayload, type CellValue, type ReviewBundle, type ReviewBundleField } from '../../api/open';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import { PanelLoading } from '../PanelPrimitives';
import { ReviewInputSources } from './ReviewInputSources';
import { FieldValue } from '../RowDrawer';
import { reviewItemLocations, type ReviewItemLocation, type ReviewItemSelection } from './reviewItemLocations';
import { OverflowRow } from '../OverflowRow';
import { reviewCitationSources, sourceLocation, sourcesForField, type ReviewCitationPayload } from './reviewCitationSources';
import styles from './ReviewSourcePreview.module.css';

const LazyArtifactSource = lazy(() => import('../EvidenceViewer').then(({ ArtifactSource }) => ({ default: ArtifactSource })));

type LoadState =
  | { key: string; phase: 'ready'; payloads: ReviewCitationPayload[] }
  | { key: string; phase: 'error' };

interface CitationTarget {
  fieldId: string;
  runId: string;
  rowId: string;
  columnId: string;
}

function currentEvidenceMatchesField(payload: CellEvidencePayload, field: Pick<ReviewBundleField, 'runId'>): boolean {
  const ref = payload.current_value_ref;
  if (!ref || typeof ref !== 'object' || Array.isArray(ref)) return false;
  const runId = (ref as Record<string, unknown>).run_id;
  return (ref as Record<string, unknown>).kind === 'run_result' && String(runId) === field.runId;
}

/** Citation-tab host for a review row. ArtifactSource remains the canonical
 * text/PDF/media renderer; this component only aggregates its inputs. */
export function ReviewSourcePreview({
  bundle,
  activeField,
  sourceEntries,
  selectedItem,
  onItemLocations,
}: {
  bundle: ReviewBundle | undefined;
  activeField: ReviewBundleField | undefined;
  sourceEntries: Array<[string, CellValue]>;
  selectedItem?: ReviewItemSelection | null;
  onItemLocations?: (locations: ReviewItemLocation[]) => void;
}) {
  const { projectApi: api } = useWorkspaceStores();
  const [state, setState] = useState<LoadState | null>(null);
  const [activeSourceId, setActiveSourceId] = useState<string | null>(null);
  const [previousFocus, setPreviousFocus] = useState<{ item: ReviewItemSelection | null; sourceId?: string }>({ item: null });
  const [showRowFields, setShowRowFields] = useState(false);
  const viewerRef = useRef<HTMLDivElement | null>(null);

  const citationTargetsKey = bundle ? citationKey(bundle) : null;
  const inputEntries: Array<[string, CellValue]> = bundle?.sources
    ? bundle.sources.map((source) => [source.columnName, source.value]) : sourceEntries;

  useEffect(() => {
    if (!citationTargetsKey) return undefined;
    let current = true;
    // This key is serialized from our typed fields below, not external JSON.
    const targets = JSON.parse(citationTargetsKey) as CitationTarget[];
    void loadCitationPayloads(targets, api).then(
      (payloads) => { if (current) setState({ key: citationTargetsKey, phase: 'ready', payloads }); },
      () => { if (current) setState({ key: citationTargetsKey, phase: 'error' }); },
    );
    return () => { current = false; };
  }, [api, citationTargetsKey]);

  // Decision, edit and note saves clone fields, but they do not change which
  // result cells own citations. Hide old payloads only for a new target set.
  const currentState = state !== null && state.key === citationTargetsKey ? state : null;
  const sources = useMemo(() => currentState?.phase === 'ready' ? reviewCitationSources(currentState.payloads) : [], [currentState]);
  const tabs = useMemo(() => sourcesForField(sources, activeField?.id), [activeField?.id, sources]);
  const itemLocations = useMemo(() => reviewItemLocations(sources, bundle?.fields ?? []), [sources, bundle?.fields]);
  useEffect(() => { onItemLocations?.(itemLocations); }, [itemLocations, onItemLocations]);
  const focusedLocations = selectedItem && selectedItem.fieldId === activeField?.id
    ? itemLocations.filter((location) => location.fieldId === selectedItem.fieldId && location.index === selectedItem.index) : [];
  const focusedSourceId = focusedLocations[0]?.sourceId;
  const focusChanged = previousFocus.item !== (selectedItem ?? null) || previousFocus.sourceId !== focusedSourceId;
  if (focusChanged) setPreviousFocus({ item: selectedItem ?? null, sourceId: focusedSourceId });
  const sourceId = focusChanged && focusedSourceId ? focusedSourceId : activeSourceId;
  const activeSource = tabs.find((source) => source.id === sourceId) ?? tabs[0] ?? null;
  const focusedSpanKey = focusedLocations.filter((location) => location.sourceId === activeSource?.id).map((location) => location.spanId).join('|');
  const focusedSpanIds = useMemo(() => focusedSpanKey ? focusedSpanKey.split('|') : [], [focusedSpanKey]);
  // Remember the displayed fallback, so a later field preserves that source
  // instead of resurrecting a tab that was no longer available.
  if (activeSource && activeSource.id !== activeSourceId) setActiveSourceId(activeSource.id);

  if (currentState?.phase === 'ready' && tabs.length === 0 && bundle?.sources?.length) {
    return <ReviewInputSources bundle={bundle} />;
  }

  return (
    <section className={styles.pane} data-testid="review-citation-preview" aria-label="Cited sources">
      <div className={styles.tabs}>
        <OverflowRow
          items={tabs}
          getKey={(source) => source.id}
          keepVisibleKey={activeSource?.id}
          className={styles.tabRow}
          triggerClassName={styles.tabOverflow}
          menuClassName={styles.tabMenu}
          triggerTestId="review-citation-tabs-overflow"
          menuTestId="review-citation-tabs-menu"
          overflowLabel="More cited sources"
          renderItem={(source) => <CitationTab source={source} activeField={activeField} selected={source.id === activeSource?.id}
            onChoose={() => { setActiveSourceId(source.id); setShowRowFields(false); }} />}
          renderOverflowItem={(source, close) => <button type="button" role="menuitem" className={styles.tabMenuItem}
            onClick={() => { setActiveSourceId(source.id); setShowRowFields(false); close(); }}>
            {source.kind} · {source.title}{tabLocation(source, activeField) ? ` · ${tabLocation(source, activeField)}` : ''}
          </button>}
        />
        <div className={styles.paneActions}>
          <button type="button" className={styles.paneAction} aria-pressed={showRowFields}
            onClick={() => setShowRowFields((shown) => !shown)}>
            Inputs · {inputEntries.length}
          </button>
        </div>
      </div>
      <div className={styles.viewer} ref={viewerRef}>
        {currentState === null && <p className={styles.empty}>Loading cited sources…</p>}
        {currentState?.phase === 'error' && <p className={styles.empty} role="status">Citations could not be loaded for this result.</p>}
        {currentState?.phase === 'ready' && activeSource && (
          <Suspense fallback={<PanelLoading label="Loading cited source…" />}>
            <FocusedArtifactSource activeSource={activeSource} activeFieldId={activeField?.id}
              focusedSpanIds={focusedSpanIds} selectedItem={selectedItem} viewerRef={viewerRef} />
          </Suspense>
        )}
        {currentState?.phase === 'ready' && !activeSource && <p className={styles.empty}>No cited sources are recorded for this field.</p>}
      </div>
      {showRowFields && <RowFieldsDrawer entries={inputEntries} bundle={bundle} onClose={() => setShowRowFields(false)} />}
    </section>
  );
}

/** Keep focus effects inside Suspense so they run after the source mounts. */
function FocusedArtifactSource({ activeSource, activeFieldId, focusedSpanIds, selectedItem, viewerRef }: {
  activeSource: ReturnType<typeof reviewCitationSources>[number];
  activeFieldId: string | undefined;
  focusedSpanIds: readonly string[];
  selectedItem: ReviewItemSelection | null | undefined;
  viewerRef: RefObject<HTMLDivElement | null>;
}) {
  // ArtifactSource owns text/media scrolling. PDF source pages expose stable
  // anchors, so the review host can target a page without cloning its renderer.
  useEffect(() => {
    if (!activeFieldId) return;
    // A new item can switch source tabs. Never seek the old recording while
    // that tab transition is pending, or an unrelated tab chosen by the user.
    if (selectedItem && focusedSpanIds.length === 0) return;
    const viewer = viewerRef.current;
    if (!viewer) return;
    const page = selectedPage(activeSource, activeFieldId, focusedSpanIds);
    if (page !== null) {
      viewer.querySelector<HTMLElement>(`#evidence-page-${CSS.escape(activeSource.id)}-${page}`)
        ?.scrollIntoView({ block: 'start' });
    }
    const emphasized = viewer.querySelector<HTMLElement>('[data-emphasized="true"]');
    emphasized?.scrollIntoView({ block: 'center' });
    if (selectedItem) emphasized?.closest('.evidence-transcript-line')?.querySelector<HTMLButtonElement>('button')?.click();
    // The canonical temporal segment button owns media readiness, seeking and
    // the autoplay-rejection guard. Reuse that behavior when a shared source
    // remains selected while the reviewer moves to a different field.
    viewer.querySelector<HTMLButtonElement>('.evidence-temporal-segment-emphasized')?.click();
  }, [activeFieldId, activeSource, focusedSpanIds, selectedItem, viewerRef]);

  return <LazyArtifactSource artifact={activeSource.artifact}
    emphasizedSpanIds={focusedSpanIds.length ? focusedSpanIds : selectedSpanIds(activeSource, activeFieldId)} />;
}

function CitationTab({ source, activeField, selected, onChoose }: {
  source: ReturnType<typeof reviewCitationSources>[number]; activeField: ReviewBundleField | undefined; selected: boolean; onChoose(): void;
}) {
  const location = tabLocation(source, activeField);
  return <button type="button" role="tab" className={styles.tab} aria-selected={selected} title={source.title} onClick={onChoose}>
    <span className={styles.tabKind}>{source.kind}</span><span className={styles.tabName}>{source.title}</span>
    {location && <span className={styles.tabLocation}>{location}</span>}
  </button>;
}

function tabLocation(source: ReturnType<typeof reviewCitationSources>[number], field: ReviewBundleField | undefined) {
  return sourceLocation(source, field?.id);
}

function citationKey(bundle: ReviewBundle): string {
  return JSON.stringify(bundle.fields.map(({ id: fieldId, runId, rowId, columnId }) => ({ fieldId, runId, rowId, columnId })));
}

async function loadCitationPayloads(targets: readonly CitationTarget[], api: ReturnType<typeof useWorkspaceStores>['projectApi']): Promise<ReviewCitationPayload[]> {
  const perField = await Promise.all(targets.map(async (target) => {
    const cell = await api.getCellEvidence(target.rowId, target.columnId);
    if (!currentEvidenceMatchesField(cell, target)) return [];
    const links = cell.links.filter((link) => link.status === 'active');
    const viewers = await Promise.all(links.map(async (link) => {
      try { return await api.getEvidenceViewer(link.stable_id); } catch { return null; }
    }));
    return viewers.flatMap((payload) => payload === null ? [] : [{ fieldId: target.fieldId, payload }]);
  }));
  return perField.flat();
}

function selectedPage(source: ReturnType<typeof reviewCitationSources>[number], fieldId: string, focusedSpanIds: readonly string[]): number | null {
  const spans = source.members.filter((item) => item.fieldId === fieldId).flatMap((item) => item.spans);
  const span = spans.find((item) => focusedSpanIds.includes(item.stable_id)) ?? spans[0];
  if (!span || span.selector === null || typeof span.selector !== 'object' || Array.isArray(span.selector)) return null;
  const raw = (span.selector as Record<string, unknown>).page_start;
  const page = typeof raw === 'number' ? raw : Number(raw);
  return Number.isFinite(page) ? page : null;
}

function selectedSpanIds(source: ReturnType<typeof reviewCitationSources>[number], fieldId: string | undefined): string[] {
  return source.members.filter((member) => member.fieldId === fieldId)
    .flatMap((member) => member.spans.map((span) => span.stable_id));
}

function RowFieldsDrawer({ entries, bundle, onClose }: { entries: Array<[string, CellValue]>; bundle: ReviewBundle | undefined; onClose(): void; }) {
  const columns = (bundle?.sources ?? []).map((source) => ({ id: source.columnId, name: source.columnName,
    type: source.columnType, format: source.format, semanticType: source.semanticType }));
  const row = { id: bundle?.rowId ?? '', index: bundle?.rowIndex ?? 0,
    cells: Object.fromEntries((bundle?.sources ?? []).map((source) => [source.columnId, source.value])), provenance: {} };
  return <aside className={styles.drawer} aria-label="Current input values" data-testid="review-row-fields-drawer">
    <div className={styles.drawerHeader}><h3>Inputs on row {bundle === undefined ? '—' : bundle.rowIndex + 1}</h3>
      <button type="button" className={styles.drawerClose} aria-label="Close row fields" onClick={onClose}><X size={16} /></button></div>
    <p className={styles.drawerCopy}>Current values. Saved citations show what was used for this result.</p>
    <dl className={styles.rowFields}>{entries.map(([name, value]) => {
      const column = columns.find((item) => item.name === name) ?? { id: name, name, type: 'text' as const };
      return <div key={name}><dt>{name}</dt><dd><FieldValue col={column} columns={columns} row={row} sheetId={bundle?.sheetId} value={value} /></dd></div>;
    })}</dl>
  </aside>;
}
