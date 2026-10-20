// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import type { PreviewSampleResult } from '../../src/api/types';
import { pollPreviewJob } from '../../src/api/previewJobTransport';

const running = { status: 'running', progress: { done: 0, total: 1 } } as PreviewSampleResult;
const done = { ...running, status: 'done' } as PreviewSampleResult;
const ordinary = {
  intervalMs: 750, initialDelayMs: 750, visibility: 'pause' as const,
  maxFailures: 3, cancelOnFailure: false,
};

beforeEach(() => {
  vi.useFakeTimers();
  Object.defineProperty(document, 'hidden', { configurable: true, value: false });
});
afterEach(() => vi.useRealTimers());

function setup() {
  const api = { getPreview: vi.fn().mockResolvedValue(done), cancelPreview: vi.fn().mockResolvedValue(undefined) };
  const controller = new AbortController();
  const onProgress = vi.fn();
  return { api, controller, onProgress };
}

it('retains delayed settle-relative cadence, failure budget reset and terminal cleanup', async () => {
  const { api, controller, onProgress } = setup();
  api.getPreview.mockRejectedValueOnce(new Error('offline')).mockRejectedValueOnce(new Error('offline'))
    .mockResolvedValueOnce(running).mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(done);
  const finished = pollPreviewJob(api, 'ordinary', { ...ordinary, signal: controller.signal }, onProgress);
  await vi.advanceTimersByTimeAsync(749);
  expect(api.getPreview).not.toHaveBeenCalled();
  await vi.advanceTimersByTimeAsync(750 * 4 + 1);
  expect(await finished).toBe(done);
  expect(api.getPreview).toHaveBeenCalledTimes(5);
  expect(onProgress.mock.calls).toEqual([[running], [done]]);
  controller.abort();
  document.dispatchEvent(new Event('visibilitychange'));
  await vi.advanceTimersByTimeAsync(1_500);
  expect(api.cancelPreview).not.toHaveBeenCalled();
  expect(api.getPreview).toHaveBeenCalledTimes(5);
});

it('pauses ordinary previews when hidden, catches up on visibility and never overlaps a read', async () => {
  const { api, controller } = setup();
  Object.defineProperty(document, 'hidden', { configurable: true, value: true });
  let release!: (preview: PreviewSampleResult) => void;
  api.getPreview.mockReturnValueOnce(new Promise((resolve) => { release = resolve; }));
  const finished = pollPreviewJob(api, 'ordinary', { ...ordinary, signal: controller.signal });
  await vi.advanceTimersByTimeAsync(1_500);
  expect(api.getPreview).not.toHaveBeenCalled();
  Object.defineProperty(document, 'hidden', { configurable: true, value: false });
  document.dispatchEvent(new Event('visibilitychange'));
  document.dispatchEvent(new Event('visibilitychange'));
  await vi.advanceTimersByTimeAsync(3_000);
  expect(api.getPreview).toHaveBeenCalledTimes(1);
  release(done);
  expect(await finished).toBe(done);
});

it('cancels once immediately and suppresses a late successful read', async () => {
  const { api, controller, onProgress } = setup();
  let release!: (preview: PreviewSampleResult) => void;
  api.getPreview.mockReturnValueOnce(new Promise((resolve) => { release = resolve; }));
  const finished = pollPreviewJob(api, 'comparison', {
    signal: controller.signal, intervalMs: 250, initialDelayMs: 0,
    visibility: 'continue', maxFailures: 1, cancelOnFailure: true,
  }, onProgress);
  controller.abort();
  expect(await finished).toBeNull();
  expect(api.cancelPreview).toHaveBeenCalledExactlyOnceWith('comparison');
  release(done);
  await Promise.resolve();
  expect(onProgress).not.toHaveBeenCalled();
  expect(vi.getTimerCount()).toBe(0);
});

it.each([false, true])('keeps caller-specific cancel-on-exhaustion policy (%s)', async (cancelOnFailure) => {
  const { api, controller } = setup();
  api.getPreview.mockRejectedValue(new Error('read failed'));
  const failed = pollPreviewJob(api, 'preview', {
    ...ordinary, signal: controller.signal, maxFailures: 1, initialDelayMs: 0, cancelOnFailure,
  });
  await expect(failed).rejects.toThrow('read failed');
  expect(api.cancelPreview).toHaveBeenCalledTimes(cancelOnFailure ? 1 : 0);
  controller.abort();
  expect(api.cancelPreview).toHaveBeenCalledTimes(cancelOnFailure ? 1 : 0);
});
