import { useCallback, useRef } from 'react';

import type {
  PdfImportSnapshot,
  PdfImportUpload,
  PdfImportsApi,
} from '../../../api/pdfImports';

interface SelectedPdf {
  file: File;
  fileId: string;
  logicalPath: string;
  batchId: string;
}

interface IntakeSelection {
  files: readonly File[];
  expectedBytes: number;
  sourceLabel: string;
}

interface ReselectionOptions {
  shouldPublish(): boolean;
  refresh(): Promise<void>;
}

export interface ActivePdfBatchIntake {
  abort(): void;
  done: Promise<void>;
}

/** Owns the one browser-side manifest/upload pipeline for a mounted PDF import flow. */
export function usePdfBatchIntake({
  api,
  projectId,
  publish,
}: {
  api: PdfImportsApi;
  projectId: string;
  publish(snapshot: PdfImportSnapshot): void;
}) {
  const controller = useRef<AbortController | null>(null);
  const running = useRef<Promise<void> | null>(null);
  const selected = useRef<SelectedPdf[]>([]);
  const manifestBatchIds = useRef<string[]>([]);
  const uploadedFileIds = useRef<Set<string>>(new Set());

  const configure = useCallback((files: readonly File[]): IntakeSelection => {
    selected.current = files.map((file) => ({
      file,
      fileId: crypto.randomUUID(),
      logicalPath: file.webkitRelativePath || file.name,
      batchId: crypto.randomUUID(),
    }));
    uploadedFileIds.current = new Set();
    manifestBatchIds.current = Array.from(
      { length: Math.ceil(files.length / 256) },
      () => crypto.randomUUID(),
    );
    const folder = files[0]?.webkitRelativePath?.split('/')[0] || null;
    return {
      files,
      expectedBytes: files.reduce((sum, file) => sum + file.size, 0),
      sourceLabel: folder ?? `${files.length} PDFs`,
    };
  }, []);

  const stop = useCallback(async () => {
    controller.current?.abort();
    const active = running.current;
    if (active) await active.catch(() => undefined);
  }, []);

  const run = useCallback(async (task: (signal: AbortSignal) => Promise<void>) => {
    const prior = running.current;
    if (prior) {
      controller.current?.abort();
      await prior.catch(() => undefined);
    }
    const nextController = new AbortController();
    controller.current = nextController;
    const active = task(nextController.signal);
    running.current = active;
    try {
      await active;
    } finally {
      if (running.current === active) running.current = null;
      if (controller.current === nextController) controller.current = null;
    }
  }, []);

  const runFresh = useCallback(async (
    source: PdfImportSnapshot,
    shouldPublish: () => boolean,
  ) => {
    await run(async (signal) => {
      let latest = source;
      if (!latest.manifest.sealed) {
        for (let start = 0, index = 0; start < selected.current.length; start += 256, index += 1) {
          const manifest = selected.current.slice(start, start + 256).map((item) => ({
            client_file_id: item.fileId,
            logical_path: item.logicalPath,
            filename: item.file.name,
            size: item.file.size,
          }));
          latest = await api.addManifest(
            projectId,
            source.draft_id,
            manifest,
            manifestBatchIds.current[index],
            { signal },
          );
          if (shouldPublish()) publish(latest);
        }
        latest = await api.sealManifest(projectId, source.draft_id, { signal });
        if (shouldPublish()) publish(latest);
      }
      for (const item of selected.current) {
        if (uploadedFileIds.current.has(item.fileId)) continue;
        const uploaded = await api.upload(
          projectId,
          source.draft_id,
          [{ fileId: item.fileId, file: item.file, logicalPath: item.logicalPath }],
          item.batchId,
          { signal },
        );
        uploadedFileIds.current.add(item.fileId);
        if (shouldPublish()) publish(uploaded);
      }
    });
  }, [api, projectId, publish, run]);

  const runReselection = useCallback(async (
    source: PdfImportSnapshot,
    files: readonly File[],
    options: ReselectionOptions,
  ) => {
    await run(async (signal) => {
      if (source.paused) {
        const resumed = await api.resume(projectId, source.draft_id, { signal });
        if (options.shouldPublish()) publish(resumed);
      }
      const pending: PdfImportUpload[] = [];
      for (let start = 0; start < files.length; start += 256) {
        const batch = files.slice(start, start + 256);
        const matched = await api.reconcileReselection(
          projectId,
          source.draft_id,
          batch.map((file) => ({
            logical_path: file.webkitRelativePath || file.name,
            filename: file.name,
            size: file.size,
          })),
          { signal },
        );
        matched.items.forEach((item, index) => {
          if (item.matched && item.upload_needed && item.occurrence_id) {
            pending.push({ fileId: item.occurrence_id, file: batch[index], logicalPath: item.logical_path });
          }
        });
      }
      for (const item of pending) {
        const uploaded = await api.upload(
          projectId, source.draft_id, [item], crypto.randomUUID(), { signal },
        );
        if (options.shouldPublish()) publish(uploaded);
      }
      if (options.shouldPublish()) await options.refresh();
    });
  }, [api, projectId, publish, run]);

  const files = useCallback(() => selected.current.map((item) => item.file), []);
  const abort = useCallback(() => controller.current?.abort(), []);
  const active = useCallback((): ActivePdfBatchIntake | null => {
    const ownedController = controller.current;
    const done = running.current;
    if (!ownedController || !done) return null;
    return { abort: () => ownedController.abort(), done };
  }, []);

  return {
    configure,
    files,
    runFresh,
    runReselection,
    stop,
    abort,
    active,
  };
}
