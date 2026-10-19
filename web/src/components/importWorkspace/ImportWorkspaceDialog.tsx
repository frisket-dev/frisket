import { Button } from '../Button';
import { TextInput, TextArea } from '../FormControls';
import { Fragment, useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react';
import { ImportStep, ImportStepBody } from './ImportStep';
import { ImportStepFooter } from './ImportStepFooter';
import { ImportDropArea } from './ImportDropArea';
import {
  FileText,
  Link2,
  X,
} from 'lucide-react';
import {
  getImportSession,
  snapshotImportFiles,
  executeBulkImport,
  prepareImportFiles,
  type ImportSessionStatus,
  type BulkImportFailedOutput,
  type BulkImportPlan,
  type PreparedImportFiles,
  type ActionTemplate,
  type SheetMeta,
} from '../../api/open';
import { pdfImportsApi } from '../../api/pdfImports';
import { useWorkspaceStores } from '../../bind/useWorkspaceStores';
import { usePoll } from '../../hooks/usePoll';
import { PanelSelect } from '../PanelSelect';
import { SegmentedToggle } from '../PanelPrimitives';
import { ImportSessionProgress } from './ImportSessionProgress';
import { ImportModeRail } from './ImportModeRail';
import { ImportStageStepper } from './ImportStageStepper';
import {
  PdfPacketImportFlow,
  type PdfPacketShellContext,
} from './pdfPacket/PdfPacketImportFlow';
import { PDF_PACKET_STAGES } from './pdfPacket/model';
import {
  DiscardImportConfirm,
  PdfBatchImportFlow,
  PdfBatchDrafts,
  canHandlePdfBatch,
  type PdfBatchShellContext,
} from './pdfBatch';
import {
  TableImportFlow,
  type TableImportShellContext,
} from './tableImport';
import { SearchPreparationReceipt } from '../SearchPreparationReceipt';
import { IMPORT_MODES, type FileImportMode, type ImportMode } from './model';
import {
  nerImportOffer,
  type NerImportOffer,
} from '../nerImportPromptModel';
import { FeedSourceForm } from './FeedSourceForm';
import { BulkImportPlanPanel } from './BulkImportPlanPanel';
import { MediaDownloadPrompt, NerExtractPrompt } from './ImportFollowupPrompts';
import { ACCEPTS, FTM_ACCEPT, prepareFileSelection, logicalPathForImport, selectionExpandsArchives, bulkPlanValidationError, importSessionIsComplete, importSessionNeedsPreparationPlan, useImportUpload, type PreparedFileSelection, type FileImportCompletion, type SingleFileFormat } from './fileIntake';
import type { ImportHandlers } from './model';

interface ImportWorkspaceState {
  mode: ImportMode;
  urlText: string;
}

type ImportWorkspaceAction =
  | { type: 'selectMode'; mode: ImportMode }
  | { type: 'setUrlText'; value: string };

const initialImportWorkspaceState = (mode: ImportMode): ImportWorkspaceState => ({
  mode,
  urlText: '',
});

function importWorkspaceReducer(
  state: ImportWorkspaceState,
  action: ImportWorkspaceAction,
): ImportWorkspaceState {
  switch (action.type) {
    case 'selectMode':
      return { ...state, mode: action.mode };
    case 'setUrlText':
      return { ...state, urlText: action.value };
    default:
      return state;
  }
}

type ImportWorkspaceDialogProps = ImportHandlers & {
  open: boolean;
  initialTableFile?: File | null;
  initialTableRerunDraftId?: string | null;
  initialFiles?: File[] | null;
  initialPreparationBatchId?: string | null;
  /** The resolved `ner` catalog entry, for its engine availability. Absent
   *  (no catalog yet) means the prompt simply does not offer. */
  nerTemplate?: ActionTemplate;
  /** True only when the ready action catalog exposes the FtM importer. */
  ftmImportEnabled?: boolean;
  /** Forces the dialog onto this mode for this open (Sources' "Add source"
   *  passes 'feed', since sources are for feeds).
   *  `null`/omitted -> the dialog reuses the last user-selected rail. */
  entryMode?: ImportMode | null;
  onClose(): void;
  onModeChange?(mode: ImportMode | null): void;
};

export function ImportWorkspaceDialog({ open, ...props }: ImportWorkspaceDialogProps) {
  // Keep only the selected rail outside an open session so a normal close can
  // reopen at the same entry point. Plans, choices, warnings, and requests
  // remain below this boundary and are discarded when `open` becomes false.
  const [lastMode, setLastMode] = useState<ImportMode>('csv');
  const initialMode = props.entryMode ?? lastMode;

  return open ? (
    <OpenImportWorkspaceSession
      key={props.initialPreparationBatchId ?? props.initialTableRerunDraftId ?? 'import'}
      {...props}
      initialMode={initialMode}
      onSessionModeChange={setLastMode}
    />
  ) : null;
}

function OpenImportWorkspaceSession({
  initialMode,
  onSessionModeChange,
  initialTableFile = null,
  initialTableRerunDraftId = null,
  initialFiles = null,
  initialPreparationBatchId = null,
  onClose,
  onImported,
  onError,
  onModeChange,
  onLaunchDownload,
  onExtractEntities,
  nerTemplate,
  ftmImportEnabled = false,
}: Omit<ImportWorkspaceDialogProps, 'open' | 'entryMode'> & {
  initialMode: ImportMode;
  onSessionModeChange(mode: ImportMode): void;
}) {
  const { projectApi, route, chromePreferences: { projectId } } = useWorkspaceStores();
  const [importSessionStatus, setImportSessionStatus] = useState<ImportSessionStatus | null>(null);
  const {
    busy: uploadBusy,
    sessionBusy,
    uploadFiles,
    uploadUrls,
  } = useImportUpload({ onError, onSessionStatus: setImportSessionStatus });
  const inputRef = useRef<HTMLInputElement | null>(null);
  const dialogRef = useRef<HTMLDialogElement | null>(null);
  const onCloseRef = useRef(onClose);
  const onModeChangeRef = useRef(onModeChange);
  const pdfPacketCloseGuardRef = useRef<(() => boolean) | null>(null);
  const pdfBatchCloseGuardRef = useRef<(() => boolean) | null>(null);
  const tableImportCloseGuardRef = useRef<(() => boolean) | null>(null);
  const [tableImportContext, setTableImportContext] = useState<TableImportShellContext | null>(null);
  const registerTableImportCloseGuard = useCallback((guard: (() => boolean) | null) => {
    tableImportCloseGuardRef.current = guard;
  }, []);
  const [pdfBatchFiles, setPdfBatchFiles] = useState<File[] | null>(null);
  const [initialDestinationSheetId] = useState(() => {
    const id = Number(route.store.get().sheetId);
    return Number.isSafeInteger(id) && id > 0 ? id : null;
  });
  const [pdfBatchResumeId, setPdfBatchResumeId] = useState<string | null>(null);
  const pdfBatchActive = pdfBatchFiles !== null || pdfBatchResumeId !== null;
  const [pdfBatchContext, setPdfBatchContext] = useState<PdfBatchShellContext | null>(null);
  const registerPdfBatchCloseGuard = useCallback((guard: (() => boolean) | null) => {
    pdfBatchCloseGuardRef.current = guard;
  }, []);
  const [pdfPacketContext, setPdfPacketContext] = useState<PdfPacketShellContext>({
    active: false,
    stage: 'check',
    subtitle: null,
    busy: false,
  });
  const [pdfPacketInitialFile, setPdfPacketInitialFile] = useState<File | null>(null);
  const updatePdfPacketContext = useCallback((context: PdfPacketShellContext) => {
    setPdfPacketContext(context);
  }, []);
  const registerPdfPacketCloseGuard = useCallback((guard: (() => boolean) | null) => {
    pdfPacketCloseGuardRef.current = guard;
  }, []);
  const [state, dispatch] = useReducer(
    importWorkspaceReducer,
    initialMode,
    initialImportWorkspaceState,
  );
  const {
    mode,
    urlText,
  } = state;
  const fileMode: FileImportMode =
    mode === 'xlsx' || mode === 'files' ? mode : 'csv';
  const railMode: ImportMode = mode === 'xlsx' ? 'csv' : mode === 'urls' ? 'files' : mode;

  // Sibling to
  // FeedSourceForm's own `populate` state — local, not a store slot,
  // for the same reason (no unmounted-target race: this prompt and its
  // onImported/onLaunchDownload targets live in the same always-mounted
  // component tree for the dialog's lifetime).
  const [downloadPrompt, setDownloadPrompt] = useState<{
    columnName: string;
    launcherKind: string;
  } | null>(null);
  const [nerPrompt, setNerPrompt] = useState<NerImportOffer | null>(null);
  const [preparationReceipt, setPreparationReceipt] = useState<{
    sheetId: number | null;
    batchId: string | null;
    importRef: string | null;
    bulkOutputs?: { sheetId: number; sheetName: string; batchId: string | null }[];
  } | null>(() => initialPreparationBatchId === null ? null : {
    sheetId: null,
    batchId: initialPreparationBatchId,
    importRef: null,
  });
  const [preparationStep, setPreparationStep] = useState<'summary' | 'review'>(initialPreparationBatchId ? 'review' : 'summary');
  const [fileSummary, setFileSummary] = useState<{ names: string[]; count: number } | null>(null);
  const [preparationFollowupError, setPreparationFollowupError] = useState<string | null>(null);
  const [ftmFile, setFtmFile] = useState<File | null>(null);
  const [singleFileFormat, setSingleFileFormat] = useState<SingleFileFormat>('csv');
  const tableImportSelected = railMode === 'csv' && singleFileFormat !== 'ftm';
  const [ftmDatasetName, setFtmDatasetName] = useState('');
  const folderInputRef = useRef<HTMLInputElement | null>(null);
  const zipInputRef = useRef<HTMLInputElement | null>(null);
  const bulkPlanControllerRef = useRef<AbortController | null>(null);
  const bulkExecuteControllerRef = useRef<AbortController | null>(null);
  const bulkGenerationRef = useRef(0);
  const initialFilesConsumedRef = useRef(false);
  const [bulkPlan, setBulkPlan] = useState<BulkImportPlan | null>(null);
  const [bulkDecisions, setBulkDecisions] = useState<Record<string, string>>({});
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkPreparing, setBulkPreparing] = useState(false);
  const [bulkPrepareDiscardPrompt, setBulkPrepareDiscardPrompt] = useState(false);
  const [bulkPrepareDiscarding, setBulkPrepareDiscarding] = useState(false);
  const bulkPrepareDiscardPromptRef = useRef(false);
  const heldPreparedImport = useRef<PreparedImportFiles | null>(null);
  const [bulkError, setBulkError] = useState<string | null>(null);
  const [bulkSuccessMessage, setBulkSuccessMessage] = useState<string | null>(null);
  const [bulkWarnings, setBulkWarnings] = useState<string[]>([]);
  const [bulkFailures, setBulkFailures] = useState<BulkImportFailedOutput[]>([]);
  const [bulkFailuresOmitted, setBulkFailuresOmitted] = useState(0);
  const [selectionPreparing, setSelectionPreparing] = useState(false);
  const selectionPreparationGeneration = useRef(0);
  const [destinationSheets, setDestinationSheets] = useState<SheetMeta[]>([]);
  const busy = uploadBusy;

  const prepareSelection = useCallback((files: File[], use: (prepared: PreparedFileSelection) => void) => {
    setImportSessionStatus(null);
    const generation = ++selectionPreparationGeneration.current;
    setFileSummary({ names: files.slice(0, 20).map(logicalPathForImport), count: files.length });
    setPreparationStep('summary');
    setSelectionPreparing(true);
    void prepareFileSelection(files).then((prepared) => {
      if (generation === selectionPreparationGeneration.current) use(prepared);
    }).finally(() => {
      if (generation === selectionPreparationGeneration.current) setSelectionPreparing(false);
    });
  }, []);
  // SheetMeta.parent is populated only from the list wire's parent_sheet_id;
  // imported roots may have a creation parent_op_id and remain appendable.
  const appendDestinationSheets = useMemo(
    () => destinationSheets.filter((sheet) => sheet.parent === undefined),
    [destinationSheets],
  );
  // A destination can disappear from a refreshed sheet list. Treat that
  // stale value as a new-sheet choice everywhere that can write an import.

  const invalidateBulkRequests = useCallback(() => {
    bulkGenerationRef.current += 1;
    bulkPlanControllerRef.current?.abort();
    bulkPlanControllerRef.current = null;
    bulkExecuteControllerRef.current?.abort();
    bulkExecuteControllerRef.current = null;
  }, []);

  const resetBulkPlan = useCallback(() => {
    invalidateBulkRequests();
    bulkPrepareDiscardPromptRef.current = false;
    heldPreparedImport.current = null;
    setBulkPlan(null);
    setBulkDecisions({});
    setBulkBusy(false);
    setBulkPreparing(false);
    setBulkPrepareDiscardPrompt(false);
    setBulkPrepareDiscarding(false);
    setBulkError(null);
    setBulkSuccessMessage(null);
    setBulkWarnings([]);
    setBulkFailures([]);
    setBulkFailuresOmitted(0);
  }, [invalidateBulkRequests]);

  // A controlled close unmounts this entire open session. Abort requests here
  // rather than mirroring `open` into state: no response from this session may
  // publish into the next one, and unmount discards all local UI state.
  useEffect(() => () => {
    selectionPreparationGeneration.current += 1;
    invalidateBulkRequests();
    onModeChangeRef.current?.(null);
  }, [invalidateBulkRequests]);

  useEffect(() => {
    let active = true;
    void projectApi.listSheets().then((sheets) => {
      if (active) setDestinationSheets(sheets);
    }).catch(() => {
      if (active) setDestinationSheets([]);
    });
    return () => { active = false; };
  }, [projectApi]);

  const closeWorkspace = useCallback(() => {
    resetBulkPlan();
    setFtmFile(null);
    setDownloadPrompt(null);
    setNerPrompt(null);
    setPreparationReceipt(null);
    setPreparationFollowupError(null);
    onClose();
  }, [onClose, resetBulkPlan]);
  const closeWorkspaceFromUser = useCallback(() => {
    if (bulkPrepareDiscardPromptRef.current) return;
    if (pdfBatchActive) {
      if (pdfBatchCloseGuardRef.current && !pdfBatchCloseGuardRef.current()) return;
      closeWorkspace();
      return;
    }
    if (tableImportSelected && tableImportCloseGuardRef.current && !tableImportCloseGuardRef.current()) return;
    if (bulkPreparing) {
      if (!bulkPrepareDiscardPromptRef.current) {
        bulkPrepareDiscardPromptRef.current = true;
        setBulkPrepareDiscardPrompt(true);
      }
      return;
    }
    if (selectionPreparing || (busy && !sessionBusy) || bulkBusy || pdfPacketContext.busy) return;
    if (mode === 'pdf_split' && pdfPacketCloseGuardRef.current && !pdfPacketCloseGuardRef.current()) return;
    closeWorkspace();
  }, [bulkBusy, bulkPreparing, busy, closeWorkspace, mode, pdfBatchActive, pdfPacketContext.busy, selectionPreparing, sessionBusy, tableImportSelected]);

  // The ONE shared success
  // handler replacing the three independent close-on-success calls
  // (confirmDraft; the file-input onChange and URL-submit upload callbacks
  // below — FeedSourceForm's own create()/populate flow is untouched, it
  // already holds the dialog open for its own "Populate feed?" prompt).
  // 1) reports the import up FIRST, unconditionally — every existing
  //    onImported consumer (the global sheets refresh) keeps working with
  //    no regression. 2) awaits getSheetData for the resulting
  //    sheet's columns — a call this adds, not a reuse of onImported's
  //    refresh, which cannot supply columns. 3) classifies via the
  //    server-computed mediaDownloadCandidate (compute_media_download_
  //    candidates, gated on the sheet having no audio/video/file column yet):
  //    found -> hold the dialog open on the download prompt; otherwise close
  //    immediately (unchanged for ordinary CSV/paste
  //    imports).
  const finishImport = useCallback(
    async (sheetId: number) => {
      onImported(sheetId);
      try {
        const page = await projectApi.getSheetData(String(sheetId), 0, 25);
        const candidate = page.columns.find((column) => column.mediaDownloadCandidate);
        if (candidate?.mediaDownloadCandidate) {
          setDownloadPrompt({
            columnName: candidate.name,
            launcherKind: candidate.mediaDownloadCandidate,
          });
          return;
        }
        // Media first when a sheet qualifies for both: undownloaded links are
        // the blocking next step, and there is no text to read until they
        // land. One prompt per import either way.
        const offer = nerImportOffer({
          columns: page.columns,
          rowCount: page.total,
          nerTemplate,
        });
        if (offer) {
          setNerPrompt(offer);
          return;
        }
      } catch {
        // Classification is a nicety, not a requirement — a failed lookup
        // here must never block the ordinary close-on-success path.
      }
      closeWorkspace();
    },
    [closeWorkspace, nerTemplate, onImported, projectApi],
  );

  const finishFileImport = useCallback((completion: FileImportCompletion | null) => {
    if (!completion) return;
    if (completion.searchPreparationBatchId || completion.importRef) {
      if (completion.sheetId !== null) onImported(completion.sheetId);
      setPreparationFollowupError(null);
      setImportSessionStatus(completion.sessionStatus);
      setPreparationStep('summary');
      setPreparationReceipt({
        sheetId: completion.sheetId,
        batchId: completion.searchPreparationBatchId,
        importRef: completion.importRef,
      });
      return;
    }
    if (completion.sheetId !== null) void finishImport(completion.sheetId);
  }, [finishImport, onImported]);

  usePoll(async () => {
    if (!preparationReceipt?.importRef) return;
    let status: ImportSessionStatus;
    try {
      status = await getImportSession(projectId, preparationReceipt.importRef);
      setPreparationFollowupError(null);
    } catch {
      setPreparationFollowupError('Could not refresh import progress. Retrying…');
      return;
    }
    setImportSessionStatus(status);
    if (preparationReceipt.sheetId === null && status.sheet_id !== null) onImported(status.sheet_id);
    setPreparationReceipt((current) => current ? {
      ...current,
      sheetId: current.sheetId ?? status.sheet_id,
      batchId: status.search_preparation_batch_id ?? current.batchId,
    } : current);
  }, {
    active: Boolean(preparationReceipt?.importRef
      && (!importSessionIsComplete(importSessionStatus)
        || importSessionNeedsPreparationPlan(importSessionStatus))),
    intervalMs: 1_000,
    guardOverlap: true,
    immediate: true,
  });

  useEffect(() => {
    onCloseRef.current = closeWorkspace;
    onModeChangeRef.current = onModeChange;
  }, [closeWorkspace, onModeChange]);

  useEffect(() => {
    onModeChangeRef.current?.(mode);
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) dialog.showModal();
    const focusFrame = window.requestAnimationFrame(() => {
      dialogRef.current?.focus();
    });
    return () => {
      window.cancelAnimationFrame(focusFrame);
    };
  }, [mode]);

  const selectMode = (nextMode: ImportMode) => {
    if (busy) return;
    selectionPreparationGeneration.current += 1;
    setSelectionPreparing(false);
    resetBulkPlan();
    setPdfPacketContext({ active: false, stage: 'check', subtitle: null, busy: false });
    pdfPacketCloseGuardRef.current = null;
    if (nextMode !== 'csv') {
      setSingleFileFormat('csv');
      setFtmDatasetName('');
    }
    if (nextMode !== 'pdf_split') setPdfPacketInitialFile(null);
    onSessionModeChange(nextMode);
    dispatch({ type: 'selectMode', mode: nextMode });
  };

  const runDownloadPrompt = () => {
    if (!downloadPrompt) return;
    const { launcherKind, columnName } = downloadPrompt;
    closeWorkspace();
    onLaunchDownload(launcherKind, columnName);
  };

  const dismissDownloadPrompt = () => {
    closeWorkspace();
  };

  const runNerPrompt = () => {
    closeWorkspace();
    onExtractEntities?.();
  };

  const dismissNerPrompt = () => {
    closeWorkspace();
  };

  const executeBulkPlan = useCallback((
    plan: BulkImportPlan,
    decisions: Record<string, string>,
  ) => {
    invalidateBulkRequests();
    setBulkPreparing(false);
    const generation = bulkGenerationRef.current;
    const controller = new AbortController();
    bulkExecuteControllerRef.current = controller;
    setBulkBusy(true);
    setBulkPlan(null);
    setBulkError(null);
    void executeBulkImport(
      projectId,
      plan.plan_id,
      decisions,
      { signal: controller.signal },
    ).then(async (result) => {
      if (controller.signal.aborted || bulkGenerationRef.current !== generation) return;
      const hasFailures = result.failed.length > 0 || result.failed_omitted > 0;
      const bulkOutputs = result.created.map((output) => ({
        sheetId: output.sheet_id,
        sheetName: output.sheet_name,
        batchId: output.search_preparation_batch_id ?? null,
      }));
      const firstPreparation = bulkOutputs.find((output) => output.batchId !== null);
      if (firstPreparation && typeof result.first_sheet_id === 'number') {
        await onImported(result.first_sheet_id);
        if (controller.signal.aborted || bulkGenerationRef.current !== generation) return;
        setBulkPlan(null);
        setBulkDecisions({});
        setBulkError(hasFailures ? result.message : null);
        setBulkSuccessMessage(hasFailures ? null : result.message);
        setBulkWarnings(result.warnings);
        setBulkFailures(result.failed);
        setBulkFailuresOmitted(result.failed_omitted);
        setPreparationStep('summary');
        setPreparationReceipt({ ...firstPreparation, importRef: null, bulkOutputs });
        return;
      }
      if (hasFailures) {
        // A 200 can still represent a partial (or total) import failure. The
        // server has consumed this one-shot plan, so remove its ready state
        // before navigating to any successful sheet.
        setBulkPlan(null);
        setBulkDecisions({});
        setBulkError(result.message);
        setBulkWarnings(result.warnings);
        setBulkFailures(result.failed);
        setBulkFailuresOmitted(result.failed_omitted);
        if (typeof result.first_sheet_id === 'number') {
          await onImported(result.first_sheet_id);
        }
        return;
      }
      if (typeof result.first_sheet_id === 'number') {
        await onImported(result.first_sheet_id);
        if (controller.signal.aborted || bulkGenerationRef.current !== generation) return;
        if (result.warnings.length) {
          // The plan is one-shot once execution starts. Keep only the
          // completed result open so warnings can be acknowledged without
          // offering a stale Execute button.
          setBulkPlan(null);
          setBulkDecisions({});
          setBulkSuccessMessage(result.message);
          setBulkWarnings(result.warnings);
          return;
        }
        closeWorkspace();
        return;
      }
      throw new Error('The import completed without a landing sheet.');
    }).catch((error: unknown) => {
      if (controller.signal.aborted || bulkGenerationRef.current !== generation) return;
      setBulkPlan(null);
      setBulkDecisions({});
      setBulkError(`Import failed: ${error instanceof Error ? error.message : String(error)}`);
    }).finally(() => {
      if (bulkExecuteControllerRef.current === controller && bulkGenerationRef.current === generation) {
        bulkExecuteControllerRef.current = null;
        setBulkBusy(false);
      }
    });
  }, [closeWorkspace, invalidateBulkRequests, onImported, projectId]);

  const acceptPreparedImport = useCallback((prepared: PreparedImportFiles) => {
    if (prepared.kind === 'pdf_import') {
      setPdfBatchFiles(null);
      setPdfBatchResumeId(prepared.draft.draft_id);
      return;
    }
    const nextPlan = prepared.plan;
    const validationError = bulkPlanValidationError(nextPlan);
    if (validationError) {
      setBulkPlan(nextPlan);
      setBulkError(validationError);
      return;
    }
    const decisions = Object.fromEntries(
      nextPlan.questions.map((question) => [question.id, question.default]),
    );
    if (nextPlan.proposed_outputs.length > 0 && nextPlan.questions.length === 0) {
      executeBulkPlan(nextPlan, decisions);
      return;
    }
    setBulkPlan(nextPlan);
    setBulkDecisions(decisions);
  }, [executeBulkPlan]);

  const startBulkPlan = useCallback((files: File[], expandArchive: boolean) => {
    if (busy || !files.length) return;
    const expandsArchives = expandArchive || selectionExpandsArchives(files);
    if (!expandsArchives && canHandlePdfBatch(files)) {
      setPdfBatchResumeId(null);
      setPdfBatchFiles(files);
      return;
    }
    setImportSessionStatus(null);
    setFileSummary({ names: files.slice(0, 20).map(logicalPathForImport), count: files.length });
    setPreparationStep('summary');
    invalidateBulkRequests();
    const generation = bulkGenerationRef.current;
    const controller = new AbortController();
    bulkPlanControllerRef.current = controller;
    setBulkPlan(null);
    setBulkDecisions({});
    setBulkError(null);
    setBulkSuccessMessage(null);
    setBulkWarnings([]);
    setBulkFailures([]);
    setBulkFailuresOmitted(0);
    setBulkBusy(true);
    setBulkPreparing(true);
    void prepareImportFiles(
      projectId,
      files,
      files.map(logicalPathForImport),
      expandsArchives,
      { signal: controller.signal },
    ).then((prepared) => {
      if (controller.signal.aborted || bulkGenerationRef.current !== generation) return;
      if (bulkPrepareDiscardPromptRef.current) {
        heldPreparedImport.current = prepared;
        return;
      }
      acceptPreparedImport(prepared);
    }).catch((error: unknown) => {
      if (controller.signal.aborted || bulkGenerationRef.current !== generation) return;
      setBulkError(`Import planning failed: ${error instanceof Error ? error.message : String(error)}`);
    }).finally(() => {
      if (bulkPlanControllerRef.current === controller && bulkGenerationRef.current === generation) {
        bulkPlanControllerRef.current = null;
        setBulkPreparing(false);
        setBulkBusy(false);
      }
    });
  }, [acceptPreparedImport, busy, invalidateBulkRequests, projectId]);

  const keepPreparingImport = () => {
    bulkPrepareDiscardPromptRef.current = false;
    setBulkPrepareDiscardPrompt(false);
    const prepared = heldPreparedImport.current;
    heldPreparedImport.current = null;
    if (prepared) acceptPreparedImport(prepared);
  };

  const discardPreparingImport = async () => {
    if (bulkPrepareDiscarding) return;
    const prepared = heldPreparedImport.current;
    if (!prepared || prepared.kind === 'bulk') {
      closeWorkspace();
      return;
    }
    setBulkPrepareDiscarding(true);
    try {
      await pdfImportsApi.discard(projectId, prepared.draft.draft_id);
      closeWorkspace();
    } catch (problem) {
      const message = problem instanceof Error ? problem.message : String(problem);
      setBulkError(message);
      onError(message);
      setBulkPrepareDiscarding(false);
    }
  };

  const handlePickedFiles = useCallback((files: File[]) => {
    if (!files.length) return;
    if (!selectionExpandsArchives(files) && canHandlePdfBatch(files)) {
      setPdfBatchResumeId(null);
      setPdfBatchFiles(files);
      return;
    }
    prepareSelection(files, (prepared) => {
      if (prepared.directNeedsBulkPlan) {
        startBulkPlan(files, selectionExpandsArchives(files));
      } else {
        resetBulkPlan();
        void uploadFiles(
          singleFileFormat === 'ftm' ? 'ftm' : fileMode,
          files, undefined, ftmDatasetName, undefined, undefined, undefined, prepared,
        ).then(finishFileImport);
      }
    });
  }, [
    fileMode,
    finishFileImport,
    ftmDatasetName,
    prepareSelection,
    resetBulkPlan,
    singleFileFormat,
    startBulkPlan,
    uploadFiles,
  ]);

  useEffect(() => {
    if (initialFilesConsumedRef.current || !initialFiles?.length) return;
    initialFilesConsumedRef.current = true;
    startBulkPlan(initialFiles, selectionExpandsArchives(initialFiles));
  }, [initialFiles, startBulkPlan]);

  const executePlannedImport = () => {
    if (!bulkPlan || bulkBusy || bulkPlanValidationError(bulkPlan)) return;
    if (bulkPlan.questions.some((question) => (
      bulkDecisions[question.id] !== 'combine' && bulkDecisions[question.id] !== 'separate'
    ))) {
      setBulkError('Choose an answer for every import decision before executing.');
      return;
    }
    executeBulkPlan(bulkPlan, bulkDecisions);
  };

  const bulkReceiptSummary = preparationReceipt?.bulkOutputs ? (
    <>
      {preparationStep === 'summary' && <BulkImportPlanPanel
        plan={null} decisions={{}} busy={false} error={bulkError}
        successMessage={bulkSuccessMessage} warnings={bulkWarnings}
        failures={bulkFailures} failuresOmitted={bulkFailuresOmitted}
        onDecisionChange={() => {}} onExecute={() => {}}
      />}
      {preparationReceipt.bulkOutputs.length > 1 && (
        <label className="form-field">
          Review imported sheet
          <PanelSelect
            ariaLabel="Review imported sheet"
            topLayer
            value={preparationReceipt.sheetId ?? ''}
            onValueChange={(value) => {
              const selected = preparationReceipt.bulkOutputs?.find((output) => output.sheetId === Number(value));
              if (selected) setPreparationReceipt((current) => current ? { ...current, ...selected } : current);
            }}
          >
            {preparationReceipt.bulkOutputs.map((output) => (
              <option key={output.sheetId} value={output.sheetId}>{output.sheetName}</option>
            ))}
          </PanelSelect>
        </label>
      )}
    </>
  ) : null;

  const importInProgress = Boolean(fileSummary && ((uploadBusy && railMode === 'files') || bulkBusy)
    && !bulkPlan);
  const showPreparation = preparationReceipt !== null || importInProgress;
  const tableImportActive = tableImportSelected && !showPreparation;
  const fileEntry = railMode === 'files' && !showPreparation && !pdfBatchActive && !downloadPrompt && !nerPrompt;
  const EntryStep = fileEntry ? ImportStep : Fragment;
  const EntryBody = fileEntry ? ImportStepBody : Fragment;
  const closeDisabled = !pdfBatchActive && (
    selectionPreparing || (busy && !sessionBusy) || (bulkBusy && !bulkPreparing) || pdfPacketContext.busy
  );
  const tableImportChoosingSource = tableImportActive
    && (tableImportContext?.canChangeMode ?? true)
    && (tableImportContext?.stage ?? 'add') === 'add';
  const showSummary = showPreparation && preparationStep === 'summary';
  const importComplete = importSessionIsComplete(importSessionStatus);
  const preparationPending = importSessionNeedsPreparationPlan(importSessionStatus);
  const importingSession = importSessionStatus !== null && !importComplete;
  const summaryWaiting = importInProgress || importingSession || preparationPending;
  const canReviewPreparation = Boolean(preparationReceipt?.batchId
    && !preparationPending
    && (importSessionStatus === null
      || importSessionStatus.state === 'completed'
      || importSessionStatus.state === 'kept'));
  const summaryHeading = importSessionStatus?.state === 'paused'
    ? 'Import paused'
    : importSessionStatus?.state === 'cancelled'
      ? 'Import cancelled'
      : importSessionStatus?.state === 'removed'
        ? 'Import removed'
        : summaryWaiting && !preparationPending
          ? 'Importing files'
          : 'Files imported';

  return (
    <dialog
      ref={dialogRef}
      className="import-workspace-layer"
      data-testid="import-workspace-dialog"
      aria-label="Import"
      onCancel={(event) => {
        event.preventDefault();
        closeWorkspaceFromUser();
      }}
      onClose={() => {
        closeWorkspaceFromUser();
      }}
    >
      <button
        type="button"
        className="import-workspace-backdrop"
        aria-label="Close import"
        onMouseDown={closeWorkspaceFromUser}
      />
      <section
        className={`import-workspace${pdfPacketContext.active || pdfBatchActive ? ' pdf-packet-workspace' : ''}${pdfBatchActive ? ' pdf-batch-workspace' : ''}${tableImportActive ? ' table-import-workspace' : ''}${tableImportChoosingSource ? ' table-import-workspace-mode-selection' : ''}`}
        data-testid="import-workspace"
        aria-label="Import"
        tabIndex={-1}
      >
        <input
          aria-label="Choose import file"
          ref={inputRef}
          type="file"
          accept={singleFileFormat === 'ftm' ? FTM_ACCEPT : ACCEPTS[fileMode]}
          multiple={railMode === 'files'}
          style={{ display: 'none' }}
          data-testid={mode === 'csv' ? 'import-csv-input' : 'import-file-input'}
          onChange={(e) => {
            const files = snapshotImportFiles(e.target.files);
            e.target.value = '';
            handlePickedFiles(files);
          }}
        />
        <input
          aria-label="Choose folder"
          ref={(input) => {
            folderInputRef.current = input;
            input?.setAttribute('webkitdirectory', '');
          }}
          type="file"
          multiple
          style={{ display: 'none' }}
          data-testid="import-folder-input"
          onChange={(e) => {
            const files = snapshotImportFiles(e.target.files);
            e.target.value = '';
            if (!files.length) return;
            if (!selectionExpandsArchives(files) && canHandlePdfBatch(files)) {
              setPdfBatchResumeId(null);
              setPdfBatchFiles(files);
              return;
            }
            prepareSelection(files, (prepared) => {
              if (prepared.usesAttachmentSession) {
                resetBulkPlan();
                void uploadFiles(
                  'files', files, undefined, undefined, undefined, undefined, undefined, prepared,
                ).then(finishFileImport);
              } else {
                startBulkPlan(files, false);
              }
            });
          }}
        />
        <input
          aria-label="Choose ZIP"
          ref={zipInputRef}
          type="file"
          multiple
          accept=".zip,application/zip,application/x-zip-compressed"
          style={{ display: 'none' }}
          data-testid="import-zip-input"
          onChange={(e) => {
            const files = snapshotImportFiles(e.target.files);
            if (!files.length || files.some((file) => !file.name.toLowerCase().endsWith('.zip'))) {
              resetBulkPlan();
              setBulkError('Choose a ZIP archive to import its contents.');
            } else {
              startBulkPlan(files, true);
            }
            e.target.value = '';
          }}
        />
        <header className="import-workspace-header">
          <div>
            <div className="import-workspace-title">Import</div>
            <div className="import-workspace-kicker">
              {tableImportContext ? tableImportContext.subtitle : pdfBatchContext ? pdfBatchContext.subtitle : pdfPacketContext.active
                ? pdfPacketContext.subtitle
                : showPreparation ? (showSummary ? 'Imported files' : 'Review text and search setup') : 'Project data intake'}
            </div>
          </div>
          {tableImportContext ? (
            <ImportStageStepper stages={[...tableImportContext.stages]} stage={tableImportContext.stage} />
          ) : pdfBatchContext ? (
            <ImportStageStepper stages={[...pdfBatchContext.stages]} stage={pdfBatchContext.stage} />
          ) : pdfPacketContext.active && !preparationReceipt ? (
            <ImportStageStepper stages={[...PDF_PACKET_STAGES]} stage={pdfPacketContext.stage} />
          ) : null}
          <button
            type="button"
            className="icon-btn"
            aria-label="Close import"
            data-testid="import-workspace-close"
            disabled={closeDisabled}
            onClick={closeWorkspaceFromUser}
          >
            <X size={16} />
          </button>
        </header>
        <div className={`import-workspace-main${pdfPacketContext.active || pdfBatchActive ? ' pdf-packet-workspace-main' : ''}${showPreparation ? ' import-workspace-main-preparation' : ''}`}>
          {!pdfPacketContext.active && !pdfBatchActive && (!tableImportActive || tableImportChoosingSource) && !showPreparation ? (
            <aside className="import-workspace-sidebar">
              <ImportModeRail modes={IMPORT_MODES} mode={railMode} onSelectMode={selectMode} />
            </aside>
          ) : null}
          <main className="import-workspace-content">
            <div className={`import-workspace-panel${mode === 'pdf_split' || pdfBatchActive || tableImportActive || fileEntry || mode === 'feed' ? ' import-workspace-panel-flush' : ''}`}>
              {tableImportActive ? (
                <TableImportFlow
                  projectId={projectId}
                  initialFile={initialTableFile}
                  initialRerunDraftId={initialTableRerunDraftId}
                  preferredDestinationSheetId={initialDestinationSheetId}
                  followTheMoneyEnabled={ftmImportEnabled}
                  destinationSheets={appendDestinationSheets.map((sheet) => ({
                    id: Number(sheet.id),
                    name: sheet.name,
                    rowCount: sheet.rowCount,
                    columns: sheet.columns.map((column) => ({ id: Number(column.id), name: column.name })),
                  }))}
                  onContextChange={setTableImportContext}
                  registerCloseGuard={registerTableImportCloseGuard}
                  onImported={(sheetId) => { onImported(sheetId); closeWorkspace(); }}
                  onDiscarded={closeWorkspace}
                  onError={onError}
                  onUseMultipleFiles={(files) => {
                    setSingleFileFormat('csv');
                    selectMode('files');
                    if (files?.length) startBulkPlan(files, selectionExpandsArchives(files));
                  }}
                  onUsePdfSplit={(file) => {
                    setPdfPacketInitialFile(file);
                    selectMode('pdf_split');
                  }}
                  onUseFollowTheMoney={(file) => {
                    setFtmFile(file);
                    setSingleFileFormat('ftm');
                    setFtmDatasetName('');
                  }}
                />
              ) : pdfBatchActive ? (
                <PdfBatchImportFlow
                  projectId={projectId}
                  files={pdfBatchFiles ?? undefined}
                  resumeDraftId={pdfBatchResumeId}
                  initialDestinationSheetId={destinationSheets.some((sheet) => sheet.parent === undefined && Number(sheet.id) === initialDestinationSheetId) ? initialDestinationSheetId : null}
                  destinationSheets={destinationSheets.filter((sheet) => sheet.parent === undefined).map((sheet) => ({ id: Number(sheet.id), name: sheet.name }))}
                  onContextChange={setPdfBatchContext}
                  registerCloseGuard={registerPdfBatchCloseGuard}
                  onImported={(sheetId) => { onImported(sheetId); closeWorkspace(); }}
                  onDiscarded={closeWorkspace}
                  onError={onError}
                />
              ) : <EntryStep><EntryBody {...(fileEntry ? { className: 'import-entry-body' } : {})}>
              {railMode === 'files' && !showPreparation && <PdfBatchDrafts projectId={projectId} onResume={setPdfBatchResumeId} />}
              {railMode === 'files' && !showPreparation ? (
                <SegmentedToggle
                  fullWidth={false}
                  ariaLabel="File import source"
                  value={mode === 'urls' ? 'urls' : 'files'}
                  onValueChange={(value) => selectMode(value as 'files' | 'urls')}
                  options={[{ value: 'files', label: 'Files' }, { value: 'urls', label: 'URLs' }]}
                  buttonTestId={(value) => `import-source-${value}`}
                  testId="import-source-toggle"
                  disabled={busy || bulkBusy || selectionPreparing}
                />
              ) : null}
              {selectionPreparing ? <div role="status">Preparing selected files…</div> : null}
              {showSummary ? (
                <section className="search-preparation-receipt" data-testid="import-files-summary">
                  <div className="search-preparation-receipt-body">
                    <h2>{summaryHeading}</h2>
                    {fileSummary && <>
                      <p>{fileSummary.count.toLocaleString()} {fileSummary.count === 1 ? 'file' : 'files'}</p>
                      <ul className="import-files-summary-list">
                        {fileSummary.names.map((name, index) => <li key={`${index}:${name}`}>
                          <FileText size={14} aria-hidden />
                          <span>{name}</span>
                        </li>)}
                      </ul>
                      {fileSummary.count > fileSummary.names.length && <p className="form-hint">
                        Showing the first {fileSummary.names.length} filenames.
                      </p>}
                    </>}
                    {bulkReceiptSummary}
                    <ImportSessionProgress
                      status={importSessionStatus}
                      selectedFiles={fileSummary?.count ?? 0}
                      active={importInProgress}
                      complete={importComplete}
                      preparationPending={preparationPending}
                    />
                    {!summaryWaiting && !preparationReceipt?.batchId
                      && importSessionStatus?.state !== 'cancelled'
                      && importSessionStatus?.state !== 'removed' && <p>
                      Files are saved. Search preparation is not available for this import.
                    </p>}
                    {importSessionStatus?.error && <div className="panel-error" role="alert">{importSessionStatus.error}</div>}
                    {preparationFollowupError && <div className="panel-error" role="alert">{preparationFollowupError}</div>}
                  </div>
                  <footer className="search-preparation-actions">
                    {canReviewPreparation ? (
                      <Button type="button" className="btn-primary" data-testid="import-files-next"
                        onClick={() => setPreparationStep('review')}>Next</Button>
                    ) : !summaryWaiting ? (
                      <Button type="button" className="btn-primary" data-testid="search-preparation-done"
                        onClick={closeWorkspace}>Done</Button>
                    ) : null}
                  </footer>
                </section>
              ) : preparationReceipt ? (
                preparationReceipt.batchId ? (
                  <SearchPreparationReceipt
                    key={`${projectId}:${preparationReceipt.sheetId ?? initialPreparationBatchId ?? 'import'}`}
                    projectId={projectId}
                    batchId={preparationReceipt.batchId}
                    importSummary={bulkReceiptSummary}
                    onBack={initialPreparationBatchId ? undefined : () => setPreparationStep('summary')}
                    onBatchChange={(batch) => setPreparationReceipt((current) => current ? {
                      ...current,
                      batchId: current.sheetId === null || current.sheetId === batch.sheet_id
                        ? batch.id : current.batchId,
                      bulkOutputs: current.bulkOutputs?.map((output) => output.sheetId === batch.sheet_id
                        ? { ...output, batchId: batch.id } : output),
                    } : current)}
                    onDone={closeWorkspace}
                    onError={onError}
                  />
                ) : (
                  <section className="search-preparation-receipt" data-testid="search-preparation-receipt">
                    <div className="search-preparation-receipt-body">
                      {bulkReceiptSummary}
                      <p>Files imported. Search preparation is not available for this sheet.</p>
                    </div>
                    <footer className="search-preparation-actions">
                      <Button type="button" className="btn-primary" data-testid="search-preparation-done"
                        onClick={closeWorkspace}>Done</Button>
                    </footer>
                  </section>
                )
              ) : downloadPrompt ? (
                <MediaDownloadPrompt
                  columnName={downloadPrompt.columnName}
                  onRun={runDownloadPrompt}
                  onDismiss={dismissDownloadPrompt}
                />
              ) : nerPrompt ? (
                <NerExtractPrompt
                  offer={nerPrompt}
                  onRun={runNerPrompt}
                  onDismiss={dismissNerPrompt}
                />
              ) : singleFileFormat === 'ftm' && ftmFile ? (
                <section className="table-import-ftm-handoff">
                  <FileText size={26} aria-hidden />
                  <h2>Import FollowTheMoney data</h2>
                  <p>{ftmFile.name}</p>
                  <label className="import-field-label" htmlFor="import-ftm-dataset-name">Dataset name (optional)</label>
                  <TextInput
                    id="import-ftm-dataset-name"

                    data-testid="import-ftm-dataset-name"
                    value={ftmDatasetName}
                    onChange={(event) => setFtmDatasetName(event.currentTarget.value)}
                  />
                  <div className="import-step-actions">
                    <Button type="button" disabled={busy} onClick={() => {
                      setFtmFile(null);
                      setSingleFileFormat('csv');
                    }}>Back</Button>
                    <Button type="button" className="btn-primary" disabled={busy} onClick={() => {
                      void uploadFiles('ftm', [ftmFile], undefined, ftmDatasetName).then(finishFileImport);
                    }}>{busy ? 'Importing…' : 'Import'}</Button>
                  </div>
                </section>
              ) : mode === 'pdf_split' ? (
                <PdfPacketImportFlow
                  initialFile={pdfPacketInitialFile}
                  projectId={projectId}
                  onContextChange={updatePdfPacketContext}
                  registerCloseGuard={registerPdfPacketCloseGuard}
                  onImported={(sheetId) => {
                    onImported(sheetId);
                    closeWorkspace();
                  }}
                  onError={onError}
                  onCancel={closeWorkspaceFromUser}
                />
              ) : mode === 'urls' ? (
                <>
                  <label className="import-field-label" htmlFor="import-url-list">
                    URLs
                  </label>
                  <TextArea
                    id="import-url-list"
                    className="import-url-list"
                    data-testid="import-url-list"
                    rows={8}
                    value={urlText}
                    placeholder="https://example.com/file.pdf"
                    onChange={(e) => dispatch({ type: 'setUrlText', value: e.target.value })}
                  />
                </>
              ) : mode === 'feed' ? (
                <FeedSourceForm onCreated={onClose} onImported={onImported} onError={onError} onCancel={closeWorkspaceFromUser} />
              ) : (
                <>
                  <ImportDropArea title="Drop files here" description="Drop files or a ZIP archive here, or choose a folder."
                    testId="import-files-chooser" disabled={busy || bulkBusy || selectionPreparing} onFiles={handlePickedFiles}>
                    <div className="import-files-chooser-actions">
                      <Button type="button" className="btn-primary" data-testid="import-file-picker" disabled={busy || bulkBusy || selectionPreparing} onClick={() => inputRef.current?.click()}>Pick files</Button>
                      <Button type="button" disabled={busy || bulkBusy || selectionPreparing} onClick={() => folderInputRef.current?.click()}>Choose folder</Button>
                      <Button type="button" disabled={busy || bulkBusy || selectionPreparing} onClick={() => zipInputRef.current?.click()}>Upload ZIP</Button>
                    </div>
                  </ImportDropArea>
                  {(bulkPlan || bulkBusy || bulkError || bulkSuccessMessage || bulkWarnings.length || bulkFailures.length || bulkFailuresOmitted > 0) && (
                    <BulkImportPlanPanel
                      plan={bulkPlan}
                      decisions={bulkDecisions}
                      busy={bulkBusy}
                      error={bulkError}
                      successMessage={bulkSuccessMessage}
                      warnings={bulkWarnings}
                      failures={bulkFailures}
                      failuresOmitted={bulkFailuresOmitted}
                      onDecisionChange={(questionId, value) => {
                        setBulkDecisions((current) => ({ ...current, [questionId]: value }));
                      }}
                      onExecute={executePlannedImport}
                    />
                  )}
                </>
              )}
              </EntryBody>
              {fileEntry && <ImportStepFooter>
                <Button type="button" disabled={closeDisabled} onClick={closeWorkspaceFromUser}>Cancel</Button>
                <span>{mode === 'urls' ? 'Enter URLs to import their contents.' : 'Choose or drop files to start preparing the import.'}</span>
                {mode === 'urls' && <Button type="button" className="btn-primary" data-testid="import-url-submit"
                  disabled={busy} onClick={() => {
                    void uploadUrls(urlText).then((sheetId) => {
                      if (sheetId != null) void finishImport(sheetId);
                    });
                  }}><Link2 size={13} /> Import URLs</Button>}
              </ImportStepFooter>}
              </EntryStep>}
            </div>
          </main>
        </div>
      </section>
      {bulkPrepareDiscardPrompt && (
        <DiscardImportConfirm
          title="Cancel this import?"
          message="Files won’t be added to the project."
          confirmLabel="Cancel import"
          busy={bulkPrepareDiscarding}
          testId="bulk-prepare-discard-dialog"
          onKeep={keepPreparingImport}
          onConfirm={() => void discardPreparingImport()}
        />
      )}
    </dialog>
  );
}
