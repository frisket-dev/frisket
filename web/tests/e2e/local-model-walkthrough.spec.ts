// Executable companion to LOCAL_MODEL_LAB_WALKTHROUGH. Playwright step names
// come from the same definition rendered by the in-product tour and Markdown
// guide. The fake server speaks Ollama's model-list protocol plus the real
// OpenAI-compatible completion protocol, so CI drives Frisket's actual
// Settings, router, queue worker, and result materialization without installing
// Ollama or downloading a model.

import { createServer, type IncomingMessage, type Server } from 'node:http';
import { expect, test, type Page } from '@playwright/test';

import {
  LOCAL_MODEL_LAB_ENDPOINT_NAME,
  LOCAL_MODEL_LAB_WALKTHROUGH,
  type WalkthroughInstruction,
} from '../../src/walkthrough/walkthroughs';
import {
  createProject,
  listSheets,
  openProject,
  sheetData,
  uniqueName,
} from './helpers';

const MODEL = 'qwen3:0.6b';
const ENDPOINT_NAME = LOCAL_MODEL_LAB_ENDPOINT_NAME;
const EXISTING_ENDPOINT_NAME = 'Existing newsroom server';

type ReturnedProvider = {
  kind: string;
  endpoint_id?: string;
  label?: string;
  origin?: string;
  models?: Array<{ id: string }>;
};

function modelOptionTestId(endpointId: string): string {
  return `model-option-ollama-${endpointId}-qwen3-0-6b`;
}

function authoredEndpointTarget(
  page: Page,
  stepId: string,
) {
  const step = LOCAL_MODEL_LAB_WALKTHROUGH.steps.find((candidate) => candidate.id === stepId);
  if (step?.target.kind !== 'local-endpoint') {
    throw new Error(`${stepId} must use a named local-endpoint target`);
  }
  const anchor = step.target.part === 'row' ? 'local-endpoint-row' : 'local-endpoint-guidance';
  return page.locator(
    `[data-tour="${anchor}"][data-endpoint-label="${step.target.label}"]`,
  );
}

function instructionText(instruction: WalkthroughInstruction): string {
  if (typeof instruction === 'string') return instruction;
  return `${instruction.lead} ${instruction.code}${instruction.explanation ? ` ${instruction.explanation}` : ''}`;
}

async function requestJson(request: IncomingMessage): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = [];
  for await (const chunk of request) chunks.push(Buffer.from(chunk));
  return JSON.parse(Buffer.concat(chunks).toString('utf8')) as Record<string, unknown>;
}

function schemaFromMessages(body: Record<string, unknown>): Record<string, unknown> {
  const messages = Array.isArray(body.messages) ? body.messages : [];
  for (const candidate of messages) {
    if (typeof candidate !== 'object' || candidate === null) continue;
    const message = candidate as { role?: unknown; content?: unknown };
    if (message.role !== 'system' || typeof message.content !== 'string') continue;
    const marker = 'Respond ONLY with JSON matching this schema:\n';
    if (message.content.startsWith(marker)) {
      return JSON.parse(message.content.slice(marker.length)) as Record<string, unknown>;
    }
  }
  throw new Error('completion request did not include Frisket JSON schema guidance');
}

