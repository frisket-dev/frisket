import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Check, ChevronDown, ChevronUp, CircleAlert, CircleHelp, LoaderCircle, Minus, MousePointer2, Play, Rows3, ScanLine, Search, Settings2, Square, X } from 'lucide-react';
import { MenuPop } from '../../components/MenuPop';
import { PanelSelect } from '../../components/PanelSelect';
import { useAnchoredPosition } from '../../hooks/useAnchoredPosition';
import { useNativePopover } from '../../hooks/useNativePopover';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import type { DocumentListPage, Row, SheetMeta } from '../../api/types';
import { documentExtractionApi, type ExtractionDocument, type ExtractionPreview, type ExtractionPreviewDocument, type ExtractionScope, type ExtractionScopeFilter } from '../../api/documentExtraction';
import type { DocumentViewState } from '../../workspace/useWorkspaceChromeState';
import { DocumentReader } from '../DocumentReader';
import { documentMediaKind } from '../documentMedia';
import { useDocumentView } from '../useDocumentView';
import { LIST_ITEM_HEIGHT } from '../useWindowedRowList';
import { ExtractPageOverlay } from './ExtractPageOverlay';
import { ExtractFields } from './ExtractFields';
import { ExtractPreview } from './ExtractPreview';
import { ExtractSheetNameDialog } from './ExtractSheetNameDialog';
import { ExtractToolHelp } from './ExtractToolHelp';
import { assignUnclaimedFields, changeRegion, previewOutcome, regionInsideSpan, removeAnnotation, spanFromRegion, templateDefaults, templateIssue, textInRegion, type AnnotationTarget, type ExtractionTemplate, type ExtractTool, type PageRegion } from './types';
import { EMPTY_EXTRACTION_DRAFT, useExtractionLayouts } from './useExtractionLayouts';
import styles from './ExtractView.module.css';

export interface ExtractRunRequest { source: string; template: ExtractionTemplate; repeat_group_id: string | null; sheet_name: string; layout_id: number; extraction_scope: ExtractionScope }
export interface ExtractViewProps {
  projectId: string;
  sheet: SheetMeta;
  state: DocumentViewState;
  onChangeState(next: DocumentViewState): void;
  onDocumentFocus(rowId: string): void;
  queryDocuments(args: { sourceColumnId: string; titleColumnId: string | null; query: string; cursor?: string; anchorRowId?: string; limit: number }): Promise<DocumentListPage>;
  hydrateRow(rowId: string, columnIds: string[]): Promise<Row | null>;
  orderKey: string;
  titleColumnOrder?: readonly string[];
  onExtract?(request: ExtractRunRequest): Promise<void> | void;
  filterScope?: ExtractionScopeFilter;
  refreshKey?: unknown;
  extractionRunning?: boolean;
}

const TOOLS = [
  { id: 'select', label: 'Select', Icon: MousePointer2 },
  { id: 'key', label: 'Key / value', Icon: ScanLine },
  { id: 'value', label: 'Value only', Icon: Square },
  { id: 'repeat', label: 'Repeated section', Icon: Rows3 },
  { id: 'ignore', label: 'Ignore region', Icon: Minus },
] as const;
interface PreviewInspection {
  document: ExtractionPreviewDocument;
  recordIndex: number | null;
  fieldId: string | null;
}
const errorText = (error: unknown) => error instanceof Error ? error.message : String(error);
const EXTRACTION_SOURCE_TYPES = ['file', 'image'];
const NO_TEXT_SOURCES: readonly string[] = [];
function useExtractionPageImages(geometry: ExtractionDocument | null, kind: ReturnType<typeof documentMediaKind>, projectId: string) {
  // PdfReader uses this array as a loading dependency; page-count updates must not recreate it.
  return useMemo(() => geometry && kind === 'pdf' ? geometry.document.pages.map((page) => ({
    page: page.page, width: page.width, height: page.height,
    url: `/api/projects/${encodeURIComponent(projectId)}/blobs/${encodeURIComponent(geometry.blob_id)}/pages/${page.page}/image`,
  })) : undefined, [geometry, kind, projectId]);
}
function matchesReference(document: ExtractionDocument | null, blobId: string, page: number | null, fingerprint: string) {
  return document?.blob_id === blobId
    && (document.reference_page ?? null) === page
    && document.document.source_fingerprint === fingerprint;
}

export function ExtractView(props: ExtractViewProps) {
  // A separate keyed component prevents late responses from another sheet
  // changing its template, selection or preview.
  return <ExtractWorkspace key={`${props.projectId}:${props.sheet.id}`} {...props} />;
}

