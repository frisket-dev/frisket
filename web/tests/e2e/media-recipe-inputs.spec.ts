// RED-FIRST acceptance for media-recipe-input-binding-ui:
// OCR/transcribe expose typed source-column pickers and submit a single
// compatible input_columns value without generic LLM prompt/schema controls.

import { expect, test, type Page } from '@playwright/test';
import {
  clickRunButton,
  createProject,
  listSheets,
  openAction,
  uniqueName,
} from './helpers';

const PNG_1X1 = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=',
  'base64',
);

function tinyWav(): Buffer {
  const b = Buffer.alloc(44);
  b.write('RIFF', 0);
  b.writeUInt32LE(36, 4);
  b.write('WAVE', 8);
  b.write('fmt ', 12);
  b.writeUInt32LE(16, 16);
  b.writeUInt16LE(1, 20);
  b.writeUInt16LE(1, 22);
  b.writeUInt32LE(8000, 24);
  b.writeUInt32LE(16000, 28);
  b.writeUInt16LE(2, 32);
  b.writeUInt16LE(16, 34);
  b.write('data', 36);
  b.writeUInt32LE(0, 40);
  return b;
}

type CapturedRunPost = {
  endpoint: 'v1';
  body: Record<string, unknown>;
};

async function stubRuns(page: Page, pid: string): Promise<CapturedRunPost[]> {
  const posts: CapturedRunPost[] = [];
  let runId = 9100;
  await page.route(`**/api/projects/${pid}/run`, async (route) => {
    await route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'media inputs should not use legacy /run' }),
    });
  });
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const body = route.request().postDataJSON() as Record<string, unknown>;
    posts.push({ endpoint: 'v1', body });
    runId += 1;
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.action_result.v1',
        action: { kind: body.kind, action_id: `act-${runId}` },
        status: 'completed',
        project_id: pid,
        run_id: runId,
        receipt_id: `receipt-${runId}`,
        errors: [],
      }),
    });
  });
  await page.route(`**/api/projects/${pid}/actions/runs/*/status`, async (route) => {
    const match = route.request().url().match(/\/runs\/([^/]+)\/status$/);
    const id = Number(match?.[1] ?? runId);
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.action_run_status.v1',
        project_id: pid,
        run_id: id,
        action_kind: 'media.ocr',
        action_name: 'Media action',
        status: 'completed',
        total: 1,
        completed: 1,
        failed: 0,
        cost: 0,
        live: false,
        public_status: {
          run_id: id,
          status: 'completed',
          total: 1,
          completed: 1,
          failed: 0,
          cost: 0,
          live: false,
        },
      }),
    });
  });
  return posts;
}

async function optionLabels(page: Page): Promise<string[]> {
  return page
    .getByTestId('media-source-column-select')
    .locator('option')
    .evaluateAll((nodes) => nodes.map((node) => node.textContent ?? ''));
}

test('OCR and transcribe bind a typed media source column without LLM prompt/schema controls', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-media-inputs'));
  const posts = await stubRuns(page, pid);

  const imageRes = await page.request.post(`/api/projects/${pid}/import/files?sheet_name=images`, {
    multipart: {
      files: { name: 'scan.png', mimeType: 'image/png', buffer: PNG_1X1 },
    },
  });
  expect(imageRes.ok()).toBeTruthy();
  const audioRes = await page.request.post(`/api/projects/${pid}/import/files?sheet_name=audio`, {
    multipart: {
      files: { name: 'clip.wav', mimeType: 'audio/wav', buffer: tinyWav() },
    },
  });
  expect(audioRes.ok()).toBeTruthy();

  const sheets = await listSheets(page.request, pid);
  const imageSheet = sheets.find((sheet) => sheet.name === 'images');
  const audioSheet = sheets.find((sheet) => sheet.name === 'audio');
  expect(imageSheet).toBeTruthy();
  expect(audioSheet).toBeTruthy();

  await page.goto(`/p/${pid}/s/${imageSheet!.id}`);
  await openAction(page, 'media.ocr');
  await expect(page.getByTestId('media-source-column-select')).toBeVisible();
  await expect.poll(() => optionLabels(page)).toEqual(['media (image)']);
  await expect(page.getByTestId('action-prompt')).toHaveCount(0);
  await expect(page.getByTestId('output-fields-label')).toHaveCount(0);
  await page.getByTestId('run-button').click();
  await expect.poll(() => posts.length).toBe(1);
  expect(posts[0].endpoint).toBe('v1');
  expect(posts[0].body.kind).toBe('media.ocr');
  expect((posts[0].body.params as Record<string, unknown>).input_columns).toEqual(['media']);

  await page.goto(`/p/${pid}/s/${audioSheet!.id}`);
  await openAction(page, 'media.transcribe');
  await expect(page.getByTestId('media-source-column-select')).toBeVisible();
  await expect.poll(() => optionLabels(page)).toEqual(['media (audio)']);
  await expect(page.getByTestId('action-prompt')).toHaveCount(0);
  await expect(page.getByTestId('output-fields-label')).toHaveCount(0);
  await clickRunButton(page, { requireCostConfirmation: false });
  await expect.poll(() => posts.length).toBe(2);
  expect(posts[1].endpoint).toBe('v1');
  expect(posts[1].body.kind).toBe('media.transcribe');
  expect((posts[1].body.params as Record<string, unknown>).input_columns).toEqual(['media']);
});
