import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import type { ActionCatalogPayload, EngineOption } from '../api/open';
import type { PreviewSampleResult } from '../api/types';
import { engineIsRemote } from '../actions/engineCatalog';
import { isUntouchedDefaultSeed, resolveDefaultEngineIds } from './compareDefaultEngines';
import type { ResolvedMediaValue } from '../media/resolveMediaValue';
import { useWorkspaceStores } from '../bind/useWorkspaceStores';
import { useResizable } from '../components/useResizable';
import { diffWords, type DiffToken } from './ocrDiff';

export type MediaKind = 'pdf' | 'image' | 'audio' | 'video' | 'text';
export type Vote = 'neutral' | 'keep' | 'reject';
export type PipState = 'idle' | 'pending' | 'running' | 'results' | 'error';

export type MediaNavTarget =
  | { kind: 'page'; page: number }
  | { kind: 'seconds'; seconds: number | null };

export interface CompareColumn {
  id: string;
  engineId: string | null;
  options: Record<string, unknown>;
}

export interface AlignedUnit {
  key: string;
  marker: string | null;
  navTarget: MediaNavTarget;
  textByColumn: Record<string, string>;
}

export type RunOutcome<R> =
  | { ok: true; results: R; confidence: number | null; preview?: PreviewSampleResult; elapsedMs?: number }
  | { ok: false; message: string };

export type EngineRunState<R> =
  | { status: 'running' }
  | { status: 'done'; results: R; confidence: number | null; preview?: PreviewSampleResult; elapsedMs?: number }
  | { status: 'error'; message: string };

export interface ScratchDoc<R> {
  id: string;
  file: File;
  filename: string;
  objectUrl: string;
  mediaKind: MediaKind;
  pages: number[];
  pageCount: number | null;
  /* Wait for page-count probes before running. */
  probing?: boolean;
  runs: Record<string, EngineRunState<R>>;
  votes: Record<string, Vote>;
}

export interface MediaCompareConfig<R> {
  testidPrefix: string;
  accept: string;
  classifyFile(file: File): MediaKind | null;
  acceptHint: string;
  defaultEngineIds: string[];
  fallbackCatalog: EngineOption[];
  enginesFromCatalog(catalog: ActionCatalogPayload | null | undefined): EngineOption[];
  includeBillableEngines?: boolean;
  prepareRun?(
    pairs: Array<{ doc: ScratchDoc<R>; column: CompareColumn }>,
    signal: AbortSignal,
  ): Promise<boolean>;
  sequential?: boolean;
  probeDoc?(doc: ScratchDoc<R>, patch: (patch: Partial<ScratchDoc<R>>) => void): void | Promise<void>;
  docSecondary(doc: ScratchDoc<R>): string;
  runColumn(
    doc: ScratchDoc<R>,
    column: CompareColumn,
    engine: EngineOption | undefined,
    signal: AbortSignal,
  ): Promise<RunOutcome<R>>;
  unitsForDoc(doc: ScratchDoc<R>, columns: CompareColumn[]): AlignedUnit[];
  autoRun: boolean;
  noDiffUnitLabel: string;
  defaultColumnOptions?: Record<string, unknown>;
}

let docSeq = 0;
let columnSeq = 0;

function makeCompareColumn(
  engineId: string | null,
  options?: Record<string, unknown>,
): CompareColumn {
  columnSeq += 1;
  return { id: `col-${columnSeq}`, engineId, options: { ...options } };
}

function cycleVote(vote: Vote): Vote {
  return vote === 'neutral' ? 'keep' : vote === 'keep' ? 'reject' : 'neutral';
}

export interface DiffModel {
  units: AlignedUnit[];
  leftTokens: DiffToken[][];
  rightTokens: DiffToken[][];
  changedCount: number;
  allCharLevel: boolean;
  tokenNavs: MediaNavTarget[];
}

