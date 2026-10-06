import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { MousePointer2, ScanLine, Rows3, Minus, Play, Save, X } from 'lucide-react';
import type { DocumentListPage, Row, SheetMeta } from '../../api/types';
import { documentExtractionApi, type ExtractionDocument, type ExtractionPreview, type ExtractionPreviewDocument, type ExtractedCell } from '../../api/documentExtraction';
import type { DocumentViewState } from '../../workspace/useWorkspaceChromeState';
import { DocumentReader } from '../DocumentReader';
import { documentMediaKind } from '../documentMedia';
import { useDocumentView } from '../useDocumentView';
import { LIST_ITEM_HEIGHT } from '../useWindowedRowList';
import { ExtractPageOverlay } from './ExtractPageOverlay';
import { ExtractFields } from './ExtractFields';
import { ExtractPreview } from './ExtractPreview';
import { assignUnclaimedFields, changeRegion, previewOutcome, regionInsideSpan, removeAnnotation, spanFromRegion, templateDefaults, templateIssue, textInRegion, type AnnotationTarget, type ExtractionTemplate, type ExtractTool, type PageRegion } from './types';
import styles from './ExtractView.module.css';

export interface ExtractRunRequest { source: string; template: ExtractionTemplate; repeat_group_id: string | null; sheet_name: string; row_ids?: number[] }
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
  toolbarTargetId?: string;
  onExtract?(request: ExtractRunRequest): Promise<void> | void;
  resolveRowIds?(sourceColumnId: string, limit?: number): Promise<number[] | undefined>;
  scopeLabel?: string;
}

const EMPTY_TEMPLATE: ExtractionTemplate = { reference_blob_id: '', reference_page: null, reference_fingerprint: '', fields: [], sections: [], ignore_bands: [], expand_values: false, look_every_page: true, continue_across_pages: false };
const TOOLS = [
  { id: 'select', label: 'Select', Icon: MousePointer2 },
  { id: 'key', label: 'Key / value', Icon: ScanLine },
  { id: 'repeat', label: 'Repeated section', Icon: Rows3 },
  { id: 'ignore', label: 'Ignore region', Icon: Minus },
] as const;
const errorText = (error: unknown) => error instanceof Error ? error.message : String(error);
const EXTRACTION_SOURCE_TYPES = ['file', 'image'];
const NO_TEXT_SOURCES: readonly string[] = [];

export function ExtractView(props: ExtractViewProps) {
  // A separate keyed component prevents late responses from another sheet
  // changing its template, selection or preview.
  return <ExtractWorkspace key={`${props.projectId}:${props.sheet.id}`} {...props} />;
}

