import { useEffect, useMemo, useRef, useState } from 'react';
import { X } from 'lucide-react';
import { type CellEvidencePayload, type CellValue, type ReviewBundle, type ReviewBundleField } from '../../api/open';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import { ArtifactSource } from '../EvidenceViewer';
import { OverflowRow } from '../OverflowRow';
import { reviewCitationSources, sourceLocation, sourcesForField, type ReviewCitationPayload } from './reviewCitationSources';
import styles from './ReviewSourcePreview.module.css';

type LoadState =
  | { key: string; fields: readonly ReviewBundleField[]; phase: 'ready'; payloads: ReviewCitationPayload[] }
  | { key: string; fields: readonly ReviewBundleField[]; phase: 'error' };

const EMPTY_FIELDS: readonly ReviewBundleField[] = [];

function currentEvidenceMatchesField(payload: CellEvidencePayload, field: ReviewBundleField): boolean {
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
}: {
  bundle: ReviewBundle | undefined;
  activeField: ReviewBundleField | undefined;
  sourceEntries: Array<[string, CellValue]>;
}) {
  const { projectApi: api } = useWorkspaceStores();
  const [state, setState] = useState<LoadState | null>(null);
  const [activeSourceId, setActiveSourceId] = useState<string | null>(null);
  const [showRowFields, setShowRowFields] = useState(false);
  const viewerRef = useRef<HTMLDivElement | null>(null);

  const fields = bundle?.fields ?? EMPTY_FIELDS;
  const bundleKey = bundle ? citationKey(bundle) : null;

  useEffect(() => {
    if (!bundleKey) return undefined;
    let current = true;
    void loadCitationPayloads(fields, api).then(
      (payloads) => { if (current) setState({ key: bundleKey, fields, phase: 'ready', payloads }); },
      () => { if (current) setState({ key: bundleKey, fields, phase: 'error' }); },
    );
    return () => { current = false; };
  }, [api, bundleKey, fields]);

  // A cloned bundle can preserve this field-array reference (for example, a
  // row-note save). A changed field array immediately hides old citations
  // until its own response lands, without deriving a large key from values.
  const currentState = state !== null && state.key === bundleKey && state.fields === fields ? state : null;
  const sources = useMemo(() => currentState?.phase === 'ready' ? reviewCitationSources(currentState.payloads) : [], [currentState]);
  const tabs = useMemo(() => sourcesForField(sources, activeField?.id), [activeField?.id, sources]);
  const activeSource = tabs.find((source) => source.id === activeSourceId) ?? tabs[0] ?? null;

  // ArtifactSource owns text/media scrolling. PDF source pages expose stable
  // anchors, so the review host can target a page without cloning its renderer.
  useEffect(() => {
    const activeFieldId = activeField?.id;
    if (!activeSource || !activeFieldId) return;
    const viewer = viewerRef.current;
    if (!viewer) return;
    const page = selectedPage(activeSource, activeFieldId);
    if (page !== null) {
      viewer.querySelector<HTMLElement>(`#evidence-page-${CSS.escape(activeSource.id)}-${page}`)
        ?.scrollIntoView({ block: 'start' });
    }
    viewer.querySelector<HTMLElement>('[data-emphasized="true"]')
      ?.scrollIntoView({ block: 'center' });
    // The canonical temporal segment button owns media readiness, seeking and
    // the autoplay-rejection guard. Reuse that behavior when a shared source
    // remains selected while the reviewer moves to a different field.
    viewer.querySelector<HTMLButtonElement>('.evidence-temporal-segment-emphasized')?.click();
  }, [activeField?.id, activeSource]);

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
            Row fields · {sourceEntries.length}
          </button>
        </div>
      </div>
      <div className={styles.viewer} ref={viewerRef}>
        {currentState === null && <p className={styles.empty}>Loading cited sources…</p>}
        {currentState?.phase === 'error' && <p className={styles.empty} role="status">Citations could not be loaded for this result.</p>}
        {currentState?.phase === 'ready' && activeSource && <ArtifactSource artifact={activeSource.artifact}
          emphasizedSpanIds={selectedSpanIds(activeSource, activeField?.id)} />}
        {currentState?.phase === 'ready' && !activeSource && <p className={styles.empty}>No cited sources are recorded for this field.</p>}
      </div>
      {showRowFields && <RowFieldsDrawer entries={sourceEntries} rowIndex={bundle?.rowIndex} onClose={() => setShowRowFields(false)} />}
    </section>
  );
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
  return bundle.id;
}

async function loadCitationPayloads(fields: readonly ReviewBundleField[], api: ReturnType<typeof useWorkspaceStores>['projectApi']): Promise<ReviewCitationPayload[]> {
  const perField = await Promise.all(fields.map(async (field) => {
    const cell = await api.getCellEvidence(field.rowId, field.columnId);
    if (!currentEvidenceMatchesField(cell, field)) return [];
    const links = cell.links.filter((link) => link.status === 'active' && link.role !== 'source_provenance');
    const viewers = await Promise.all(links.map(async (link) => {
      try { return await api.getEvidenceViewer(link.stable_id); } catch { return null; }
    }));
    return viewers.flatMap((payload) => payload === null ? [] : [{ fieldId: field.id, payload }]);
  }));
  return perField.flat();
}

function selectedPage(source: ReturnType<typeof reviewCitationSources>[number], fieldId: string): number | null {
  const span = source.members.find((item) => item.fieldId === fieldId)?.spans[0];
  if (!span || span.selector === null || typeof span.selector !== 'object' || Array.isArray(span.selector)) return null;
  const raw = (span.selector as Record<string, unknown>).page_start;
  const page = typeof raw === 'number' ? raw : Number(raw);
  return Number.isFinite(page) ? page : null;
}

function selectedSpanIds(source: ReturnType<typeof reviewCitationSources>[number], fieldId: string | undefined): string[] {
  return source.members.filter((member) => member.fieldId === fieldId)
    .flatMap((member) => member.spans.map((span) => span.stable_id));
}

function RowFieldsDrawer({ entries, rowIndex, onClose }: { entries: Array<[string, CellValue]>; rowIndex: number | undefined; onClose(): void; }) {
  return <aside className={styles.drawer} aria-label="Other row fields" data-testid="review-row-fields-drawer">
    <div className={styles.drawerHeader}><h3>Other data on row {rowIndex === undefined ? '—' : rowIndex + 1}</h3>
      <button type="button" className={styles.drawerClose} aria-label="Close row fields" onClick={onClose}><X size={16} /></button></div>
    <p className={styles.drawerCopy}>Not a record of what was sent to the model.</p>
    <dl className={styles.rowFields}>{entries.map(([name, value]) => <div key={name}><dt>{name}</dt><dd>{value === null ? '∅' : String(value)}</dd></div>)}</dl>
  </aside>;
}
