import { expect, test, type Page } from '@playwright/test';
import {
  clickRunButton,
  createProject,
  listSheets,
  openAction,
  uniqueName,
} from './helpers';
import {
  actionSelectorResponse,
  stubActionSelectorChoices,
  type SelectorGroupFixture,
} from './selectorChoicesFixture';

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

async function stubActionRun(page: Page, pid: string): Promise<Record<string, unknown>[]> {
  const posts: Record<string, unknown>[] = [];
  await page.route(`**/api/projects/${pid}/run`, async (route) => {
    await route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'media selector must use the action v1 run endpoint' }),
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
        action: { kind: body.action_id, action_id: 'act-web-tier-picker' },
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

async function stubTranscribeValidation(page: Page, pid: string): Promise<void> {
  await page.route(`**/api/projects/${pid}/actions/v1/validate-params`, async (route) => {
    const body = route.request().postDataJSON() as { action?: { action_id?: string } };
    expect(body.action?.action_id).toBe('media.transcribe');
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.action_param_validation_result.v1',
        action: { action_id: 'media.transcribe', kind: 'media.transcribe' },
        project_id: pid,
        diagnostics: {},
        logical_outputs: [{ key: 'transcript', column_type: 'text', existing_column_policy: 'generated' }],
        creates_sheet: false,
      }),
    });
  });
}

test('media transcribe selects a ready sidecar choice through selector choices before running', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-media-engine-tier'));
  const posts = await stubActionRun(page, pid);
  await stubTranscribeValidation(page, pid);
  const audioRes = await page.request.post(`/api/projects/${pid}/import/files?sheet_name=audio`, {
    multipart: {
      files: { name: 'clip.wav', mimeType: 'audio/wav', buffer: tinyWav() },
    },
  });
  expect(audioRes.ok()).toBeTruthy();
  const audioSheet = (await listSheets(page.request, pid)).find((sheet) => sheet.name === 'audio');
  expect(audioSheet).toBeTruthy();

  const groups: SelectorGroupFixture[] = [
    {
      id: 'local',
      label: 'Local',
      choices: [{
        choiceId: 'faster-whisper',
        label: 'Faster Whisper sidecar proof',
        summary: 'Runs on this computer',
        description: 'A ready local sidecar transcription engine.',
        authoredSelection: { kind: 'engine', engine: 'faster-whisper' },
      }],
    },
    {
      id: 'remote',
      label: 'Remote',
      choices: [{
        choiceId: 'remote',
        label: 'Remote speech proof',
        summary: 'Requires provider setup',
        status: 'unavailable',
        canAuthor: false,
        canRun: false,
        blocker: 'REMOTE_SPEECH_API_KEY is not configured',
        authoredSelection: { kind: 'engine', engine: 'remote' },
      }],
    },
  ];
  await stubActionSelectorChoices(page, pid, ({ actionId, field, params }) => {
    expect(actionId).toBe('media.transcribe');
    expect(field).toBe('engine');
    const currentChoiceId = params.engine === 'faster-whisper' ? 'faster-whisper' : 'remote';
    return actionSelectorResponse({
      projectId: pid,
      actionId,
      field,
      groups,
      currentChoiceId,
      orphanedCurrent: currentChoiceId === 'remote' ? groups[1].choices[0] : null,
    });
  });

  await page.goto(`/p/${pid}/s/${audioSheet!.id}`);
  await openAction(page, 'media.transcribe');

  const field = page.getByTestId('field-engine');
  const trigger = field.locator('.engine-selector__trigger');
  await expect(trigger).toContainText('Remote speech proof');
  await trigger.click();
  const dialog = page.getByTestId('engine-selector-dialog');
  await expect(dialog).toHaveJSProperty('open', true);
  await dialog.getByRole('searchbox', { name: 'Search Engine' }).fill('Faster Whisper');
  await dialog.locator('[data-engine-selector-choice="faster-whisper"]').click();
  await expect(dialog).toHaveCount(0);
  await expect(trigger).toContainText('Faster Whisper sidecar proof');

  await clickRunButton(page);

  await expect.poll(() => posts.length, { timeout: 5000 }).toBe(1);
  expect(posts[0].action_id).toBe('media.transcribe');
  expect((posts[0].params as Record<string, unknown>).engine).toBe('faster-whisper');
});