function ExtractWorkspace({ toolbarTargetId, onExtract, ...props }: ExtractViewProps) {
  const { projectId, sheet, state, onChangeState } = props;
  const { activeRowId, sourceColumn, activeMedia: documentMedia, activeItem, sources, search, setSearch,
    list, items, listBodyRef, onListScroll, onListKeyDown, windowRows, startIndex, loadMore, selectDocument, recordPageCount,
  } = useDocumentView({ ...props, annotatedTextColumnIds: NO_TEXT_SOURCES, sourceColumnTypes: EXTRACTION_SOURCE_TYPES });
  const [toolbarTarget, setToolbarTarget] = useState<HTMLElement | null>(null);
  useEffect(() => {
    const frame = window.requestAnimationFrame(() => setToolbarTarget(toolbarTargetId ? document.getElementById(toolbarTargetId) : null));
    return () => window.cancelAnimationFrame(frame);
  }, [toolbarTargetId]);
  const [template, setTemplate] = useState<ExtractionTemplate>(EMPTY_TEMPLATE);
  const [templateName, setTemplateName] = useState('Extraction template');
  const [outputName, setOutputName] = useState('Extracted data');
  const [savedKey, setSavedKey] = useState<string | null>(null);
  const [savedId, setSavedId] = useState<number | undefined>();
  const [referenceRowId, setReferenceRowId] = useState<number | null>(null);
  const [loadedTemplates, setLoadedTemplates] = useState(false);
  const [tool, setTool] = useState<ExtractTool>('key');
  const [pending, setPending] = useState<PageRegion | null>(null);
  const [selected, setSelected] = useState<AnnotationTarget | null>(null);
  const [repeatGroupId, setRepeatGroupId] = useState<string | null>(null);
  const [geometry, setGeometry] = useState<{ key: string; value: ExtractionDocument | null; error: string | null }>({ key: '', value: null, error: null });
  const [reference, setReference] = useState<ExtractionDocument | null>(null);
  const [preview, setPreview] = useState<ExtractionPreview | null>(null);
  const [previewOpen, setPreviewOpen] = useState(false);
  const [busy, setBusy] = useState<'save' | 'preview' | 'run' | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [focusRegions, setFocusRegions] = useState<PageRegion[]>([]);
  const [readerPage, setReaderPage] = useState(1);
  const [jumpRow, setJumpRow] = useState<ExtractionPreviewDocument | null>(null);
  const previewController = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const restoreContext = useRef({ state, onChangeState, columns: sheet.columns });
  useEffect(() => { restoreContext.current = { state, onChangeState, columns: sheet.columns }; }, [state, onChangeState, sheet.columns]);
  const edits = useRef(0);
  const currentRowId = jumpRow ? String(jumpRow.row_id) : activeRowId;
  const geometryKey = `${sourceColumn?.id ?? ''}:${currentRowId ?? ''}`;
  const currentGeometry = geometry.key === geometryKey ? geometry.value : null;
  const geometryError = geometry.key === geometryKey ? geometry.error : null;
  const activeMedia = jumpRow ? { url: `/api/projects/${encodeURIComponent(projectId)}/blobs/${encodeURIComponent(jumpRow.blob_id)}`, label: jumpRow.filename,
    filename: jumpRow.filename, mime: currentGeometry?.mime, blobHash: jumpRow.blob_id } : documentMedia;
  const activeKind = documentMediaKind(activeMedia, sourceColumn?.type ?? 'file');
  const pageImages = useMemo(() => currentGeometry && activeKind === 'pdf' ? currentGeometry.document.pages.map((page) => ({
    page: page.page, width: page.width, height: page.height,
    url: `/api/projects/${encodeURIComponent(projectId)}/blobs/${encodeURIComponent(currentGeometry.blob_id)}/pages/${page.page}/image`,
  })) : undefined, [currentGeometry, activeKind, projectId]);
  const geometryMatchesReference = currentGeometry?.blob_id === template.reference_blob_id
    && (currentGeometry.reference_page ?? null) === template.reference_page;
  const referenceDocument = geometryMatchesReference ? currentGeometry : reference;
  const isReference = !template.reference_blob_id || geometryMatchesReference;
  const templateKey = JSON.stringify({ template, repeatGroupId, templateName });

  useEffect(() => {
    const controller = new AbortController();
    void documentExtractionApi.templates(projectId, sheet.id, controller.signal).then(({ templates }) => {
      if (controller.signal.aborted) return;
      const saved = templates[0];
      if (saved && edits.current === 0) {
        const restored = templateDefaults(saved.spec.params.template);
        setTemplate(restored);
        setSavedId(saved.id);
        setReferenceRowId(saved.reference_row_id);
        const context = restoreContext.current;
        const savedSource = context.columns.find((column) => column.name === saved.spec.params.source && EXTRACTION_SOURCE_TYPES.includes(column.type));
        if (savedSource && String(savedSource.id) !== context.state.sourceColumnId) context.onChangeState({ ...context.state, sourceColumnId: String(savedSource.id) });
        setTemplateName(saved.name);
        const group = saved.spec.params.repeat_group_id ?? null;
        setRepeatGroupId(group);
        setSavedKey(JSON.stringify({ template: restored, repeatGroupId: group, templateName: saved.name }));
      }
      setLoadedTemplates(true);
    }).catch((cause) => { if (!controller.signal.aborted) { setError(errorText(cause)); setLoadedTemplates(true); } });
    return () => controller.abort();
  }, [projectId, sheet.id]);

  useEffect(() => {
    if (referenceRowId === null || !sourceColumn) return;
    const controller = new AbortController();
    void documentExtractionApi.document(projectId, sheet.id, String(sourceColumn.id), String(referenceRowId), controller.signal)
      .then((value) => { if (!controller.signal.aborted) {
        setReference(value);
        setJumpRow({ row_id: value.row_id, blob_id: value.blob_id, filename: value.filename, result: { records: [], diagnostics: [], outcome: 'extracted' } });
      } }).catch((cause) => { if (!controller.signal.aborted) setError(errorText(cause)); });
    return () => controller.abort();
  }, [projectId, sheet.id, sourceColumn, referenceRowId]);

  useEffect(() => {
    if (!currentRowId || !sourceColumn) return;
    const controller = new AbortController();
    void documentExtractionApi.document(projectId, sheet.id, String(sourceColumn.id), currentRowId, controller.signal)
      .then((value) => { if (!controller.signal.aborted) {
        setGeometry({ key: geometryKey, value, error: null });
        setReaderPage(value.reference_page ?? 1);
      } })
      .catch((cause) => { if (!controller.signal.aborted) setGeometry({ key: geometryKey, value: null, error: errorText(cause) }); });
    return () => controller.abort();
  }, [projectId, sheet.id, sourceColumn, currentRowId, geometryKey]);

  useEffect(() => { mounted.current = true; return () => { mounted.current = false; previewController.current?.abort(); }; }, []);

  const update = useCallback((next: ExtractionTemplate) => {
    edits.current += 1;
    previewController.current?.abort();
    setBusy((current) => current === 'preview' ? null : current);
    setTemplate(next);
    setPreview(null);
    setFocusRegions([]);
    setError(null);
  }, []);
  const remove = (target: AnnotationTarget) => {
    update(removeAnnotation(template, target));
    setSelected(null);
    if ((target.kind === 'first' || target.kind === 'rest') && target.id === repeatGroupId) setRepeatGroupId(null);
  };
  const select = (target: AnnotationTarget) => {
    setSelected(target);
    setTool('select');
    setFocusRegions([]);
    if (!isReference && reference) setJumpRow({ row_id: reference.row_id, blob_id: reference.blob_id, filename: reference.filename,
      result: { records: [], diagnostics: [], outcome: 'extracted' } });
    const field = template.fields.find((item) => item.id === target.id);
    if (field && (target.kind === 'key' || target.kind === 'value')) setReaderPage(field[target.kind].page);
    const section = template.sections.find((item) => item.id === target.id);
    if (section && (target.kind === 'first' || target.kind === 'rest')) setReaderPage(section[target.kind].start.page);
  };
  const draw = (region: PageRegion) => {
    if (!currentGeometry || !loadedTemplates || !isReference) return;
    const base = template.reference_blob_id ? template : { ...template, reference_blob_id: currentGeometry.blob_id,
      reference_page: currentGeometry.reference_page ?? null,
      reference_fingerprint: currentGeometry.document.source_fingerprint };
    setReference(currentGeometry);
    if (referenceRowId === null) setReferenceRowId(currentGeometry.row_id);
    if (tool === 'ignore') { update({ ...base, ignore_bands: [...base.ignore_bands, { box: region.box }] }); return; }
    if (!pending) { edits.current += 1; setPending(region); return; }
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
      update({ ...base, fields: [...base.fields, { id, name, key: pending, value: region, section_id: section?.id ?? null }] });
      setSelected({ kind: 'value', id });
    }
    setPending(null);
  };
  const request = () => ({ sheet_id: Number(sheet.id), source: sourceColumn?.name ?? '', template, repeat_group_id: repeatGroupId });
  const validationIssue = templateIssue(template, repeatGroupId);
  const valid = validationIssue === null;
  const previewTemplate = { ...template, fields: template.fields.filter((field) => !field.section_id || field.section_id === repeatGroupId) };
  const runPreview = async () => {
    if (!valid || !sourceColumn) return;
    previewController.current?.abort();
    const controller = new AbortController();
    previewController.current = controller;
    setBusy('preview'); setError(null); setPreviewOpen(true);
    try {
      const rowIds = await props.resolveRowIds?.(String(sourceColumn.id), 12);
      if (controller.signal.aborted) return;
      const value = await documentExtractionApi.preview(projectId, { ...request(), ...(rowIds === undefined ? {} : { row_ids: rowIds }) }, controller.signal);
      if (!controller.signal.aborted) setPreview(value);
    } catch (cause) { if (!controller.signal.aborted) setError(errorText(cause)); }
    finally { if (!controller.signal.aborted) setBusy(null); }
  };
  const choosePreview = (document: ExtractionPreviewDocument, cell: ExtractedCell | null) => {
    setJumpRow(document);
    props.onDocumentFocus(String(document.row_id));
    setReaderPage(cell?.regions[0]?.page ?? 1);
    setFocusRegions(cell?.regions ?? []);
    setTool('select');
    setPending(null);
  };
  const toolbar = <div className={styles.toolbar} aria-label="PDF extraction tools">
    <div className={styles.toolbarGroup}><div className={styles.tools}>{TOOLS.map(({ id, label, Icon }) => <button key={id} type="button"
      className={tool === id ? styles.activeTool : ''} aria-pressed={tool === id} disabled={!isReference || !currentGeometry || !loadedTemplates}
      onClick={() => { setTool(id); setPending(null); }}><Icon size={18} /><span>{label}</span></button>)}</div><small>Annotate</small></div>
    <div className={styles.toolbarGroup}><div className={styles.options}>
      <label><input type="checkbox" aria-describedby="extract-area-help" checked={template.expand_values} onChange={(event) => update({ ...template, expand_values: event.target.checked })} />Expand value areas</label>
      <label><input type="checkbox" checked={template.look_every_page} onChange={(event) => update({ ...template, look_every_page: event.target.checked })} />Look on every page</label>
      <label><input type="checkbox" checked={template.continue_across_pages} onChange={(event) => update({ ...template, continue_across_pages: event.target.checked })} />Continue across pages</label>
      <label>Rows <select className="form-input" aria-label="Result rows" value={repeatGroupId ?? ''} onChange={(event) => { update(template); setRepeatGroupId(event.target.value || null); }}>
        {template.sections.length === 0 ? <option value="">One per document</option> : <option value="" disabled>Choose repeated section</option>}
        {template.sections.map((section) => <option key={section.id} value={section.id}>One per {section.name}</option>)}
      </select></label>
    </div><small>Template</small></div>
    <div className={styles.toolbarGroup}><div className={styles.runButtons}>
      <label>New sheet <input className="form-input" aria-label="Result sheet name" value={outputName} onChange={(event) => setOutputName(event.target.value)} /></label>
      <button type="button" className="mini-btn" disabled={!valid || busy !== null} onClick={() => void runPreview()}><Play size={13} />{busy === 'preview' ? 'Previewing…' : 'Preview on 12 documents'}</button>
      <button type="button" className="mini-btn" disabled={!valid || busy !== null || !onExtract || !outputName.trim()} onClick={async () => {
        const snapshot = request();
        const editVersion = edits.current;
        setBusy('run'); setError(null);
        try { const rowIds = await props.resolveRowIds?.(String(sourceColumn?.id));
          if (!mounted.current) return;
          if (editVersion !== edits.current) { setError('The template changed. Start extraction again with your updated fields.'); return; }
          await onExtract?.({ ...snapshot, sheet_name: outputName.trim(), ...(rowIds === undefined ? {} : { row_ids: rowIds }) }); }
        catch (cause) { if (mounted.current) setError(errorText(cause)); }
        finally { if (mounted.current) setBusy(null); }
      }}>{busy === 'run' ? 'Starting extraction…' : `Extract ${props.scopeLabel ?? 'all'} → new sheet`}</button>
    </div><small>Run</small></div>
  </div>;
  return <section className={styles.workspace} data-testid="extract-view" aria-label="Extract structured data"
    onKeyDown={(event) => {
      if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey
        || (event.target instanceof HTMLElement && event.target.closest('input, textarea, select, [contenteditable]:not([contenteditable="false"])'))) return;
      if (event.key === 'Escape' && pending) { event.preventDefault(); event.stopPropagation(); setPending(null); return; }
      if (!isReference || !currentGeometry || !loadedTemplates) return;
      const shortcuts: Record<string, ExtractTool> = { k: 'key', r: 'repeat', i: 'ignore', v: 'select' };
      const nextTool = shortcuts[event.key.toLowerCase()];
      if (nextTool) { event.preventDefault(); event.stopPropagation(); setTool(nextTool); setPending(null); }
      if ((event.key === 'Delete' || event.key === 'Backspace') && selected) { event.preventDefault(); event.stopPropagation(); remove(selected); }
    }}>
    {toolbarTarget ? createPortal(toolbar, toolbarTarget) : !toolbarTargetId ? toolbar : null}
    <div className={styles.saveBar}>
      <input className="form-input" aria-label="Template name" value={templateName} onChange={(event) => setTemplateName(event.target.value)} />
      <span className="muted" role="status">{!loadedTemplates ? 'Loading template…' : savedKey === templateKey ? 'Template saved' : 'Unsaved changes'}</span>
      <button type="button" className="mini-btn" disabled={!valid || busy !== null || !templateName.trim()} onClick={async () => {
        const key = templateKey; const editVersion = edits.current; setBusy('save'); setError(null);
        try { const saved = await documentExtractionApi.save(projectId, { ...request(), name: templateName, id: savedId, reference_row_id: referenceRowId ?? Number(currentRowId) });
          if (mounted.current && editVersion === edits.current) { setSavedKey(key); setSavedId(saved.id); } }
        catch (cause) { if (mounted.current) setError(errorText(cause)); }
        finally { if (mounted.current) setBusy(null); }
      }}><Save size={13} />Save template</button>
      {sources.filter((entry) => entry.column.type === 'file' || entry.column.type === 'image').length > 1 && <label>Source <select className="form-input" value={sourceColumn?.id ?? ''} onChange={(event) => {
        onChangeState({ ...state, sourceColumnId: event.target.value, activeRowId: null });
        setJumpRow(null); setPending(null); update(EMPTY_TEMPLATE); setReference(null); setReferenceRowId(null); setSavedId(undefined); setSavedKey(null); setRepeatGroupId(null);
      }}>{sources.filter((entry) => entry.column.type === 'file' || entry.column.type === 'image').map(({ column }) => <option key={column.id} value={column.id}>{column.name}</option>)}</select></label>}
    </div>
    {(error || geometryError) && <div className={styles.error} role="alert">{error || geometryError}</div>}
    {validationIssue && <div className={styles.validation} role="status">{validationIssue}</div>}
    <div className={styles.body}>
      <aside className={styles.rail} aria-label="Documents">
        <input className="form-input" type="search" placeholder="Search documents…" aria-label="Search documents" value={search} onChange={(event) => setSearch(event.target.value)} />
        <small>{preview ? `Preview sample · ${preview.documents.length} documents` : `${items.length} loaded`}</small>
        <div className="document-list-body drawer-body" ref={listBodyRef} onScroll={onListScroll} onKeyDown={onListKeyDown} tabIndex={0} role="listbox" aria-label="Document list">
          {list.error && <p role="alert">{list.error}</p>}
          <div style={{ position: 'relative', height: `${items.length * LIST_ITEM_HEIGHT}px` }}>
            {windowRows.map((item, offset) => {
              const result = preview?.documents.find((doc) => String(doc.row_id) === item.rowId)?.result;
              const outcome = result ? previewOutcome(result) : null;
              return <button key={item.rowId} type="button" role="option" aria-selected={currentRowId === item.rowId}
                className={`document-list-item${currentRowId === item.rowId ? ' active' : ''}`} style={{ position: 'absolute', top: `${(startIndex + offset) * LIST_ITEM_HEIGHT}px`, height: LIST_ITEM_HEIGHT }}
                onClick={() => { setJumpRow(null); selectDocument(item.rowId); setReaderPage(1); setFocusRegions([]); setPending(null); }}>
                <span className="document-list-item-title">{outcome && <span className={outcome.warning ? styles.warning : result?.outcome === 'zero_records' ? styles.empty : styles.success}>{result?.outcome === 'zero_records' ? '○ ' : '● '}</span>}{item.title}</span>
                <span className="document-list-item-secondary muted">{outcome?.text ?? item.sourceLabel ?? 'Document'}</span>
              </button>;
            })}
          </div>
          {list.pages.at(-1)?.nextCursor && <button type="button" className="mini-btn" disabled={list.loading} onClick={() => void loadMore()}>Load more</button>}
        </div>
      </aside>
      <div className={styles.center}>
        <div className={styles.instruction} role="status">
          {!isReference ? 'Preview document · annotations are read-only' : pending ? tool === 'repeat' ? 'Now mark all remaining records together.' : 'Now draw the value box. An empty value is valid.' : tool === 'key' ? 'Draw a box around a key, then its value.' : tool === 'repeat' ? 'Mark the first record with a full-width band.' : tool === 'ignore' ? 'Mark a header or footer to ignore on every page.' : 'Select a box to move, resize or delete it.'}
          {pending && <button type="button" className="icon-btn" aria-label="Cancel drawing" onClick={() => setPending(null)}><X size={13} /></button>}
          {!isReference && reference && <button type="button" className="mini-btn" onClick={() => {
            setJumpRow({ row_id: reference.row_id, blob_id: reference.blob_id, filename: reference.filename, result: { records: [], diagnostics: [], outcome: 'extracted' } }); setReaderPage(1); setFocusRegions([]);
          }}>Back to example</button>}
        </div>
        <p id="extract-area-help" className={styles.areaHelp}>{template.expand_values
          ? 'Expanded areas follow matched field boundaries. Check Preview for alignment warnings.'
          : 'Fixed areas read only the selected space. Draw the full possible value area, or enable Expand value areas for variable-length text.'}</p>
        <DocumentReader key={`${currentRowId}:${readerPage}`} media={activeMedia} mediaKind={activeKind} title={jumpRow?.filename ?? activeItem?.title ?? 'No document selected'}
          layout="single" fit="width" videoFit="full" onVideoFitChange={() => undefined} textLayer={false}
          onPageCount={recordPageCount} rowKey={currentRowId ?? ''} onOpenDetail={() => undefined} canOpenDetail={false}
          optionsOpen={false} onToggleOptions={() => undefined} optionsPopover={null} selectionCount={0} initialPage={readerPage}
          pageImages={pageImages}
          renderPageOverlay={(page) => currentGeometry && (activeKind === 'pdf' ? pageImages !== undefined : activeKind === 'image') ? <ExtractPageOverlay page={page} template={template}
            tool={tool} selected={selected} muted={!isReference} focusRegions={focusRegions} pending={pending}
            onSelect={select} onDraw={draw} onChange={(target, region) => update(changeRegion(template, target, region))} onDelete={remove} /> : null} />
        <button type="button" className={styles.previewToggle} aria-expanded={previewOpen} onClick={() => setPreviewOpen((open) => !open)}>
          <strong>Preview</strong><span>{preview ? `${preview.documents.length}-document sample · ${preview.documents.reduce((count, doc) => count + doc.result.records.length, 0)} rows` : 'Add fields, then preview on a sample of documents'}</span><span>{previewOpen ? '▾' : '▴'}</span>
        </button>
        {previewOpen && <div className={styles.previewPane}>{busy === 'preview' ? <p role="status">Extracting sample documents…</p> : preview ? <ExtractPreview template={previewTemplate} preview={preview} onSelect={choosePreview} /> : <p>Run Preview to inspect extracted values and their source regions.</p>}</div>}
      </div>
      <ExtractFields template={template} reference={referenceDocument?.document ?? null} selected={selected} preview={preview} onChange={update} onSelect={select} onDelete={remove} />
    </div>
  </section>;
}
