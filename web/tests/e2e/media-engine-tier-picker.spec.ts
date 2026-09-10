import { expect, test, type Page } from '@playwright/test';
import {
  clickRunButton,
  createProject,
  listSheets,
  openAction,
  uniqueName,
} from './helpers';

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

async function patchTranscribeEngines(page: Page): Promise<void> {
  await page.route('**/actions/v1/catalog', async (route) => {
    const response = await route.fetch();
    const catalog = await response.json();
    const patched = catalog.actions.map((action: Record<string, unknown>) => {
      if (action.kind !== 'media.transcribe') return action;
      const uiHints = (action.ui_hints as Record<string, unknown> | undefined) ?? {};
      return {
        ...action,
        ui_hints: {
          ...uiHints,
          engines: [
            {
              id: 'faster_whisper',
              label: 'Whisper local proof',
              tier: 'local',
              available: true,
            },
            {
              id: 'faster-whisper',
              label: 'Faster Whisper sidecar proof',
              tier: 'sidecar',
              available: true,
              models: ['small'],
            },
            {
              id: 'remote',
              label: 'Remote speech proof',
              tier: 'hosted',
              billable: true,
              available: false,
              error: 'REMOTE_SPEECH_API_KEY is not configured',
            },
          ],
        },
      };
    });
    await route.fulfill({
      status: response.status(),
      contentType: 'application/json',
      body: JSON.stringify({ ...catalog, actions: patched }),
    });
  });
}

async function stubActionRun(page: Page, pid: string): Promise<Record<string, unknown>[]> {
  const posts: Record<string, unknown>[] = [];
  await page.route(`**/api/projects/${pid}/run`, async (route) => {
    await route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'media engine tier picker should not use legacy /run' }),
    });
  });
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const body = route.request().postDataJSON() as Record<string, unknown>;
    posts.push(body);
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.action_result.v1',
        action: { kind: body.kind, action_id: 'act-web-tier-picker' },
        status: 'completed',
        project_id: pid,
        run_id: 9901,
        receipt_id: 'receipt-web-tier-picker',
        errors: [],
      }),
    });
  });
  await page.route(`**/api/projects/${pid}/actions/runs/*/status`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        run_id: 9901,
        status: 'completed',
        total: 1,
        completed: 1,
        failed: 0,
        cost: 0,
        live: false,
      }),
    });
  });
  return posts;
}

test('media engine picker exposes catalog-backed local sidecar and remote tiers', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-media-engine-tier'));
  const posts = await stubActionRun(page, pid);
  const audioRes = await page.request.post(`/api/projects/${pid}/import/files?sheet_name=audio`, {
    multipart: {
      files: { name: 'clip.wav', mimeType: 'audio/wav', buffer: tinyWav() },
    },
  });
  expect(audioRes.ok()).toBeTruthy();
  const audioSheet = (await listSheets(page.request, pid)).find((sheet) => sheet.name === 'audio');
  expect(audioSheet).toBeTruthy();

  await patchTranscribeEngines(page);
  await page.goto(`/p/${pid}/s/${audioSheet!.id}`);
  await openAction(page, 'media.transcribe');

  // The old
  // tier-chips + PanelSelect combo is gone — ONE searchable dropdown
  // (EnginePicker) grouped by tier, opened from a single trigger button.
  await expect(page.getByTestId('engine-tier-picker')).toBeVisible();
  await expect(page.getByTestId('engine-tier-picker')).toContainText('REMOTE_SPEECH_API_KEY');

  await page.getByTestId('engine-picker-button').click();
  await expect(page.getByTestId('engine-picker-menu')).toBeVisible();
  await expect(page.getByTestId('engine-picker-tier-local')).toContainText('Local');
  await expect(page.getByTestId('engine-picker-tier-sidecar')).toContainText('Sidecar');
  await expect(page.getByTestId('engine-picker-tier-hosted')).toContainText('Hosted');
  await expect(page.getByTestId('engine-picker-tier-hosted-unavailable')).toBeVisible();

  await page.getByTestId('engine-picker-tier-sidecar').click();
  await page.getByTestId('engine-option-faster-whisper').click();
  await expect(page.getByTestId('engine-picker-button')).toContainText('Sidecar');
  await expect(page.getByTestId('engine-picker-button')).toContainText('small');

  await clickRunButton(page);

  await expect.poll(() => posts.length, { timeout: 5000 }).toBe(1);
  expect(posts[0].kind).toBe('media.transcribe');
  expect((posts[0].params as Record<string, unknown>).engine).toBe('faster-whisper');
});