export interface MediaCompareSession<R> {
  catalog: EngineOption[];
  engineLabel(id: string | null): string;
  columns: CompareColumn[];
  setColumns: React.Dispatch<React.SetStateAction<CompareColumn[]>>;
  runnableColumns: CompareColumn[];
  docs: ScratchDoc<R>[];
  activeDoc: ScratchDoc<R> | null;
  activeDocId: string | null;
  setActiveDocId: React.Dispatch<React.SetStateAction<string | null>>;
  ingestFiles(files: File[]): void;
  mode: 'diff' | 'survey';
  setMode: React.Dispatch<React.SetStateAction<'diff' | 'survey'>>;
  effectiveMode: 'diff' | 'survey';
  diffArmed: boolean;
  diff: DiffModel | null;
  pendingPairs: Array<{ doc: ScratchDoc<R>; column: CompareColumn }>;
  runningCount: number;
  runState: 'disabled' | 'probing' | 'pending' | 'running' | 'fresh';
  runProgress: { done: number; total: number } | null;
  preparing: boolean;
  cancelRuns(): void;
  runPending(): void;
  runAll(): void;
  retryColumn(doc: ScratchDoc<R>, column: CompareColumn): void;
  removeColumn(columnId: string): void;
  invalidateColumn(columnId: string): void;
  updateColumnOptions(columnId: string, patch: Record<string, unknown>): void;
  addBlankColumn(): string;
  addEngineColumn(engineId: string, options?: Record<string, unknown>): string;
  duplicateColumn(columnId: string): string | null;
  chooseEngine(columnId: string, engineId: string): void;

  billableEngines: EngineOption[];

  rejectedDrop: string | null;
  dismissRejectedDrop(): void;
  castVote(columnId: string): void;
  verdict: string;
  hasSessionData: boolean;
  sourceOpen: boolean;
  setSourceOpen: React.Dispatch<React.SetStateAction<boolean>>;
  nav: MediaNavTarget | null;
  navigateTo(target: MediaNavTarget): void;
  jumpToken(delta: number): void;
  wrapText: boolean;
  setWrapText: React.Dispatch<React.SetStateAction<boolean>>;
  peekResize: ReturnType<typeof useResizable>;
  peekMedia: ResolvedMediaValue | null;
}

export const PEEK_MIN_WIDTH = 160;
export const PEEK_MAX_WIDTH = 520;
const PEEK_DEFAULT_WIDTH = 214;