async function startWeakLocalModel(): Promise<{
  server: Server;
  origin: string;
  completions: () => number;
  requestedModels: () => string[];
}> {
  let completionCount = 0;
  const requestedModels: string[] = [];
  const server = createServer(async (request, response) => {
    const url = new URL(request.url ?? '/', 'http://127.0.0.1');
    response.setHeader('content-type', 'application/json');
    if (request.method === 'GET' && url.pathname === '/api/tags') {
      response.end(JSON.stringify({ models: [{ name: MODEL }] }));
      return;
    }
    if (request.method === 'GET' && url.pathname === '/v1/models') {
      response.end(JSON.stringify({ object: 'list', data: [{ id: MODEL }] }));
      return;
    }
    if (request.method === 'POST' && url.pathname === '/v1/chat/completions') {
      completionCount += 1;
      const body = await requestJson(request);
      requestedModels.push(String(body.model));
      const schema = schemaFromMessages(body);
      const properties = (
        typeof schema.properties === 'object' && schema.properties !== null
          ? schema.properties
          : {}
      ) as Record<string, unknown>;
      const outputName = Object.keys(properties)[0];
      if (!outputName) throw new Error('completion schema has no output property');
      const transcript = JSON.stringify(body.messages ?? []);
      let value: string;
      if (outputName === 'small_model_label') {
        value = transcript.includes('MEETING NOTICE')
          ? 'meeting'
          : transcript.includes('INSPECTION REPORT')
            ? 'inspection'
            : 'contract';
      } else {
        // Deterministic weak-model behavior for the teaching boundary: valid
        // structured output, but a confident answer that ignores the row's
        // constraints. The tour compares this with challenge_answer.
        value = 'REQ-000 — This looks like the safest record to review.';
      }
      response.end(JSON.stringify({
        id: `local-lab-${completionCount}`,
        model: MODEL,
        choices: [{ finish_reason: 'stop', message: { role: 'assistant', content: JSON.stringify({ [outputName]: value }) } }],
        usage: { prompt_tokens: 40, completion_tokens: 12 },
      }));
      return;
    }
    response.statusCode = 404;
    response.end(JSON.stringify({ error: 'not found' }));
  });
  await new Promise<void>((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('fake local model did not bind');
  return {
    server,
    origin: `http://127.0.0.1:${address.port}`,
    completions: () => completionCount,
    requestedModels: () => [...requestedModels],
  };
}

async function stopServer(server: Server): Promise<void> {
  await new Promise<void>((resolve, reject) => server.close((error) => (
    error ? reject(error) : resolve()
  )));
}

test('local-model walkthrough connects a named endpoint, succeeds simply, and exposes a hard-task limit', async ({
  page,
}) => {
  test.setTimeout(120_000);
  const localModel = await startWeakLocalModel();
  const existingModel = await startWeakLocalModel();
  const endpointIdsToDelete: string[] = [];
  try {
    const completedSteps: string[] = [];
    const walkthroughStep = async (id: string, body: () => Promise<void>) => {
      const step = LOCAL_MODEL_LAB_WALKTHROUGH.steps.find((candidate) => candidate.id === id);
      if (!step) throw new Error(`Unknown local-model walkthrough step: ${id}`);
      await test.step(`${step.title} — ${instructionText(step.instruction)}`, async () => {
        completedSteps.push(id);
        await body();
      });
    };

    const existingResponse = await page.request.post('/api/providers/local-endpoints', {
      data: {
        display_name: EXISTING_ENDPOINT_NAME,
        origin: existingModel.origin,
      },
    });
    expect(existingResponse.ok()).toBeTruthy();
    const existingCatalog = await existingResponse.json() as { providers?: ReturnedProvider[] };
    const existingEndpoint = (existingCatalog.providers ?? []).find((provider) => (
      provider.kind === 'local_http'
      && provider.label === EXISTING_ENDPOINT_NAME
      && provider.origin === existingModel.origin
    ));
    expect(existingEndpoint?.endpoint_id).toBeTruthy();
    endpointIdsToDelete.push(existingEndpoint!.endpoint_id!);

    const pid = await createProject(page.request, uniqueName('e2e-local-model-walkthrough'));
    const seed = await page.request.post(`/api/projects/${pid}/seed-sample`);
    expect(seed.ok()).toBeTruthy();
    const sheets = await listSheets(page.request, pid);
    const lab = sheets.find((sheet) => sheet.name === 'Small model lab');
    expect(lab).toBeTruthy();
    let endpointId = '';
    // Route to the lab sheet up front so the workbench's initial sheet
    // hydration cannot race the authored tab click back to the first sheet.
    await openProject(page, pid, lab!.id);

    await walkthroughStep('open-local-model-lab', async () => {
      await page.getByTestId(`workbench-mainView-tab-${lab!.id}`).click();
      await expect(page.getByTestId(`workbench-mainView-tab-${lab!.id}`)).toHaveClass(/active/);
    });
    await walkthroughStep('inspect-easy-notices', async () => {
      await expect(page.getByTestId('grid-column-easy_notice')).toBeVisible();
      await expect(page.getByTestId('grid-column-expected_notice_type')).toBeVisible();
    });
    await walkthroughStep('open-local-analyze', async () => {
      await page.getByTestId('ribbon-tab-home').click();
      await expect(page.getByTestId('ribbon-action-map.classify')).toBeVisible();
    });
    await walkthroughStep('open-local-classify', async () => {
      await page.getByTestId('ribbon-action-map.classify').click();
      await expect(page.getByTestId('action-form')).toBeVisible();
    });
    await walkthroughStep('choose-easy-source-preview', async () => {
      await page.getByTestId('classify-source-column-select').selectOption('easy_notice');
    });
    await walkthroughStep('open-local-account-menu', async () => {
      await page.getByTestId('chrome-account').click();
      await expect(page.getByTestId('account-menu')).toBeVisible();
    });
    await walkthroughStep('open-local-account-settings', async () => {
      await page.getByTestId('account-settings').click();
      await expect(page.getByTestId('settings-workspace')).toBeVisible();
    });
    await walkthroughStep('configure-local-providers', async () => {
      await page.getByTestId('settings-nav-personal-ai-providers').click();
      await expect(page.getByTestId('workspace-ai-providers-settings')).toBeVisible();
    });
    await walkthroughStep('open-local-server-form', async () => {
      await page.getByTestId('local-endpoint-add').click();
      await expect(page.getByTestId('local-endpoint-add-form')).toBeVisible();
    });
    await walkthroughStep('name-local-server', async () => {
      await page.getByTestId('local-endpoint-add-name').fill(ENDPOINT_NAME);
    });
    await walkthroughStep('point-at-local-server', async () => {
      await page.getByTestId('local-endpoint-add-origin').fill(localModel.origin);
    });
    await walkthroughStep('save-local-server', async () => {
      const createdResponse = page.waitForResponse((response) => (
        response.url().endsWith('/api/providers/local-endpoints')
        && response.request().method() === 'POST'
      ));
      await page.getByTestId('local-endpoint-add-submit').click();
      const response = await createdResponse;
      expect(response.ok()).toBeTruthy();
      const catalog = await response.json() as { providers?: ReturnedProvider[] };
      const created = (catalog.providers ?? []).find((provider) => (
        provider.kind === 'local_http'
        && provider.label === ENDPOINT_NAME
        && provider.origin === localModel.origin
      ));
      expect(created?.endpoint_id).toBeTruthy();
      endpointId = created!.endpoint_id!;
      endpointIdsToDelete.push(endpointId);
      expect(created?.models?.map((model) => model.id)).toContain(
        `ollama/@${endpointId}/${MODEL}`,
      );
      await expect(page.getByTestId(`local-endpoint-${endpointId}`)).toBeVisible();
    });
    await walkthroughStep('verify-local-reachability', async () => {
      const row = authoredEndpointTarget(page, 'verify-local-reachability');
      await expect(row).toHaveAttribute('data-testid', `local-endpoint-${endpointId}`);
      await expect(row.getByTestId(`local-endpoint-status-${endpointId}`)).toContainText('reachable');
      await expect(row.getByTestId(`local-endpoint-origin-${endpointId}`)).toHaveValue(localModel.origin);
    });
    await walkthroughStep('install-small-model', async () => {
      const row = page.getByTestId(`local-endpoint-${endpointId}`);
      await expect(row.getByRole('checkbox', { name: /allow model downloads/i })).toBeVisible();
      const guidance = authoredEndpointTarget(page, 'install-small-model');
      await expect(guidance).toHaveAttribute(
        'data-testid',
        `local-endpoint-guidance-${endpointId}`,
      );
    });
    await walkthroughStep('return-to-local-project', async () => {
      await page.getByTestId('settings-return-project').click();
      await expect(page.getByTestId('grid')).toBeVisible();
      if (!(await page.getByTestId('grid-column-easy_notice').isVisible())) {
        await page.getByTestId(`workbench-mainView-tab-${lab!.id}`).click();
      }
    });
    await walkthroughStep('reopen-local-analyze', async () => {
      await page.getByTestId('ribbon-tab-home').click();
    });
    await walkthroughStep('reopen-local-classify', async () => {
      await page.getByTestId('ribbon-action-map.classify').click();
      await expect(page.getByTestId('action-form')).toBeVisible();
    });
    await walkthroughStep('choose-easy-source', async () => {
      await page.getByTestId('classify-source-column-select').selectOption('easy_notice');
    });
    await walkthroughStep('choose-local-llm', async () => {
      await expect(page.getByTestId('model-picker-button')).toBeVisible();
    });
    await walkthroughStep('set-easy-labels', async () => {
      await page.getByLabel('Field 1 labels').fill('meeting, inspection, contract');
    });
    await walkthroughStep('name-local-label', async () => {
      await page.getByTestId('new-column-name').fill('small_model_label');
    });
    await walkthroughStep('choose-small-model', async () => {
      await page.getByTestId('model-picker-button').click();
      await page.getByTestId(`model-provider-group-${endpointId}`).click();
      await page.getByTestId(modelOptionTestId(endpointId)).click();
      await expect(page.getByTestId('model-picker-button')).toContainText('qwen3:0.6b');
    });
    await walkthroughStep('run-easy-local-task', async () => {
      await page.getByTestId('run-button').click();
      await expect(page.getByTestId('cost-gate-modal')).toHaveCount(0);
      await expect(page.getByTestId('action-drawer')).toHaveCount(0, { timeout: 20_000 });
    });
    await walkthroughStep('review-easy-local-task', async () => {
      await expect.poll(async () => {
        const data = await sheetData(page.request, pid, lab!.id);
        const expected = data.columns.find((column) => column.name === 'expected_notice_type');
        const actual = data.columns.find((column) => column.name === 'small_model_label');
        if (!expected || !actual) return null;
        return data.rows.map((row) => [
          row.cells[String(expected.id)],
          row.cells[String(actual.id)],
        ]);
      }, { timeout: 40_000 }).toEqual([
        ['meeting', 'meeting'],
        ['inspection', 'inspection'],
        ['contract', 'contract'],
        ['meeting', 'meeting'],
        ['inspection', 'inspection'],
        ['contract', 'contract'],
      ]);
      await expect(page.getByTestId('grid-column-small_model_label')).toBeVisible();
    });
    await walkthroughStep('open-hard-analyze', async () => {
      await page.getByTestId('ribbon-tab-home').click();
      await expect(page.getByTestId('ribbon-action-map.ask')).toBeVisible();
    });
    await walkthroughStep('open-hard-ask', async () => {
      await page.getByTestId('ribbon-action-map.ask').click();
      await expect(page.getByTestId('generated-action-form')).toBeVisible();
    });
    await walkthroughStep('choose-hard-source', async () => {
      const source = page.getByTestId('text-source-columns');
      for (const remove of await source.getByRole('button', { name: /^Remove / }).all()) {
        await remove.click();
      }
      await source.click();
      await page.getByLabel('Filter columns').fill('challenge_text');
      await page.getByRole('option', { name: /challenge_text/ }).click();
    });
    await walkthroughStep('enter-hard-question', async () => {
      await page.getByTestId('field-question').fill(
        'Apply every rule in the text. Return the single requested ID or result, followed by one sentence explaining it.',
      );
    });
    await walkthroughStep('name-hard-answer', async () => {
      await page.getByTestId('field-output-answer').fill('small_model_answer');
    });
    await walkthroughStep('confirm-hard-model', async () => {
      await page.getByTestId('model-picker-button').click();
      await page.getByTestId(`model-provider-group-${endpointId}`).click();
      await page.getByTestId(modelOptionTestId(endpointId)).click();
      await expect(page.getByTestId('model-picker-button')).toContainText('qwen3:0.6b');
    });
    await walkthroughStep('run-hard-local-task', async () => {
      await page.getByTestId('generated-action-run').click();
      await expect(page.getByTestId('cost-gate-modal')).toHaveCount(0);
      await expect(page.getByTestId('action-drawer')).toHaveCount(0, { timeout: 20_000 });
    });
    await walkthroughStep('review-local-limit', async () => {
      await expect.poll(async () => {
        const data = await sheetData(page.request, pid, lab!.id);
        const expected = data.columns.find((column) => column.name === 'challenge_answer');
        const actual = data.columns.find((column) => column.name === 'small_model_answer');
        if (!expected || !actual) return null;
        return data.rows.map((row) => ({
          expected: String(row.cells[String(expected.id)]),
          actual: String(row.cells[String(actual.id)]),
        }));
      }, { timeout: 40_000 }).toEqual([
        { expected: 'MEM-204', actual: 'REQ-000 — This looks like the safest record to review.' },
        { expected: 'INV-447', actual: 'REQ-000 — This looks like the safest record to review.' },
        { expected: 'Bid C', actual: 'REQ-000 — This looks like the safest record to review.' },
        { expected: '$890,000', actual: 'REQ-000 — This looks like the safest record to review.' },
        { expected: '4-3; passed', actual: 'REQ-000 — This looks like the safest record to review.' },
        { expected: '10:06 a.m.', actual: 'REQ-000 — This looks like the safest record to review.' },
      ]);
      await expect(page.getByTestId('grid-column-small_model_answer')).toBeVisible();
    });

    expect(localModel.completions()).toBe(12);
    expect(localModel.requestedModels()).toEqual(Array<string>(12).fill(MODEL));
    expect(existingModel.completions()).toBe(0);
    expect(existingModel.requestedModels()).toEqual([]);
    expect(completedSteps).toEqual(LOCAL_MODEL_LAB_WALKTHROUGH.steps.map((step) => step.id));
  } finally {
    for (const endpointId of endpointIdsToDelete) {
      await page.request.delete(`/api/providers/local-endpoints/${endpointId}`);
    }
    await stopServer(localModel.server);
    await stopServer(existingModel.server);
  }
});
