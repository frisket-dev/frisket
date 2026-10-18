import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import {
  TABLE_IMPORT_LARGE_PASTE_WARNING_BYTES,
  TableImportResponseError,
  tableImportsApi,
  type TableImportColumnPatch,
  type TableImportDestinationConfig,
  type TableImportDestinationSnapshot,
  type TableImportDraft,
  type TableImportMappingRequest,
  type TableImportRequestedSettings,
  type TableImportsApi,
} from '../../../api/tableImports';
import { usePoll } from '../../../hooks/usePoll';
import { notifyImportSessionChanged } from '../../importProgressSession';
import { DiscardImportConfirm } from '../pdfBatch/DiscardImportConfirm';
import {
  AddDataStep,
  CheckRowsStep,
  ColumnsStep,
  ImportTableStep,
  type TableImportDestinationSheet,
} from './TableImportSteps';
import {
  sourceIssueForFiles,
  tableImportDefaultSheetName,
  TABLE_IMPORT_STAGES,
  type TableImportSourceIssue,
  type TableImportSourceMode,
  type TableImportStage,
} from './model';

export interface TableImportShellContext {
  active: true;
  busy: boolean;
  stage: TableImportStage;
  stages: typeof TABLE_IMPORT_STAGES;
  subtitle: string;
}

export interface TableImportFlowProps {
  api?: TableImportsApi;
  destinationSheets: readonly TableImportDestinationSheet[];
  followTheMoneyEnabled?: boolean;
  initialFile?: File | null;
  initialRerunDraftId?: string | null;
  onContextChange(context: TableImportShellContext | null): void;
  onDiscarded(): void;
  onError(message: string): void;
  onImported(sheetId: number): void;
  onUseFollowTheMoney(file: File): void;
  onUseMultipleFiles(files?: File[]): void;
  onUsePdfSplit(file: File): void;
  projectId: string;
  registerCloseGuard(guard: (() => boolean) | null): void;
}

function sameDraft(current: TableImportDraft | null, next: TableImportDraft): boolean {
  return current === null || current.draft_id === next.draft_id;
}

function mappingsFor(
  draft: TableImportDraft,
  destination: TableImportDestinationConfig,
  sheets: readonly TableImportDestinationSheet[],
): TableImportMappingRequest[] {
  const sheet = destination.kind === 'existing'
    ? sheets.find((candidate) => candidate.id === destination.sheet_id)
    : null;
  return draft.columns.filter((column) => column.included && column.target_type !== 'omit').map((column) => {
    const normalized = column.name.replace(/[\s_]+/g, '').toLocaleLowerCase();
    const matched = sheet?.columns.find((candidate) => candidate.name.replace(/[\s_]+/g, '').toLocaleLowerCase() === normalized);
    return {
      source_index: column.source_index,
      target: matched
        ? { kind: 'existing' as const, column_id: matched.id }
        : { kind: 'new' as const, name: column.name },
    };
  });
}