export function useMediaCompareSession<R>(
  config: MediaCompareConfig<R>,
  onSessionChange: (state: { hasData: boolean; verdict: string }) => void,
): MediaCompareSession<R> {
  const { projectApi: api, chromePreferences: { projectId } } = useWorkspaceStores();
  const [rawCatalog, setRawCatalog] = useState<EngineOption[]>(config.fallbackCatalog);

  const catalog = useMemo(
    () =>
      config.includeBillableEngines
        ? rawCatalog
        : rawCatalog.filter((engine) => !engineIsRemote(engine)),
    [config.includeBillableEngines, rawCatalog],
  );
  const billableEngines = useMemo(
    () => rawCatalog.filter((engine) => engineIsRemote(engine)),
    [rawCatalog],
  );
  const [columns, setColumns] = useState<CompareColumn[]>(() =>
    config.defaultEngineIds.map((id) => makeCompareColumn(id, config.defaultColumnOptions)),
  );
  const [docs, setDocs] = useState<ScratchDoc<R>[]>([]);
  const [activeDocId, setActiveDocId] = useState<string | null>(null);
  const [mode, setMode] = useState<'diff' | 'survey'>('diff');
  const [sourceOpen, setSourceOpen] = useState(false);

  const [runProgress, setRunProgress] = useState<{ done: number; total: number } | null>(null);
  const [preparing, setPreparing] = useState(false);
  const [tokenIndex, setTokenIndex] = useState(0);
  const [nav, setNav] = useState<MediaNavTarget | null>(null);
  const [wrapText, setWrapText] = useState(true);
  const [rejectedDrop, setRejectedDrop] = useState<string | null>(null);
  const objectUrls = useRef<string[]>([]);
  const activeBatch = useRef<{
    controller: AbortController;
    pairs: Array<{ doc: ScratchDoc<R>; column: CompareColumn }>;
  } | null>(null);

  const peekResize = useResizable({
    storageKey: `frisket:${config.testidPrefix}-peek-width:${projectId}`,
    minWidth: PEEK_MIN_WIDTH,
    maxWidth: PEEK_MAX_WIDTH,
    defaultWidth: PEEK_DEFAULT_WIDTH,
    handleEdge: 'right',
  });

  const ingested = useRef(false);

  const applyCatalog = useCallback(
    (engines: EngineOption[]) => {
      setRawCatalog(engines);

      setColumns((prev) => {
        if (ingested.current || !isUntouchedDefaultSeed(prev, config.defaultEngineIds)) {
          return prev;
        }
        const eligible = config.includeBillableEngines
          ? engines
          : engines.filter((engine) => !engineIsRemote(engine));
        const resolved = resolveDefaultEngineIds(config.defaultEngineIds, eligible);
        if (isUntouchedDefaultSeed(prev, resolved)) return prev;

        return resolved.map((engineId, index) => ({ ...prev[index], engineId }));
      });
    },
    [config.defaultEngineIds, config.includeBillableEngines],
  );

  useEffect(() => {
    let cancelled = false;

    api
      .listActionCatalog()
      .then((response) => {
        if (cancelled) return;
        applyCatalog(config.enginesFromCatalog(response));
      })
      .catch(() => setRawCatalog(config.fallbackCatalog));
    return () => {
      cancelled = true;
    };

    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(
    () => () => {
      activeBatch.current?.controller.abort();
      for (const url of objectUrls.current) URL.revokeObjectURL(url);
    },
    [],
  );

  const engineLabel = useCallback(
    (id: string | null) =>
      id ? catalog.find((engine) => engine.id === id)?.label ?? id : 'Choose engine…',
    [catalog],
  );

  const runnableColumns = useMemo(
    () => {
      const runnableEngineIds = new Set(
        catalog
          .filter((engine) => engine.available !== false)
          .map((engine) => engine.id),
      );
      return columns.filter(
        (column) => column.engineId && runnableEngineIds.has(column.engineId),
      );
    },
    [catalog, columns],
  );

  const activeDoc = useMemo(
    () => docs.find((doc) => doc.id === activeDocId) ?? docs[0] ?? null,
    [docs, activeDocId],
  );

  const setRunState = useCallback(
    (docId: string, columnId: string, run: EngineRunState<R>) => {
      setDocs((prev) =>
        prev.map((doc) =>
          doc.id === docId ? { ...doc, runs: { ...doc.runs, [columnId]: run } } : doc,
        ),
      );
    },
    [],
  );

  // Fence writes by generation.

  const runGenerations = useRef<Map<string, number> | null>(null);
  runGenerations.current ??= new Map();
  const bumpGeneration = useCallback((columnId: string) => {
    runGenerations.current!.set(columnId, (runGenerations.current!.get(columnId) ?? 0) + 1);
  }, []);

  const bumpProgress = useCallback((signal: AbortSignal) => {
    if (signal.aborted || activeBatch.current?.controller.signal !== signal) return;
    setRunProgress((prev) => {
      if (!prev) return prev;
      const done = prev.done + 1;
      return done >= prev.total ? null : { done, total: prev.total };
    });
  }, []);

  const runColumnForDoc = useCallback(
    async (doc: ScratchDoc<R>, column: CompareColumn, signal: AbortSignal) => {
      const engineId = column.engineId;
      if (!engineId) {
        bumpProgress(signal);
        return;
      }
      const engine = catalog.find((candidate) => candidate.id === engineId);
      const generation = runGenerations.current!.get(column.id) ?? 0;
      const isStale = () =>
        signal.aborted || (runGenerations.current!.get(column.id) ?? 0) !== generation;
      setRunState(doc.id, column.id, { status: 'running' });
      try {
        const outcome = await config.runColumn(doc, column, engine, signal);

        // Drop stale in-flight results.

        if (isStale()) return;
        if (!outcome.ok) {
          setRunState(doc.id, column.id, { status: 'error', message: outcome.message });
          return;
        }
        setRunState(doc.id, column.id, {
          status: 'done',
          results: outcome.results,
          confidence: outcome.confidence,
          preview: outcome.preview,
          elapsedMs: outcome.elapsedMs,
        });
      } catch (error) {
        if (isStale()) return;
        setRunState(doc.id, column.id, {
          status: 'error',
          message: error instanceof Error ? error.message : String(error),
        });
      } finally {
        bumpProgress(signal);
      }
    },
    [catalog, config, setRunState, bumpProgress],
  );

  const pendingPairs = useMemo(
    () =>
      docs.flatMap((doc) =>

        doc.probing
          ? []
          : runnableColumns.flatMap((column) => (doc.runs[column.id] ? [] : [{ doc, column }])),
      ),
    [docs, runnableColumns],
  );

  const probingCount = useMemo(() => docs.filter((doc) => doc.probing).length, [docs]);

  const runningCount = useMemo(
    () =>
      docs.reduce(
        (total, doc) =>
          total +
          runnableColumns.filter((column) => doc.runs[column.id]?.status === 'running').length,
        0,
      ),
    [docs, runnableColumns],
  );

  const runState: MediaCompareSession<R>['runState'] =
    runnableColumns.length === 0
      ? 'disabled'
      : preparing || runningCount > 0
        ? 'running'
        : pendingPairs.length > 0
          ? 'pending'
          :

            probingCount > 0
            ? 'probing'
            : 'fresh';

  const cancelRuns = useCallback(() => {
    const batch = activeBatch.current;
    if (!batch) return;
    batch.controller.abort();
    for (const { column } of batch.pairs) bumpGeneration(column.id);
    activeBatch.current = null;
    setPreparing(false);
    setRunProgress(null);
    setDocs((prev) =>
      prev.map((doc) => {
        const runs = { ...doc.runs };
        let changed = false;
        for (const pair of batch.pairs) {
          if (pair.doc.id === doc.id && runs[pair.column.id]?.status === 'running') {
            delete runs[pair.column.id];
            changed = true;
          }
        }
        return changed ? { ...doc, runs } : doc;
      }),
    );
  }, [bumpGeneration]);

  const runPairs = useCallback(
    (requestedPairs: Array<{ doc: ScratchDoc<R>; column: CompareColumn }>) => {
      if (requestedPairs.length === 0 || activeBatch.current) return;
      const pairs = [...requestedPairs];
      const controller = new AbortController();
      const batch = { controller, pairs };
      activeBatch.current = batch;
      void (async () => {
        try {
          if (config.prepareRun) {
            setPreparing(true);
            const approved = await config.prepareRun(pairs, controller.signal);
            setPreparing(false);
            if (!approved || controller.signal.aborted) return;
          }
          setRunProgress({ done: 0, total: pairs.length });
          if (config.sequential) {
            for (const { doc, column } of pairs) {
              if (controller.signal.aborted) break;
              await runColumnForDoc(doc, column, controller.signal);
            }
          } else {
            await Promise.all(
              pairs.map(({ doc, column }) => runColumnForDoc(doc, column, controller.signal)),
            );
          }
        } catch {
          // Preparation owns any user-facing error; a failed preparation starts no work.
        } finally {
          if (activeBatch.current === batch) {
            activeBatch.current = null;
            setPreparing(false);
            setRunProgress(null);
          }
        }
      })();
    },
    [config, runColumnForDoc],
  );

  const runPending = useCallback(() => runPairs(pendingPairs), [runPairs, pendingPairs]);

  const runAll = useCallback(() => {

    const pairs = docs.flatMap((doc) =>
      doc.probing ? [] : runnableColumns.map((column) => ({ doc, column })),
    );
    runPairs(pairs);
  }, [runPairs, docs, runnableColumns]);

  const retryColumn = useCallback(
    (doc: ScratchDoc<R>, column: CompareColumn) => runPairs([{ doc, column }]),
    [runPairs],
  );

  const autoRunGuard = useRef(false);
  useEffect(() => {
    if (!config.autoRun) return;
    if (pendingPairs.length === 0 || runningCount > 0) {
      autoRunGuard.current = false;
      return;
    }
    if (autoRunGuard.current) return;
    autoRunGuard.current = true;
    runPairs(pendingPairs);
  }, [config.autoRun, pendingPairs, runningCount, runPairs]);

  const invalidateColumn = useCallback(
    (columnId: string) => {

      bumpGeneration(columnId);
      setDocs((prev) =>
        prev.map((doc) => {
          if (!doc.runs[columnId]) return doc;
          const runs = { ...doc.runs };
          delete runs[columnId];
          return { ...doc, runs };
        }),
      );
    },
    [bumpGeneration],
  );

  const removeColumn = useCallback(
    (columnId: string) => {
      // In-flight work must not resurrect removed columns.
      bumpGeneration(columnId);
      setColumns((prev) => prev.filter((column) => column.id !== columnId));
      setDocs((prev) =>
        prev.map((doc) => {
          const runs = { ...doc.runs };
          const votes = { ...doc.votes };
          delete runs[columnId];
          delete votes[columnId];
          return { ...doc, runs, votes };
        }),
      );
    },
    [bumpGeneration],
  );

  const updateColumnOptions = useCallback(
    (columnId: string, patch: Record<string, unknown>) => {
      setColumns((prev) =>
        prev.map((column) =>
          column.id === columnId
            ? { ...column, options: { ...column.options, ...patch } }
            : column,
        ),
      );
      invalidateColumn(columnId);
    },
    [invalidateColumn],
  );

  const addBlankColumn = useCallback(() => {
    const column = makeCompareColumn(null, config.defaultColumnOptions);
    setColumns((prev) => [...prev, column]);
    return column.id;
  }, [config.defaultColumnOptions]);

  const addEngineColumn = useCallback(
    (engineId: string, options?: Record<string, unknown>) => {
      const column = makeCompareColumn(engineId, options ?? config.defaultColumnOptions);
      setColumns((prev) => [...prev, column]);
      return column.id;
    },
    [config.defaultColumnOptions],
  );

  const duplicateColumn = useCallback(
    (columnId: string): string | null => {
      const source = columns.find((column) => column.id === columnId);
      if (!source) return null;

      const copy = makeCompareColumn(source.engineId, { ...source.options });
      setColumns((prev) => {
        const index = prev.findIndex((column) => column.id === columnId);
        if (index < 0) return prev;
        const next = [...prev];
        next.splice(index + 1, 0, copy);
        return next;
      });
      return copy.id;
    },
    [columns],
  );

  const chooseEngine = useCallback(
    (columnId: string, engineId: string) => {

      setColumns((prev) =>
        prev.map((column) => (column.id === columnId ? { ...column, engineId } : column)),
      );
      invalidateColumn(columnId);
    },
    [invalidateColumn],
  );

  const [prevRunnableCount, setPrevRunnableCount] = useState(runnableColumns.length);
  if (runnableColumns.length !== prevRunnableCount) {
    setPrevRunnableCount(runnableColumns.length);
    if (runnableColumns.length === 2) setMode('diff');
  }

  const effectiveMode: 'diff' | 'survey' =
    runnableColumns.length >= 3 ? 'survey' : runnableColumns.length <= 1 ? 'survey' : mode;
  const diffArmed = effectiveMode === 'diff' && runnableColumns.length === 2;
  const bothColumnsDone =
    diffArmed &&
    activeDoc !== null &&
    runnableColumns.every((column) => activeDoc.runs[column.id]?.status === 'done');

  const diff = useMemo<DiffModel | null>(() => {
    if (!bothColumnsDone || !activeDoc) return null;
    const [columnA, columnB] = runnableColumns;
    const units = config.unitsForDoc(activeDoc, [columnA, columnB]);
    const leftTokens: DiffToken[][] = [];
    const rightTokens: DiffToken[][] = [];
    let changedCount = 0;
    let allCharLevel = true;
    const tokenNavs: MediaNavTarget[] = [];
    for (const unit of units) {
      const unitDiff = diffWords(
        unit.textByColumn[columnA.id] ?? '',
        unit.textByColumn[columnB.id] ?? '',
      );
      leftTokens.push(unitDiff.left);
      rightTokens.push(unitDiff.right);
      changedCount += unitDiff.changedCount;
      if (!unitDiff.allCharLevel) allCharLevel = false;
      for (const token of unitDiff.left) if (token.changed) tokenNavs.push(unit.navTarget);
    }
    return { units, leftTokens, rightTokens, changedCount, allCharLevel, tokenNavs };
  }, [bothColumnsDone, activeDoc, runnableColumns, config]);

  const verdict = useMemo(() => {
    const tallies = runnableColumns.map((column) => {
      let keeps = 0;
      let rejects = 0;
      for (const doc of docs) {
        if (doc.votes[column.id] === 'keep') keeps += 1;
        if (doc.votes[column.id] === 'reject') rejects += 1;
      }
      return { column, keeps, rejects };
    });
    const voted = tallies.filter((tally) => tally.keeps > 0 || tally.rejects > 0);
    if (voted.length === 0) return 'No votes cast yet.';
    return voted
      .map(
        (tally) =>
          `${engineLabel(tally.column.engineId)} ${tally.keeps > 0 ? `✓${tally.keeps}` : ''}${
            tally.rejects > 0 ? ` ✗${tally.rejects}` : ''
          }`.trim(),
      )
      .join(' · ');
  }, [runnableColumns, docs, engineLabel]);

  const hasSessionData = useMemo(
    () => docs.length > 0 || docs.some((doc) => Object.keys(doc.votes).length > 0),
    [docs],
  );

  // Defer ancestor updates until after descendant render.
  useEffect(() => {
    onSessionChange({ hasData: hasSessionData, verdict });
  }, [hasSessionData, verdict, onSessionChange]);

  const castVote = useCallback(
    (columnId: string) => {
      if (!activeDoc) return;
      setDocs((prev) =>
        prev.map((doc) =>
          doc.id === activeDoc.id
            ? {
                ...doc,
                votes: {
                  ...doc.votes,
                  [columnId]: cycleVote(doc.votes[columnId] ?? 'neutral'),
                },
              }
            : doc,
        ),
      );
    },
    [activeDoc],
  );

  const ingestFiles = useCallback(
    (files: File[]) => {
        // Validate drops because they bypass the input accept filter.
      const rejected = files.filter((file) => config.classifyFile(file) === null);
      const accepted = files.length - rejected.length;
      setRejectedDrop(
        rejected.length === 0
          ? null
          : `Skipped ${rejected.map((file) => file.name).join(', ')} — ${config.acceptHint}`,
      );
      if (accepted === 0) return;
      for (const file of files) {
        const mediaKind = config.classifyFile(file);
        if (!mediaKind) continue;
        ingested.current = true;
        const objectUrl = URL.createObjectURL(file);
        objectUrls.current.push(objectUrl);
        docSeq += 1;
        const id = `doc-${docSeq}`;
        const doc: ScratchDoc<R> = {
          id,
          file,
          filename: file.name,
          objectUrl,
          mediaKind,
          pages: [1],
          pageCount: null,

          probing: Boolean(config.probeDoc),
          runs: {},
          votes: {},
        };
        setDocs((prev) => [...prev, doc]);
        setActiveDocId((prev) => prev ?? id);
        const patchDoc = (patch: Partial<ScratchDoc<R>>) =>
          setDocs((prev) =>
            prev.map((existing) => (existing.id === id ? { ...existing, ...patch } : existing)),
          );
        const probe = config.probeDoc?.(doc, patchDoc);
        if (config.probeDoc) {

          Promise.resolve(probe).finally(() => patchDoc({ probing: false }));
        }
      }
    },
    [config],
  );

  const dismissRejectedDrop = useCallback(() => setRejectedDrop(null), []);

  const navigateTo = useCallback((target: MediaNavTarget) => {
    setNav(target);
    setSourceOpen(true);
  }, []);

  const jumpToken = useCallback(
    (delta: number) => {
      if (!diff || diff.tokenNavs.length === 0) return;
      const next = (tokenIndex + delta + diff.tokenNavs.length) % diff.tokenNavs.length;
      setTokenIndex(next);
      navigateTo(diff.tokenNavs[next]);
    },
    [diff, tokenIndex, navigateTo],
  );

  const activeDocObjectUrl = activeDoc?.objectUrl;
  const activeDocFilename = activeDoc?.filename;
  const activeDocMediaKind = activeDoc?.mediaKind;
  const activeDocFile = activeDoc?.file;
  const peekMedia: ResolvedMediaValue | null = useMemo(
    () =>
      activeDocObjectUrl !== undefined &&
      activeDocFilename !== undefined &&
      activeDocMediaKind !== undefined &&
      activeDocFile !== undefined
        ? {
            url: activeDocObjectUrl,
            label: activeDocFilename,
            filename: activeDocFilename,
            mime: activeDocMediaKind === 'pdf' ? 'application/pdf' : activeDocFile.type,
          }
        : null,
    [activeDocObjectUrl, activeDocFilename, activeDocMediaKind, activeDocFile],
  );

  return {
    catalog,
    engineLabel,
    columns,
    setColumns,
    runnableColumns,
    docs,
    activeDoc,
    activeDocId,
    setActiveDocId,
    ingestFiles,
    mode,
    setMode,
    effectiveMode,
    diffArmed,
    diff,
    pendingPairs,
    runningCount,
    runState,
    runProgress,
    preparing,
    cancelRuns,
    runPending,
    runAll,
    retryColumn,
    removeColumn,
    invalidateColumn,
    updateColumnOptions,
    addBlankColumn,
    addEngineColumn,
    duplicateColumn,
    chooseEngine,
    billableEngines,
    rejectedDrop,
    dismissRejectedDrop,
    castVote,
    verdict,
    hasSessionData,
    sourceOpen,
    setSourceOpen,
    nav,
    navigateTo,
    jumpToken,
    wrapText,
    setWrapText,
    peekResize,
    peekMedia,
  };
}

export function variantPip(
  column: CompareColumn,
  run: EngineRunState<unknown> | undefined,
): PipState {
  if (!column.engineId) return 'pending';
  if (!run) return 'pending';
  if (run.status === 'running') return 'running';
  if (run.status === 'error') return 'error';
  return 'results';
}

export const VARIANT_PIP_GLYPH: Record<Exclude<PipState, 'running'>, string> = {
  idle: '',
  pending: '◷',
  results: '✓',
  error: '!',
};

export const MEDIA_LANGUAGE_OPTIONS: Array<{ value: string; label: string }> = [
  { value: '', label: 'Auto' },
  { value: 'eng', label: 'English' },
  { value: 'fra', label: 'French' },
  { value: 'deu', label: 'German' },
  { value: 'spa', label: 'Spanish' },
];
