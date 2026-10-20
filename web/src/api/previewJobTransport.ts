import type { ProjectApiPort } from './ports';
import type { PreviewSampleResult } from './types';

/** Job transport only. Callers retain launch/consent, result composition and
 * whether a failed read retires the server job or merely its local view. */
export function pollPreviewJob(
  api: Pick<ProjectApiPort, 'getPreview' | 'cancelPreview'>,
  previewId: string,
  policy: {
    signal: AbortSignal;
    intervalMs: number;
    initialDelayMs: number;
    visibility: 'pause' | 'continue';
    maxFailures: number;
    cancelOnFailure: boolean;
  },
  onProgress?: (preview: PreviewSampleResult) => void,
): Promise<PreviewSampleResult | null> {
  return new Promise((resolve, reject) => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    let finished = false;
    let inFlight = false;
    let failures = 0;
    const hidden = () => policy.visibility === 'pause'
      && typeof document !== 'undefined' && document.hidden === true;
    const cleanup = () => {
      finished = true;
      if (timer !== null) clearTimeout(timer);
      policy.signal.removeEventListener('abort', abort);
      if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', onVisibility);
    };
    const cancel = () => { void api.cancelPreview(previewId).catch(() => undefined); };
    function abort() {
      if (finished) return;
      cleanup();
      cancel();
      resolve(null);
    }
    const arm = (delay: number) => { timer = setTimeout(() => void poll(), delay); };
    const poll = async () => {
      timer = null;
      if (finished || hidden() || inFlight) return;
      inFlight = true;
      let preview: PreviewSampleResult;
      try {
        preview = await api.getPreview(previewId);
      } catch (error) {
        inFlight = false;
        if (finished) return;
        if (++failures < policy.maxFailures) arm(policy.intervalMs);
        else {
          cleanup();
          if (policy.cancelOnFailure) cancel();
          reject(error);
        }
        return;
      }
      inFlight = false;
      if (finished) return;
      failures = 0;
      onProgress?.(preview);
      if (finished) return;
      if (preview.status === 'running') arm(policy.intervalMs);
      else {
        cleanup();
        resolve(preview);
      }
    };
    function onVisibility() {
      if (finished || hidden() || inFlight) return;
      if (timer !== null) clearTimeout(timer);
      void poll();
    }
    if (policy.signal.aborted) { abort(); return; }
    policy.signal.addEventListener('abort', abort, { once: true });
    if (policy.visibility === 'pause' && typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', onVisibility);
    }
    if (policy.initialDelayMs === 0) void poll();
    else arm(policy.initialDelayMs);
  });
}