export function TableImportFlow({
  api = tableImportsApi,
  destinationSheets,
  followTheMoneyEnabled = false,
  initialFile = null,
  initialRerunDraftId = null,
  onContextChange,
  onDiscarded,
  onError,
  onImported,
  onUseFollowTheMoney,
  onUseMultipleFiles,
  onUsePdfSplit,
  projectId,
  registerCloseGuard,
}: TableImportFlowProps) {
  const [stage, setStage] = useState<TableImportStage>('add');
  const [sourceMode, setSourceMode] = useState<TableImportSourceMode>('file');
  const [sourceIssue, setSourceIssue] = useState<TableImportSourceIssue | null>(null);
  const [selectedFile, setSelectedFile] = useState<File | null>(initialFile);
  const [pasteText, setPasteText] = useState('');
  const [largePasteConfirmed, setLargePasteConfirmed] = useState(false);
  const [password, setPassword] = useState('');
  const [snapshot, setSnapshot] = useState<TableImportDraft | null>(null);
  const [destination, setDestination] = useState<TableImportDestinationConfig>({ kind: 'new', sheet_name: 'Imported table' });
  const [busy, setBusy] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [discardPrompt, setDiscardPrompt] = useState(false);
  const [discarding, setDiscarding] = useState(false);
  const generation = useRef(0);
  const currentDraft = useRef<string | null>(null);
  const latestSnapshot = useRef<TableImportDraft | null>(null);
  const requestController = useRef<AbortController | null>(null);
  const mutationTail = useRef<Promise<void>>(Promise.resolve());
  const commitKey = useRef(crypto.randomUUID());
  const destinationSaveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const callbacks = useRef({ onDiscarded, onError, onImported });
  const rerunSource = useRef<string | null>(null);
  const initialFileStarted = useRef<File | null>(null);

  useEffect(() => { callbacks.current = { onDiscarded, onError, onImported }; }, [onDiscarded, onError, onImported]);

  const publish = useCallback((next: TableImportDraft) => {
    if (currentDraft.current !== null && currentDraft.current !== next.draft_id) return;
    const current = latestSnapshot.current;
    if (!sameDraft(current, next) || (current && next.revision < current.revision)) return;
    currentDraft.current = next.draft_id;
    latestSnapshot.current = next;
    setSnapshot(next);
    setError(next.analysis.error ?? null);
  }, []);

  const reportError = useCallback((problem: unknown) => {
    if (problem instanceof DOMException && problem.name === 'AbortError') return;
    const message = problem instanceof Error ? problem.message : String(problem);
    setError(message);
    callbacks.current.onError(message);
  }, []);

  const resetDraft = useCallback(() => {
    generation.current += 1;
    requestController.current?.abort();
    requestController.current = null;
    currentDraft.current = null;
    latestSnapshot.current = null;
    setSnapshot(null);
    setSelectedFile(null);
    setPassword('');
    setError(null);
    setStage('add');
    commitKey.current = crypto.randomUUID();
  }, []);

  const createFromFile = useCallback(async (file: File) => {
    const epoch = ++generation.current;
    requestController.current?.abort();
    const controller = new AbortController();
    requestController.current = controller;
    setBusy(true);
    setSourceIssue(null);
    setError(null);
    try {
      const old = latestSnapshot.current;
      if (old && old.state !== 'committed' && old.state !== 'committing') {
        await api.discard(projectId, old.draft_id, { signal: controller.signal });
      }
      currentDraft.current = null;
      latestSnapshot.current = null;
      setSnapshot(null);
      const next = await api.createFromFile(projectId, file, { signal: controller.signal });
      if (generation.current !== epoch || controller.signal.aborted) return;
      publish(next);
      setDestination({ kind: 'new', sheet_name: tableImportDefaultSheetName(next.source.filename, next.source.input_kind) });
      setStage(next.state === 'password_required' ? 'add' : 'rows');
    } catch (problem) {
      if (generation.current !== epoch || controller.signal.aborted) return;
      if (problem instanceof TableImportResponseError && (problem.code === 'empty_source' || problem.code === 'unsupported_source' || problem.code === 'invalid_table')) {
        setSourceIssue(problem.code === 'empty_source' ? { kind: 'empty', filename: file.name } : { kind: 'unsupported', filename: file.name, message: problem.message });
      } else reportError(problem);
    } finally {
      if (generation.current === epoch && requestController.current === controller) {
        requestController.current = null;
        setBusy(false);
      }
    }
  }, [api, projectId, publish, reportError]);

  const chooseFiles = useCallback((files: File[]) => {
    const issue = sourceIssueForFiles(files, followTheMoneyEnabled);
    if (issue) { setSelectedFile(null); setSourceIssue(issue); return; }
    const file = files[0];
    setSelectedFile(file ?? null);
    setSourceIssue(null);
    if (file) void createFromFile(file);
  }, [createFromFile, followTheMoneyEnabled]);

  useEffect(() => {
    if (!initialFile || initialFileStarted.current === initialFile) return;
    initialFileStarted.current = initialFile;
    chooseFiles([initialFile]);
  }, [chooseFiles, initialFile]);

  const createFromPaste = useCallback(async (confirmed: boolean) => {
    if (!pasteText.trim()) return;
    const file = new File([pasteText], 'Pasted rows.csv', { type: 'text/csv;charset=utf-8' });
    if (!confirmed && file.size > TABLE_IMPORT_LARGE_PASTE_WARNING_BYTES) {
      setSourceIssue({ kind: 'large_paste', size: file.size });
      return;
    }
    const epoch = ++generation.current;
    const controller = new AbortController();
    requestController.current?.abort();
    requestController.current = controller;
    setBusy(true);
    setError(null);
    try {
      const old = latestSnapshot.current;
      if (old && old.state !== 'committed' && old.state !== 'committing') await api.discard(projectId, old.draft_id, { signal: controller.signal });
      currentDraft.current = null;
      latestSnapshot.current = null;
      setSnapshot(null);
      const next = await api.createFromPaste(projectId, file, confirmed, { signal: controller.signal });
      if (generation.current !== epoch || controller.signal.aborted) return;
      publish(next);
      setLargePasteConfirmed(confirmed);
      setSourceIssue(null);
      setDestination({ kind: 'new', sheet_name: 'Pasted rows' });
      setStage('rows');
    } catch (problem) {
      if (generation.current !== epoch || controller.signal.aborted) return;
      if (problem instanceof TableImportResponseError && problem.code === 'large_paste_confirmation_required') setSourceIssue({ kind: 'large_paste', size: file.size });
      else reportError(problem);
    } finally {
      if (generation.current === epoch && requestController.current === controller) {
        requestController.current = null;
        setBusy(false);
      }
    }
  }, [api, pasteText, projectId, publish, reportError]);

  useEffect(() => {
    if (!initialRerunDraftId || rerunSource.current === initialRerunDraftId) return;
    rerunSource.current = initialRerunDraftId;
    const epoch = ++generation.current;
    requestController.current?.abort();
    const controller = new AbortController();
    requestController.current = controller;
    setBusy(true);
    setError(null);
    void (async () => {
      try {
        const retained = await api.get(projectId, initialRerunDraftId, { signal: controller.signal });
        if (!retained.source.can_rerun_workbook) throw new Error('This workbook is no longer available to import another sheet.');
        const worksheet = retained.settings?.resolved.worksheet
          ?? retained.settings?.requested.worksheet
          ?? retained.source.sheets.find((sheet) => !sheet.empty)?.name;
        if (!worksheet) throw new Error('This workbook has no other readable sheets.');
        const next = await api.rerun(projectId, initialRerunDraftId, worksheet, { signal: controller.signal });
        if (generation.current !== epoch || controller.signal.aborted) return;
        publish(next);
        setDestination({ kind: 'new', sheet_name: tableImportDefaultSheetName(next.source.filename, next.source.input_kind) });
        setStage(next.state === 'password_required' ? 'add' : 'rows');
      } catch (problem) {
        if (generation.current === epoch && !controller.signal.aborted) reportError(problem);
      } finally {
        if (generation.current === epoch && requestController.current === controller) {
          requestController.current = null;
          setBusy(false);
        }
      }
    })();
  }, [api, initialRerunDraftId, projectId, publish, reportError]);

  const refresh = useCallback(async () => {
    const draftId = currentDraft.current;
    const epoch = generation.current;
    if (!draftId) return 'stop' as const;
    try {
      const next = await api.get(projectId, draftId);
      if (generation.current !== epoch || currentDraft.current !== draftId) return 'stop' as const;
      publish(next);
      return next.analysis.state === 'running' ? undefined : 'stop' as const;
    } catch (problem) {
      if (generation.current === epoch) reportError(problem);
      return 'stop' as const;
    }
  }, [api, projectId, publish, reportError]);

  usePoll(refresh, { active: snapshot?.analysis.state === 'running', intervalMs: 1200, guardOverlap: true, mode: 'settle-relative' });

  const enqueueMutation = useCallback((mutation: (source: TableImportDraft) => Promise<TableImportDraft>): Promise<TableImportDraft | null> => {
    let result: TableImportDraft | null = null;
    const run = async () => {
      const source = latestSnapshot.current;
      if (!source) return;
      setSaving(true);
      try {
        try {
          result = await mutation(source);
        } catch (problem) {
          if (!(problem instanceof TableImportResponseError) || problem.status !== 409) throw problem;
          const latest = await api.get(projectId, source.draft_id);
          publish(latest);
          result = await mutation(latest);
        }
        if (result) publish(result);
      } catch (problem) {
        reportError(problem);
      } finally {
        setSaving(false);
      }
    };
    const promise = mutationTail.current.then(run, run);
    mutationTail.current = promise.then(() => undefined, () => undefined);
    return promise.then(() => result);
  }, [api, projectId, publish, reportError]);

  const updateSettings = useCallback((settings: TableImportRequestedSettings) => {
    void enqueueMutation((source) => api.updateSettings(projectId, source.draft_id, source.revision, settings));
  }, [api, enqueueMutation, projectId]);

  const updateColumns = useCallback((columns: TableImportColumnPatch[]) => {
    void enqueueMutation((source) => api.updateColumns(projectId, source.draft_id, source.revision, columns));
  }, [api, enqueueMutation, projectId]);

  const saveDestination = useCallback((nextDestination: TableImportDestinationConfig, edited?: TableImportDestinationSnapshot | null) => {
    setDestination(nextDestination);
    return enqueueMutation((source) => {
      const mappings = edited?.mappings ?? mappingsFor(source, nextDestination, destinationSheets);
      const dedup = edited?.dedup ?? { enabled: false, key_source_index: null };
      return api.updateDestination(projectId, source.draft_id, source.revision, nextDestination, mappings, dedup);
    });
  }, [api, destinationSheets, enqueueMutation, projectId]);

  const scheduleDestination = useCallback((nextDestination: TableImportDestinationConfig, edited?: TableImportDestinationSnapshot | null) => {
    setDestination(nextDestination);
    if (destinationSaveTimer.current !== null) clearTimeout(destinationSaveTimer.current);
    destinationSaveTimer.current = setTimeout(() => {
      destinationSaveTimer.current = null;
      void saveDestination(nextDestination, edited);
    }, edited ? 0 : 250);
  }, [saveDestination]);

  useEffect(() => () => {
    generation.current += 1;
    requestController.current?.abort();
    if (destinationSaveTimer.current !== null) clearTimeout(destinationSaveTimer.current);
  }, []);

  const openImportStep = useCallback(async () => {
    const source = latestSnapshot.current;
    if (!source) return;
    const defaultDestination: TableImportDestinationConfig = { kind: 'new', sheet_name: tableImportDefaultSheetName(source.source.filename, source.source.input_kind) };
    const desired = destination.kind === 'new' && destination.sheet_name === 'Imported table' ? defaultDestination : destination;
    setDestination(desired);
    await saveDestination(desired);
    setStage('import');
  }, [destination, saveDestination]);

  const unlock = useCallback(async () => {
    const source = latestSnapshot.current;
    if (!source || !password) return;
    setBusy(true);
    try {
      const next = await api.unlock(projectId, source.draft_id, password);
      publish(next);
      setPassword('');
      setStage('rows');
    } catch (problem) { reportError(problem); }
    finally { setBusy(false); }
  }, [api, password, projectId, publish, reportError]);

  const requestDiscard = useCallback(() => {
    const source = latestSnapshot.current;
    if (!source) { callbacks.current.onDiscarded(); return; }
    if (source.state === 'committed' || source.state === 'committing') return;
    setDiscardPrompt(true);
  }, []);

  const handOff = useCallback(async (action: () => void) => {
    const source = latestSnapshot.current;
    setBusy(true);
    try {
      if (source && source.state !== 'committed' && source.state !== 'committing') {
        await api.discard(projectId, source.draft_id);
      }
      resetDraft();
      action();
    } catch (problem) {
      reportError(problem);
    } finally {
      setBusy(false);
    }
  }, [api, projectId, reportError, resetDraft]);

  useEffect(() => {
    registerCloseGuard(snapshot && snapshot.state !== 'committed' && snapshot.state !== 'committing' ? () => { requestDiscard(); return false; } : null);
    return () => registerCloseGuard(null);
  }, [registerCloseGuard, requestDiscard, snapshot]);

  const discard = async () => {
    const source = latestSnapshot.current;
    if (!source || discarding) return;
    setDiscarding(true);
    try {
      await api.discard(projectId, source.draft_id);
      resetDraft();
      callbacks.current.onDiscarded();
    } catch (problem) { reportError(problem); setDiscarding(false); }
  };

  const commit = async () => {
    if (busy) return;
    if (destinationSaveTimer.current !== null) { clearTimeout(destinationSaveTimer.current); destinationSaveTimer.current = null; }
    setBusy(true);
    try {
      await mutationTail.current;
      const saved = await saveDestination(destination);
      const source = saved ?? latestSnapshot.current;
      if (!source) return;
      try {
        const result = await api.commit(projectId, source.draft_id, source.revision, commitKey.current);
        notifyImportSessionChanged();
        callbacks.current.onImported(result.sheet_id);
      } catch (problem) {
        if (!(problem instanceof TableImportResponseError) || problem.status !== 409) throw problem;
        const latest = await api.get(projectId, source.draft_id);
        publish(latest);
        setStage('import');
        throw new Error('The import changed while you were reviewing it. Check the latest totals, then import again.', { cause: problem });
      }
    } catch (problem) { reportError(problem); }
    finally { setBusy(false); }
  };

  const subtitle = useMemo(() => {
    if (!snapshot) return 'One file · CSV, Excel or pasted rows';
    const rows = snapshot.analysis.exact_rows ?? snapshot.analysis.estimated_rows;
    return `${snapshot.source.filename}${rows == null ? '' : ` · ${snapshot.analysis.complete ? '' : 'about '}${rows.toLocaleString()} rows`}${snapshot.columns.length ? ` · ${snapshot.columns.length.toLocaleString()} columns` : ''}`;
  }, [snapshot]);

  useEffect(() => {
    onContextChange({ active: true, busy: busy || saving, stage, stages: TABLE_IMPORT_STAGES, subtitle });
    return () => onContextChange(null);
  }, [busy, onContextChange, saving, stage, subtitle]);

  const working = busy || saving;
  return <div className="table-import-flow">
    {error && <div className="table-import-error" role="alert">{error}</div>}
    {stage === 'add' && <AddDataStep
      busy={busy}
      issue={sourceIssue}
      mode={sourceMode}
      selectedFile={selectedFile}
      password={password}
      passwordFilename={snapshot?.state === 'password_required' ? snapshot.source.filename : null}
      pasteText={pasteText}
      onCancel={requestDiscard}
      onChooseFtm={(file) => { void handOff(() => onUseFollowTheMoney(file)); }}
      onChooseMode={(mode) => { setSourceMode(mode); setSourceIssue(null); }}
      onFiles={chooseFiles}
      onLargePasteConfirm={() => { setLargePasteConfirmed(true); void createFromPaste(true); }}
      onMultipleFiles={(files) => { void handOff(() => onUseMultipleFiles(files)); }}
      onPasswordChange={setPassword}
      onPasteChange={(value) => { setPasteText(value); setSourceIssue(null); setLargePasteConfirmed(false); }}
      onPasteSubmit={() => void createFromPaste(largePasteConfirmed)}
      onSelectedFileSubmit={() => { if (selectedFile) void createFromFile(selectedFile); }}
      onResetIssue={() => { setSourceMode('file'); setSelectedFile(null); setSourceIssue(null); }}
      onSplitPdf={(file) => { void handOff(() => onUsePdfSplit(file)); }}
      onUnlock={() => void unlock()}
    />}
    {stage === 'rows' && snapshot && <CheckRowsStep draft={snapshot} busy={working} onBack={() => setStage('add')} onContinue={() => setStage('columns')} onSettings={updateSettings} />}
    {stage === 'columns' && snapshot && <ColumnsStep draft={snapshot} busy={working} onBack={() => setStage('rows')} onColumns={updateColumns} onContinue={() => void openImportStep()} />}
    {stage === 'import' && snapshot && <ImportTableStep draft={snapshot} destination={destination} destinationSheets={destinationSheets} busy={working} onBack={() => setStage('columns')} onCommit={() => void commit()} onDestination={scheduleDestination} />}
    {discardPrompt && <DiscardImportConfirm title="Cancel this import?" message="Files won’t be added to the project." confirmLabel="Discard import" busy={discarding} testId="table-import-discard-dialog" onKeep={() => setDiscardPrompt(false)} onConfirm={() => void discard()} />}
  </div>;
}