function ExtractWorkspace(props: ExtractViewProps) {
  const { extractionLayouts } = useWorkspaceStores();
  const browse = useDocumentView({ ...props, annotatedTextColumnIds: NO_TEXT_SOURCES, sourceColumnTypes: EXTRACTION_SOURCE_TYPES });
  const persistence = useExtractionLayouts(extractionLayouts, props.sheet.id, browse.sourceColumn?.name ?? '', props.refreshKey);
  return <ExtractEditor key={`${browse.sourceColumn?.id ?? ''}:${persistence.layout?.id ?? ''}`} {...props} browse={browse} persistence={persistence} />;
}

function ExtractEditor({ onExtract, persistence, browse, ...props }: ExtractViewProps & {
  persistence: ReturnType<typeof useExtractionLayouts>; browse: ReturnType<typeof useDocumentView>;
}) {
  const { projectId, sheet, state, onChangeState } = props;
  const { activeRowId, sourceColumn, activeMedia: documentMedia, activeItem, sources, search, setSearch,
    list, items, listBodyRef, onListScroll, onListKeyDown, windowRows, startIndex, loadMore, selectDocument, recordPageCount,
  } = browse;
  const { layout, change: changeLayout } = persistence;
  const template = useMemo(() => templateDefaults(layout?.draft ?? EMPTY_EXTRACTION_DRAFT), [layout?.draft]);
  const repeatGroupId = layout?.repeat_group_id ?? null;
  const referenceRowId = layout?.reference_row_id ?? null;
  const pending = layout?.draft.pending?.region ?? null;
  const loadedTemplates = !persistence.loading && layout !== null;
  const [tool, setTool] = useState<ExtractTool>(layout?.draft.pending?.tool ?? 'key');
  const [selected, setSelected] = useState<AnnotationTarget | null>(null);
  const [geometry, setGeometry] = useState<{ key: string; value: ExtractionDocument | null; error: string | null }>({ key: '', value: null, error: null });
  const [reference, setReference] = useState<ExtractionDocument | null>(null);
  const [referenceVerification, setReferenceVerification] = useState<{ key: string; status: 'ready' | 'stale'; error?: string } | null>(null);
  const [preview, setPreview] = useState<ExtractionPreview | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [busy, setBusy] = useState<'preview' | 'run' | null>(null);
  const unavailable = !loadedTemplates || persistence.switching || busy === 'run';
  const [error, setError] = useState<string | null>(null);
  const [readerPage, setReaderPage] = useState(1);
  const [readerJump, setReaderJump] = useState({ rowId: null as string | null, page: 1, revision: 0 });
  const [jumpRow, setJumpRow] = useState<ExtractionPreviewDocument | null>(null);
  const [inspection, setInspection] = useState<PreviewInspection | null>(null);
  const [nameDialogOpen, setNameDialogOpen] = useState(false);
  const [optionsOpen, setOptionsOpen] = useState(false);
  const optionsTrigger = useRef<HTMLButtonElement>(null);
  const optionsPopoverRef = useRef<HTMLDivElement>(null);
  const rowsMenuRef = useRef<HTMLDivElement>(null);
  const optionsMenuRefs = useMemo(() => [rowsMenuRef], []);
  useNativePopover(optionsPopoverRef, () => setOptionsOpen(false), { enabled: optionsOpen,
    ignoreSelector: '[data-testid="extract-options-button"]', extraRefs: optionsMenuRefs, focusRestore: true });
  const optionsPosition = useAnchoredPosition(optionsTrigger, { enabled: optionsOpen, width: 270, gap: 4 });
  const [helpOpen, setHelpOpen] = useState(false);
  const helpTrigger = useRef<HTMLButtonElement>(null);
  const helpPopoverRef = useRef<HTMLDivElement>(null);
  useNativePopover(helpPopoverRef, () => setHelpOpen(false), { enabled: helpOpen,
    ignoreSelector: '[data-testid="extract-tool-help-button"]', focusRestore: true });
  const helpPosition = useAnchoredPosition(helpTrigger, { enabled: helpOpen, width: 300, gap: 4 });
  const previewController = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const edits = useRef(0);
  const currentRowId = jumpRow ? String(jumpRow.row_id) : activeRowId;
  const jumpToPage = useCallback((rowId: string, page: number) => {
    setReaderPage(page);
    setReaderJump((current) => ({ rowId, page, revision: current.revision + 1 }));
  }, [setReaderJump, setReaderPage]);
  const geometryKey = `${sourceColumn?.id ?? ''}:${currentRowId ?? ''}`;
  const currentGeometry = geometry.key === geometryKey ? geometry.value : null;
  const geometryError = geometry.key === geometryKey ? geometry.error : null;
  const activeMedia = jumpRow ? { url: `/api/projects/${encodeURIComponent(projectId)}/blobs/${encodeURIComponent(jumpRow.blob_id)}`, label: jumpRow.filename,
    filename: jumpRow.filename, mime: currentGeometry?.mime, blobHash: jumpRow.blob_id } : documentMedia;
  const activeKind = documentMediaKind(activeMedia, sourceColumn?.type ?? 'file');
  const pageImages = useExtractionPageImages(currentGeometry, activeKind, projectId);
  const referenceBlobId = template.reference_blob_id;
  const referencePage = template.reference_page;
  const referenceFingerprint = template.reference_fingerprint;
  const referenceKey = referenceRowId === null ? null
    : JSON.stringify([projectId, sheet.id, sourceColumn?.id, layout?.id, referenceRowId, referenceBlobId, referencePage, referenceFingerprint]);
  const referenceStatus = referenceKey === null ? 'ready'
    : referenceVerification?.key === referenceKey ? referenceVerification.status : 'loading';
  const referenceError = referenceVerification?.key === referenceKey ? referenceVerification.error : null;
  const geometryMatchesReference = matchesReference(currentGeometry, referenceBlobId, referencePage, referenceFingerprint);
  const referenceDocument = geometryMatchesReference ? currentGeometry : reference;
  const isReference = !template.reference_blob_id || geometryMatchesReference;
  const templateReadOnly = unavailable || referenceStatus !== 'ready';
  const editingReference = isReference && inspection === null;
  const editorReadOnly = templateReadOnly || !editingReference;
  const setPending = useCallback((region: PageRegion | null) => changeLayout((current) => ({ draft: { ...current.draft,
    pending: region ? { tool: tool === 'repeat' ? 'repeat' : 'key', region } : null } })), [changeLayout, tool]);
  const setRepeatGroupId = (id: string | null) => changeLayout({ repeat_group_id: id });
  const [scopeChoice, setScopeChoice] = useState<{ layoutId: number; kind: ExtractionScope['kind'] } | null>(layout
    ? { layoutId: layout.id, kind: layout.has_applied ? 'layout' : 'all' } : null);
  const scopeKind = scopeChoice && scopeChoice.layoutId === layout?.id ? scopeChoice.kind : layout?.has_applied ? 'layout' : 'all';
  const filterScope: ExtractionScopeFilter = { filter: props.filterScope?.filter,
    parent_row_id: props.filterScope?.parent_row_id, scope_row_ids: props.filterScope?.scope_row_ids };
  const scope: ExtractionScope = { kind: scopeKind, ...(scopeKind === 'filter' ? filterScope : {}),
    ...(scopeKind === 'this' ? { row_id: currentRowId ? Number(currentRowId) : null } : {}),
    ...(scopeKind === 'layout' ? { layout_id: layout?.id } : {}) };
  const countsKey = JSON.stringify({ sheetId: sheet.id, source: sourceColumn?.name, layoutId: layout?.id, currentRowId, filterScope });
  const [countResult, setCountResult] = useState<{ key: string; value: Awaited<ReturnType<typeof documentExtractionApi.counts>> } | null>(null);
  const counts = countResult?.key === countsKey ? countResult.value : null;
  const sourceId = sourceColumn?.id;

  useEffect(() => {
    if (!sourceColumn || !layout) return;
    const controller = new AbortController();
    void documentExtractionApi.counts(projectId, { sheet_id: Number(sheet.id), source: sourceColumn.name,
      layout_id: layout.id, row_id: currentRowId ? Number(currentRowId) : null, ...filterScope }, controller.signal)
      .then((value) => { if (!controller.signal.aborted) setCountResult({ key: countsKey, value }); })
      .catch((cause) => { if (!controller.signal.aborted) setError(errorText(cause)); });
    return () => controller.abort();
    // countsKey serializes the request inputs; layout edits do not change its scope.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [countsKey, props.refreshKey]);

  useEffect(() => {
    if (referenceRowId === null || !sourceId) return;
    const controller = new AbortController();
    void documentExtractionApi.document(projectId, sheet.id, String(sourceId), String(referenceRowId), controller.signal)
      .then((value) => { if (!controller.signal.aborted) {
        if (!matchesReference(value, referenceBlobId, referencePage, referenceFingerprint)) {
          setReference(null);
          setReferenceVerification({ key: referenceKey!, status: 'stale',
            error: 'The saved reference document has changed or is unavailable. The layout was kept unchanged.' });
          return;
        }
        setReference(value);
        setReferenceVerification({ key: referenceKey!, status: 'ready' });
        setJumpRow({ row_id: value.row_id, blob_id: value.blob_id, filename: value.filename, result: { records: [], diagnostics: [], outcome: 'extracted' } });
        jumpToPage(String(value.row_id), value.reference_page ?? 1);
      } }).catch((cause) => { if (!controller.signal.aborted) {
        setReference(null);
        setReferenceVerification({ key: referenceKey!, status: 'stale',
          error: `The saved reference document is unavailable: ${errorText(cause)}` });
      } });
    return () => controller.abort();
  }, [projectId, sheet.id, sourceId, referenceRowId, referenceKey, referenceBlobId, referencePage, referenceFingerprint, jumpToPage]);

  useEffect(() => {
    if (!currentRowId || !sourceColumn) return;
    const controller = new AbortController();
    void documentExtractionApi.document(projectId, sheet.id, String(sourceColumn.id), currentRowId, controller.signal)
      .then((value) => { if (!controller.signal.aborted) {
        setGeometry({ key: geometryKey, value, error: null });
        setReaderJump((current) => current.rowId === currentRowId ? current
          : { rowId: currentRowId, page: value.reference_page ?? 1, revision: current.revision + 1 });
      } })
      .catch((cause) => { if (!controller.signal.aborted) setGeometry({ key: geometryKey, value: null, error: errorText(cause) }); });
    return () => controller.abort();
  }, [projectId, sheet.id, sourceColumn, currentRowId, geometryKey]);

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; previewController.current?.abort(); }; }, []);

  const update = (next: ExtractionTemplate) => {
    edits.current += 1;
    previewController.current?.abort();
    setBusy((current) => current === 'preview' ? null : current);
    changeLayout((current) => ({ draft: { ...next, pending: current.draft.pending ?? null } }));
    setPreview(null);
    setInspection(null);
    setError(null);
  };
  const remove = (target: AnnotationTarget) => {
    update(removeAnnotation(template, target));
    setSelected(null);
    if ((target.kind === 'first' || target.kind === 'rest') && target.id === repeatGroupId) setRepeatGroupId(null);
  };
  const select = (target: AnnotationTarget) => {
    setSelected(target);
    setTool('select');
    setInspection(null);
    if (!isReference && reference) setJumpRow({ row_id: reference.row_id, blob_id: reference.blob_id, filename: reference.filename,
      result: { records: [], diagnostics: [], outcome: 'extracted' } });
    const field = template.fields.find((item) => item.id === target.id);
    if (field && (target.kind === 'key' || target.kind === 'value')) {
      const region = field[target.kind];
      if (region) jumpToPage(String(reference?.row_id ?? currentRowId ?? ''), region.page);
    }
    const section = template.sections.find((item) => item.id === target.id);
    if (section && (target.kind === 'first' || target.kind === 'rest')) jumpToPage(String(reference?.row_id ?? currentRowId ?? ''), section[target.kind].start.page);
  };
  const draw = (region: PageRegion) => {
    if (!currentGeometry || editorReadOnly) return;
    const base = template.reference_blob_id ? template : { ...template, reference_blob_id: currentGeometry.blob_id,
      reference_page: currentGeometry.reference_page ?? null,
      reference_fingerprint: currentGeometry.document.source_fingerprint };
    setReference(currentGeometry);
    if (referenceRowId === null) {
      changeLayout({ reference_row_id: currentGeometry.row_id });
    }
    if (tool === 'ignore') { update({ ...base, ignore_bands: [...base.ignore_bands, { box: region.box }] }); return; }
    if (tool === 'value') {
      const text = textInRegion(currentGeometry.document, region).trim();
      const nameBase = text || `Field ${base.fields.length + 1}`;
      let name = nameBase;
      for (let suffix = 2; base.fields.some((field) => field.name === name); suffix++) name = `${nameBase} ${suffix}`;
      const id = crypto.randomUUID();
      update({ ...base, fields: [...base.fields, { id, name, kind: 'value_only', key: null, value: region, section_id: null }] });
      setSelected({ kind: 'value', id });
      return;
    }
    if (!pending) { update(base); setPending(region); return; }
    if (tool === 'repeat') {
      if (region.page < pending.page || (region.page === pending.page && region.box.y0 < pending.box.y1)) {
        setError('Draw the remaining records after the first record.'); return;
      }
      const id = crypto.randomUUID();
      const section = { id, name: `Repeated section ${base.sections.length + 1}`, first: spanFromRegion(pending), rest: spanFromRegion(region) };
      update({ ...base, sections: [...base.sections, section], fields: assignUnclaimedFields(base.fields, section) });
      setRepeatGroupId(id);
      setTool('key');
    } else if (tool === 'key') {
      const text = textInRegion(currentGeometry.document, pending);
      if (!text.trim()) { setError('The key box must contain text. Draw a box around the label first.'); setPending(null); return; }
      const id = crypto.randomUUID();
      const nameBase = text.trim().replace(/[:\s]+$/, '') || `Field ${base.fields.length + 1}`;
      let name = nameBase;
      for (let suffix = 2; base.fields.some((field) => field.name === name); suffix++) name = `${nameBase} ${suffix}`;
      const section = base.sections.find((item) => item.id === repeatGroupId && regionInsideSpan(pending, item.first))
        ?? base.sections.find((item) => regionInsideSpan(pending, item.first));
      if (section && !regionInsideSpan(region, section.first)) { setError(`Both key and value must fit inside ${section.name}'s first record. Draw the value inside the band, or enlarge the band.`); return; }
      update({ ...base, fields: [...base.fields, { id, name, kind: 'key_value', key: pending, value: region, section_id: section?.id ?? null }] });
      setSelected({ kind: 'value', id });
    }
    setPending(null);
  };
  const request = () => ({ sheet_id: Number(sheet.id), source: sourceColumn?.name ?? '', template, repeat_group_id: repeatGroupId, layout_id: layout?.id, scope });
  const validationIssue = pending ? 'Finish or cancel the current drawing before extracting.' : templateIssue(template, repeatGroupId);
  const valid = validationIssue === null;
  const previewTemplate = { ...template, fields: template.fields.filter((field) => !field.section_id || field.section_id === repeatGroupId) };
  const runPreview = async () => {
    if (!valid || !sourceColumn) return;
    previewController.current?.abort();
    const controller = new AbortController();
    previewController.current = controller;
    setBusy('preview'); setError(null); setPreviewOpen(true);
    setInspection(null);
    try {
      const value = await documentExtractionApi.preview(projectId, request(), controller.signal);
      if (!controller.signal.aborted) setPreview(value);
    } catch (cause) { if (!controller.signal.aborted) setError(errorText(cause)); }
    finally { if (!controller.signal.aborted) setBusy(null); }
  };
  const choosePreview = (document: ExtractionPreviewDocument, recordIndex: number | null, fieldId: string | null) => {
    const record = recordIndex === null ? null : document.result.records[recordIndex] ?? null;
    const cell = fieldId ? record?.cells[fieldId] : null;
    const firstRegion = cell?.regions[0] ?? (record ? Object.values(record.cells).flatMap((item) => item.regions)[0] : null);
    setInspection({ document, recordIndex, fieldId });
    setJumpRow(document);
    props.onDocumentFocus(String(document.row_id));
    jumpToPage(String(document.row_id), firstRegion?.page ?? 1);
    setTool('select');
  };
  const inspectedRecord = inspection?.recordIndex === null || inspection === null
    ? null : inspection.document.result.records[inspection.recordIndex] ?? null;
  const resultFields = inspectedRecord ? template.fields.map((field) => ({ id: field.id, name: field.name,
    regions: inspectedRecord.cells[field.id]?.regions ?? [] })) : [];
  const inspectField = (fieldId: string) => {
    if (!inspection || inspection.recordIndex === null) return;
    const cell = inspection.document.result.records[inspection.recordIndex]?.cells[fieldId];
    setInspection({ ...inspection, fieldId });
    if (cell?.regions[0]) jumpToPage(String(inspection.document.row_id), cell.regions[0].page);
  };
  const backToExample = () => {
    setInspection(null);
    if (!reference) return;
    setJumpRow({ row_id: reference.row_id, blob_id: reference.blob_id, filename: reference.filename,
      result: { records: [], diagnostics: [], outcome: 'extracted' } });
    jumpToPage(String(reference.row_id), template.reference_page ?? 1);
  };
  const startExtraction = async (sheetName: string) => {
    const snapshot = request();
    const editVersion = edits.current;
    setNameDialogOpen(false);
    setBusy('run'); setError(null);
    try {
      const saved = await persistence.flush();
      if (!mounted.current) return;
      if (editVersion !== edits.current) { setError('The layout changed. Start extraction again with your updated fields.'); return; }
      await onExtract?.({ source: snapshot.source, template: snapshot.template, repeat_group_id: snapshot.repeat_group_id,
        layout_id: saved.id, extraction_scope: snapshot.scope, sheet_name: sheetName });
    } catch (cause) { if (mounted.current) setError(errorText(cause)); }
    finally { if (mounted.current) setBusy(null); }
  };
  const areaHelp = template.expand_values
    ? 'Expanded areas follow matched field boundaries. Check Preview for alignment warnings.'
    : 'Fixed areas read only the selected space. Draw the full possible value area, or expand areas for variable-length text.';
  const optionsPopover = optionsOpen && <MenuPop ref={optionsPopoverRef} style={optionsPosition
    ? { position: 'fixed', inset: 'auto', margin: 0, ...optionsPosition } : { position: 'fixed', visibility: 'hidden' }}
    className="document-options" role="dialog" aria-label="Extraction options">
      <label className="document-option-row document-option-check"><input disabled={editorReadOnly} type="checkbox" aria-describedby="extract-area-help" checked={template.expand_values} onChange={(event) => update({ ...template, expand_values: event.target.checked })} />Expand value areas</label>
      <p id="extract-area-help" className={styles.areaHelp}>{areaHelp}</p>
      <label className="document-option-row document-option-check"><input disabled={editorReadOnly} type="checkbox" checked={template.look_every_page} onChange={(event) => update({ ...template, look_every_page: event.target.checked })} />Look on every page</label>
      <label className="document-option-row document-option-check"><input disabled={editorReadOnly} type="checkbox" checked={template.continue_across_pages} onChange={(event) => update({ ...template, continue_across_pages: event.target.checked })} />Continue across pages</label>
      <label className="document-option-row">Rows <PanelSelect disabled={editorReadOnly} className="row-height-select" topLayer menuRef={rowsMenuRef} aria-label="Result rows" value={repeatGroupId ?? ''} onChange={(event) => { update(template); setRepeatGroupId(event.target.value || null); }}>
        {template.sections.length === 0 ? <option value="">One per document</option> : <option value="" disabled>Choose repeated section</option>}
        {template.sections.map((section) => <option key={section.id} value={section.id}>One per {section.name}</option>)}
      </PanelSelect></label>
    </MenuPop>;
  const helpPopover = helpOpen && <MenuPop ref={helpPopoverRef} style={helpPosition
    ? { position: 'fixed', inset: 'auto', margin: 0, ...helpPosition } : { position: 'fixed', visibility: 'hidden' }}
    className="document-options" role="dialog" aria-label="Extraction tool help"><ExtractToolHelp /></MenuPop>;
  const scopeCount = counts?.[scopeKind] ?? null;
  const saveStatus = persistence.loading ? 'Loading layouts…' : persistence.switching ? 'Changing layout…'
    : persistence.saving ? 'Saving layout…' : persistence.saveError ? 'Layout could not be saved'
      : persistence.dirty ? 'Unsaved changes' : layout ? 'Layout saved' : 'No layout available';
  const SaveStatusIcon = persistence.loading || persistence.switching || persistence.saving ? LoaderCircle
    : persistence.saveError || persistence.error ? CircleAlert : Check;
  const toolbar = <div className={styles.toolbar} aria-label="PDF extraction tools" data-testid="extract-toolbar">
    <label className={styles.toolbarLabel}><span className={styles.toolbarLabelText}>Layout</span><PanelSelect className="row-height-select" aria-label="Extraction layout" data-testid="extract-layout-selector" value={layout?.id ?? ''} disabled={unavailable || busy !== null} onValueChange={(value) => void persistence.choose(value === 'new' ? null : Number(value))}>
      {persistence.layouts.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
      <option value="new">+ New layout</option>
    </PanelSelect></label>
    <span className={`${styles.saveStatus} muted`} role="status" data-testid="extract-save-status" title={saveStatus}>
      <SaveStatusIcon size={14} aria-hidden /><span className="sr-only">{saveStatus}</span></span>
    {persistence.saveError && layout && <button type="button" className="mini-btn" disabled={persistence.saving} onClick={() => void persistence.flush().catch(() => undefined)}>Retry saving</button>}
    {persistence.error && !layout && <button type="button" className="mini-btn" disabled={persistence.loading} onClick={persistence.retryLoad}>Retry loading layouts</button>}
    {sources.length > 1 && <label className={styles.toolbarLabel}><span className={styles.toolbarLabelText}>Source</span><PanelSelect className="row-height-select" aria-label="Document source" value={sourceColumn?.id ?? ''} disabled={busy !== null || persistence.switching} onValueChange={(value) => {
      onChangeState({ ...state, sourceColumnId: value, activeRowId: null });
      setJumpRow(null); setInspection(null); setReference(null);
    }}>{sources.map(({ column }) => <option key={column.id} value={column.id}>{column.name}</option>)}</PanelSelect></label>}
    <div className={`segmented segmented-toolbar ${styles.tools}`} role="group" aria-label="Annotation tools">{TOOLS.map(({ id, label, Icon }) => <button key={id} type="button"
      className={tool === id ? 'active' : ''} aria-label={label} title={label} aria-pressed={tool === id} disabled={editorReadOnly || !currentGeometry}
      onClick={() => { setTool(id); setPending(null); }}><Icon size={14} /><span>{label}</span></button>)}</div>
    <button type="button" ref={helpTrigger} className={`icon-btn${helpOpen ? ' active' : ''}`} data-testid="extract-tool-help-button"
      aria-label="Extraction tool help" title="Extraction tool help" aria-expanded={helpOpen} onClick={() => setHelpOpen((open) => !open)}><CircleHelp size={15} /></button>
    {helpPopover}
    {pending && <button type="button" className="mini-btn" aria-label="Cancel drawing" onClick={() => setPending(null)}><X size={13} />Cancel</button>}
    <label className={styles.toolbarLabel}><span className={styles.toolbarLabelText}>Scope</span><PanelSelect className="row-height-select" data-testid="extract-scope-selector" aria-label="Extraction scope" value={scopeKind} disabled={unavailable} onValueChange={(value) => {
      if (layout) { previewController.current?.abort(); setPreview(null); setInspection(null); setBusy(null); setScopeChoice({ layoutId: layout.id, kind: value as ExtractionScope['kind'] }); }
    }}>
      <option value="this" disabled={!currentRowId}>This document</option>
      <option value="filter">Current filter ({counts?.filter ?? '…'} documents)</option>
      <option value="all">All ({counts?.all ?? '…'} documents)</option>
      <option value="layout">Documents using this layout ({counts?.layout ?? '…'} documents)</option>
    </PanelSelect></label>
    <button type="button" ref={optionsTrigger} className={`icon-btn${optionsOpen ? ' active' : ''}`} data-testid="extract-options-button" aria-label="Extraction options" title="Extraction options" aria-expanded={optionsOpen} disabled={editorReadOnly} onClick={() => setOptionsOpen((open) => !open)}><Settings2 size={15} /></button>
    {optionsPopover}
    {inspection && <><span className={styles.toolbarStatus}>Preview result · page {readerPage}</span>
      <button type="button" className="mini-btn" onClick={backToExample}>Back to example</button></>}
    {!inspection && !isReference && <><span className={styles.toolbarStatus}>Preview document · run Preview to see matched regions</span>
      {reference && <button type="button" className="mini-btn" onClick={backToExample}>Back to example</button>}</>}
    <div className={styles.runButtons} role="group" aria-label="Extract to a new sheet">
      <button type="button" className="mini-btn" data-testid="extract-preview-button" title="Preview on up to 12 documents in this scope" disabled={!valid || templateReadOnly || busy !== null || scopeCount === null || scopeCount === 0} onClick={() => void runPreview()}><Play size={13} />{busy === 'preview' ? 'Previewing…' : 'Preview'}</button>
      <button type="button" className="mini-btn" data-testid="extract-new-sheet" disabled={!valid || templateReadOnly || busy !== null || props.extractionRunning || !onExtract || scopeCount === null || scopeCount === 0}
        onClick={() => setNameDialogOpen(true)}>{busy === 'run' ? 'Starting extraction…' : props.extractionRunning ? 'Extracting…' : 'Extract to new sheet'}</button>
    </div>
  </div>;
  return <section className={styles.workspace} data-testid="extract-view" aria-label="Extract structured data"
    onKeyDown={(event) => {
      if (optionsOpen || helpOpen || nameDialogOpen || editorReadOnly || event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey
        || (event.target instanceof HTMLElement && event.target.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"])'))) return;
      if (event.key === 'Escape' && pending) { event.preventDefault(); event.stopPropagation(); setPending(null); return; }
      if (!editingReference || !currentGeometry || !loadedTemplates) return;
      const shortcuts: Record<string, ExtractTool> = { k: 'key', r: 'repeat', i: 'ignore', v: 'select' };
      const nextTool = shortcuts[event.key.toLowerCase()];
      if (nextTool) { event.preventDefault(); event.stopPropagation(); setTool(nextTool); setPending(null); }
      if ((event.key === 'Delete' || event.key === 'Backspace') && selected) { event.preventDefault(); event.stopPropagation(); remove(selected); }
    }}>
    {toolbar}
    {nameDialogOpen && <ExtractSheetNameDialog suggestedName={`${layout?.name ?? 'Layout 1'} results`}
      onCancel={() => setNameDialogOpen(false)} onConfirm={(name) => void startExtraction(name)} />}
    {(error || referenceError || persistence.saveError || persistence.error || geometryError) && <div className={styles.error} role="alert">{error || referenceError || persistence.saveError || persistence.error || geometryError}</div>}
    {validationIssue && !pending && template.fields.length > 0 && <div className={styles.validation} role="status">{validationIssue}</div>}
    <div className={styles.body}>
      <aside className="document-list" aria-label="Documents">
        <div className="document-list-search"><Search size={13} aria-hidden /><input type="search" placeholder="Search documents…" aria-label="Search documents" value={search} onChange={(event) => setSearch(event.target.value)} /></div>
        <div className="document-list-count muted mono">{preview ? `Preview sample · ${preview.documents.length} documents` : `${items.length} loaded`}</div>
        <div className="document-list-body drawer-body" ref={listBodyRef} onScroll={onListScroll} onKeyDown={onListKeyDown} tabIndex={0} role="listbox" aria-label="Document list">
          {list.error && <p role="alert">{list.error}</p>}
          <div style={{ position: 'relative', height: `${items.length * LIST_ITEM_HEIGHT}px` }}>
            {windowRows.map((item, offset) => {
              const result = preview?.documents.find((doc) => String(doc.row_id) === item.rowId)?.result;
              const outcome = result ? previewOutcome(result) : null;
              return <button key={item.rowId} type="button" role="option" aria-selected={currentRowId === item.rowId}
                className={`document-list-item${currentRowId === item.rowId ? ' active' : ''}`} style={{ position: 'absolute', top: `${(startIndex + offset) * LIST_ITEM_HEIGHT}px`, height: LIST_ITEM_HEIGHT }}
                onClick={() => { setJumpRow(null); setInspection(null); setReaderPage(1);
                  setReaderJump((current) => ({ rowId: null, page: 1, revision: current.revision + 1 })); selectDocument(item.rowId); }}>
                <span className="document-list-item-title">{outcome && <span className={outcome.warning ? styles.warning : result?.outcome === 'zero_records' ? styles.empty : styles.success}>{result?.outcome === 'zero_records' ? '○ ' : '● '}</span>}{item.title}</span>
                <span className="document-list-item-secondary muted">{outcome?.text ?? item.sourceLabel ?? 'Document'}</span>
              </button>;
            })}
          </div>
          {list.pages.at(-1)?.nextCursor && <button type="button" className="mini-btn" disabled={list.loading} onClick={() => void loadMore()}>Load more</button>}
        </div>
      </aside>
      <div className={styles.center}>
        <DocumentReader key={`${currentRowId}:${readerJump.revision}`} media={activeMedia} mediaKind={activeKind} title={jumpRow?.filename ?? activeItem?.title ?? 'No document selected'}
          layout="single" fit="width" videoFit="full" onVideoFitChange={() => undefined} textLayer={false}
          onPageCount={recordPageCount} rowKey={currentRowId ?? ''} onOpenDetail={() => undefined} canOpenDetail={false}
          optionsOpen={false} onToggleOptions={() => undefined} optionsPopover={null} selectionCount={0} initialPage={readerJump.page}
          onPageChange={setReaderPage}
          pageImages={pageImages} totalPageCount={currentGeometry?.page_count}
          renderPageOverlay={(page) => currentGeometry && (activeKind === 'pdf' ? pageImages?.some((image) => image.page === page) : activeKind === 'image') ? <ExtractPageOverlay page={page} template={template}
            tool={tool} selected={selected} muted={editorReadOnly} showTemplate={editingReference}
            resultFields={inspection && String(inspection.document.row_id) === currentRowId ? resultFields : []}
            focusedResultFieldId={inspection?.fieldId ?? null} pending={editingReference ? pending : null}
            onSelect={select} onDraw={draw} onChange={(target, region) => update(changeRegion(template, target, region))} onDelete={remove} /> : null} />
        <button type="button" className={styles.previewToggle} aria-expanded={previewOpen} onClick={() => setPreviewOpen((open) => !open)}>
          <strong>Preview</strong><span>{preview ? `${preview.documents.length}-document sample · ${preview.documents.reduce((count, doc) => count + doc.result.records.length, 0)} rows` : 'Add fields, then preview on a sample of documents'}</span>
          {previewOpen ? <ChevronDown size={14} aria-hidden /> : <ChevronUp size={14} aria-hidden />}
        </button>
        {previewOpen && <div className={styles.previewPane}>{busy === 'preview' ? <p role="status">Extracting sample documents…</p> : preview ? <ExtractPreview template={previewTemplate} preview={preview} onSelect={choosePreview} /> : <p>Run Preview to inspect extracted values and their source regions.</p>}</div>}
      </div>
      <ExtractFields disabled={editorReadOnly} template={template} reference={referenceDocument?.document ?? null} selected={selected} preview={preview}
        inspection={inspection} onInspectField={inspectField} onChange={update} onSelect={select} onDelete={remove} />
    </div>
  </section>;
}
