import { mkdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { expect, test } from '@playwright/test';
import { createProject } from '../e2e/helpers';
import { selectorChoicesResponse } from '../e2e/selectorChoicesFixture';

for (const engine of [
  { id: 'parakeet-tdt', label: 'Parakeet', setup: 'engine-setup:parakeet-tdt.local-onnx@1', display: 'Parakeet TDT (local ONNX)' },
  { id: 'faster_whisper', label: 'Whisper', setup: 'engine-setup:faster-whisper.local@1', display: 'Whisper base (local)' },
]) test(`${engine.label} uses the existing setup component for missing runtime and models`, async ({ page, context }) => {
  await context.route('**/*', route => new URL(route.request().url()).origin === process.env.FRISKET_INVENTORY_APP_URL
    ? route.continue() : route.abort());
  const pid = await createProject(page.request, `${engine.label} setup`);
  const imported = await page.request.post(`/api/projects/${pid}/import/files?sheet_name=interviews`, {
    multipart: { files: { name: 'interview.wav', mimeType: 'audio/wav',
      buffer: readFileSync(path.resolve('tests/fixtures/media/tiny-audio.wav')) } },
  });
  expect(imported.ok()).toBeTruthy();
  const sheets = await (await page.request.get(`/api/projects/${pid}/sheets`)).json();
  const sid = sheets.find((sheet: { name: string }) => sheet.name === 'interviews').id;
  const output = path.resolve('screenshots/speech-tools-setup');
  mkdirSync(output, { recursive: true });
  let state: 'missing' | 'working' | 'ready' = 'missing';
  let starts = 0;
  let runs = 0;
  const pull = () => ({ schemaVersion: 'frisket.model_pull.v4', id: 7002, operation_kind: 'engine_setup',
    display_name: engine.display, model: engine.setup, status: state === 'ready' ? 'done' : 'running',
    capabilities: { cancel: state !== 'ready', retry: false, remove: false }, endpoint_id: null,
    endpoint_origin: null, cancel_requested: false, error: null, created_at: '2026-10-29T00:00:00Z',
    started_at: '2026-10-29T00:00:01Z', finished_at: state === 'ready' ? '2026-10-29T00:01:00Z' : null,
    initiated_by: null, phase: state === 'ready' ? 'done' : 'Installing transcription components',
    completed_bytes: 0, total_bytes: null, resolved_digest: null, resolved_size: null, artifact: null });
  await page.route(`**/api/projects/${pid}/selector-choices`, route => {
    const subject = route.request().postDataJSON().subject;
    const response = selectorChoicesResponse({ projectId: pid, subject, currentChoiceId: engine.id,
      groups: [{ id: 'local', label: 'Local', choices: [{ choiceId: engine.id, label: engine.id === 'faster_whisper'
          ? 'Whisper (multilingual transcription)' : 'Parakeet (fast multilingual transcription)',
        authoredSelection: { kind: 'engine', engine: engine.id },
        status: state === 'ready' ? 'ready' : state === 'working' ? 'working' : 'needs_setup',
        canAuthor: true, canRun: state === 'ready', blocker: state === 'ready' ? null
          : state === 'working' ? 'Engine setup is still in progress.' : engine.id === 'faster_whisper'
            ? 'Set up the local Whisper runtime and base model.'
            : 'Download and set up the local Parakeet runtime and pinned models.',
      }] }] });
    Object.assign(response.groups[0].choices[0], { is_default: true,
      active_operation: state === 'working' ? pull() : null,
      setup: state === 'ready' ? null : { kind: 'engine_setup', scope: 'workspace', setup_ref: engine.setup,
        can_mutate: true, can_start: state === 'missing', blocked_by_operation: null } });
    return route.fulfill({ json: response });
  });
  await page.route('**/api/providers/models/setup', route => {
    starts += 1; state = 'working';
    return route.fulfill({ json: { pull: pull(), deduplicated: false } });
  });
  await page.route('**/api/providers/models/pulls/7002', route => route.fulfill({ json: pull() }));
  await page.route(`**/api/projects/${pid}/actions/v1/run`, route => { runs += 1; return route.abort(); });
  await page.goto(`/p/${pid}/s/${sid}/action/media.transcribe`);
  const form = page.getByTestId('generated-action-form');
  const run = page.getByTestId('generated-action-run');
  await form.getByTestId('engine-selector').getByRole('button').click();
  const dialog = page.getByTestId('engine-selector-dialog');
  await dialog.getByRole('button', { name: new RegExp(engine.label) }).first().click();
  const start = dialog.getByRole('button', { name: 'Download and set up', exact: true });
  await expect(start).toBeVisible();
  await expect(dialog).not.toContainText('pip install');
  await expect(run).toBeDisabled();
  await page.screenshot({ path: path.join(output, `${engine.id}-01-setup.png`) });
  expect(starts).toBe(0);
  await start.click();
  await expect(dialog.getByText('Installing transcription components')).toBeVisible();
  await expect(dialog.getByRole('progressbar')).not.toHaveAttribute('aria-valuenow');
  await page.screenshot({ path: path.join(output, `${engine.id}-02-installing.png`) });
  state = 'ready';
  await expect(dialog.getByText('Ready', { exact: true })).toBeVisible();
  await expect(run).toBeEnabled();
  await expect(dialog.getByRole('button', { name: 'Download and set up', exact: true })).toHaveCount(0);
  await page.screenshot({ path: path.join(output, `${engine.id}-03-ready.png`) });
  expect(starts).toBe(1);
  expect(runs).toBe(0);
});
