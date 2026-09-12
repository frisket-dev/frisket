// Copilot chat UI acceptance coverage.
// RED-FIRST acceptance check authored by the check-author, NOT the implementer
// (verifier/generator split — the loop builds the UI to satisfy this; it does
// not write its own check). The backend copilot endpoint already works.
// This asserts that the FRONTEND surfaces the working backend endpoint:
// open the panel, send a message, and get back a proposal card you can Run.
//
// Contract the implementer must meet (testids):
//   chrome-copilot-toggle — the ✧ chrome control that opens the copilot popover
//   copilot-panel    — the panel container (inside the ✧ Focus popover)
//   copilot-input    — the message textbox
//   copilot-send     — send control (Enter may also send)
//   copilot-proposal — a proposal card (>=1 after a column-suggesting message)
//   copilot-run      — the Run control inside a proposal card

import { expect, test } from '@playwright/test';
import { createProject, importCsv, openHistory, sheetColumns, uniqueName } from './helpers';
import {
  selectorChoicesResponse,
  stubSelectorChoices,
  type SelectorGroupFixture,
} from './selectorChoicesFixture';

const CSV =
  'story\n' +
  '"The city council approved a $4M paving contract on Tuesday."\n' +
  '"A local pharmacy in the north district will close by March."\n';

test('copilot selector: interacting with the portaled dialog keeps the panel open', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-copilot-model'));
  await importCsv(page.request, pid, 'stories.csv', CSV);
  const groups: SelectorGroupFixture[] = [{
    id: 'ollama',
    label: 'Ollama',
    choices: [{
      choiceId: 'ollama-qwen',
      label: 'Qwen',
      summary: 'Local model',
      authoredSelection: { kind: 'model', model: 'ollama/qwen' },
    }],
  }, {
    id: 'anthropic',
    label: 'Anthropic',
    choices: [{
      choiceId: 'anthropic-claude-haiku-4-5',
      label: 'Claude Haiku 4.5',
      summary: 'Hosted model',
      authoredSelection: { kind: 'model', model: 'anthropic/claude-haiku-4-5' },
    }],
  }];
  await stubSelectorChoices(page, pid, (subject) => {
    expect(subject.kind).toBe('copilot');
    const model = typeof subject.model === 'string' ? subject.model : 'ollama/qwen';
    return selectorChoicesResponse({
      projectId: pid,
      subject: { kind: 'copilot', model },
      groups,
      currentChoiceId: model === 'anthropic/claude-haiku-4-5'
        ? 'anthropic-claude-haiku-4-5' : 'ollama-qwen',
    });
  });
  await page.goto(`/p/${pid}`);

  await page.getByTestId('chrome-copilot-toggle').click();
  const panel = page.getByTestId('copilot-panel');
  await expect(panel).toBeVisible();
  await expect(page.getByTestId('copilot-model-row')).toBeVisible();

  const trigger = panel.locator('.engine-selector__trigger');
  await trigger.click();
  const dialog = page.getByTestId('engine-selector-dialog');
  await expect(dialog).toBeVisible();

  // The dialog lives at the native top layer. Its provider navigation must
  // not be read as a click outside Copilot's popover.
  await dialog.getByRole('button', { name: 'Ollama', exact: true }).click();
  await expect(dialog).toBeVisible();
  await expect(panel).toBeVisible();

  // Selecting a model closes the dialog but leaves the panel usable.
  await dialog.getByRole('searchbox', { name: 'Search Model' }).fill('Haiku');
  await dialog.locator('[data-engine-selector-choice="anthropic-claude-haiku-4-5"]').click();
  await expect(dialog).not.toBeVisible();
  await expect(panel).toBeVisible();
  await expect(page.getByTestId('copilot-input')).toBeEditable();
});

test('copilot panel sends a message and surfaces a runnable proposal', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-copilot'));
  const sheetId = await importCsv(page.request, pid, 'stories.csv', CSV);
  await page.route(`**/api/projects/${pid}/copilot`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        reply: 'I can add a news beat classifier.',
        needs_import: false,
        proposals: [
          {
            kind: 'map',
            title: 'Add news beat classifier',
            spec: {
              action_id: 'map.classify',
              scope: { kind: 'sheet_rows', sheet_id: sheetId },
              output_names: { beat: 'beat' },
              params: {
                engine: 'llm',
                source: ['story'],
                model: 'anthropic/claude-haiku-4-5',
                context: 'Classify each story by news beat.',
                fields: [{ name: 'beat', type: 'category', labels: ['transit', 'money', 'other'] }],
              },
            },
          },
        ],
      }),
    });
  });
  await page.goto(`/p/${pid}`);

  // open the copilot (fail fast if the entry point doesn't exist — red-first)
  await expect(
    page.getByTestId('chrome-copilot-toggle'),
    'copilot entry point must exist',
  ).toBeVisible({ timeout: 10_000 });
  await page.getByTestId('chrome-copilot-toggle').click();
  const panel = page.getByTestId('copilot-panel');
  await expect(panel).toBeVisible();

  // ask for something that should yield a column proposal
  await page
    .getByTestId('copilot-input')
    .fill('Add a column classifying each story by news beat.');
  await page.getByTestId('copilot-send').click();

  // a proposal card with a Run control comes back
  const proposal = page.getByTestId('copilot-proposal').first();
  await expect(proposal).toBeVisible({ timeout: 60_000 });
  await expect(proposal.getByTestId('copilot-run')).toBeVisible();
  // the proposal references a real, runnable action (non-empty title)
  await expect(proposal).not.toBeEmpty();
});

test('copilot proposal executes through the v1 action run path', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-copilot-run'));
  const sheetId = await importCsv(page.request, pid, 'stories.csv', CSV);
  await page.route(`**/api/projects/${pid}/copilot`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        reply: 'I can add a deterministic note column.',
        needs_import: false,
        proposals: [
          {
            kind: 'map',
            title: 'Add copilot note',
            spec: {
              action_id: 'map.template',
              scope: { kind: 'sheet_rows', sheet_id: sheetId },
              params: { template: { text: 'copilot: {{story}}' } },
              output_names: { rendered: 'copilot_note' },
            },
          },
        ],
      }),
    });
  });

  await page.goto(`/p/${pid}`);
  await page.getByTestId('chrome-copilot-toggle').click();
  await page.getByTestId('copilot-input').fill('Add a note column');
  await page.getByTestId('copilot-send').click();
  await expect(page.getByTestId('copilot-proposal')).toContainText('Add copilot note');

  const [runRequest, runResponse] = await Promise.all([
    page.waitForRequest((request) =>
      request.url().includes(`/api/projects/${pid}/actions/v1/run`) &&
      request.method() === 'POST',
    ),
    page.waitForResponse((response) =>
      response.url().includes(`/api/projects/${pid}/actions/v1/run`) &&
      response.request().method() === 'POST',
    ),
    page.getByTestId('copilot-run').click(),
  ]);
  const posted = runRequest.postDataJSON() as Record<string, unknown>;
  expect(posted).toMatchObject({
    action_id: 'map.template',
    scope: { kind: 'sheet_rows', sheet_id: sheetId },
    params: { template: { text: 'copilot: {{story}}' } },
    output_names: { rendered: 'copilot_note' },
  });
  expect(String(posted.idempotency_key ?? '')).toContain('map.template');
  expect(posted).not.toHaveProperty('schema_version');
  expect(posted).not.toHaveProperty('kind');
  expect(posted).not.toHaveProperty('capabilities');
  const { run_id: runId } = await runResponse.json();
  expect(runId).toBeTruthy();

  await expect(page.getByTestId('run-progress')).toContainText('complete', {
    timeout: 30_000,
  });
  await expect(page.getByTestId('sheet-stats')).toHaveText('2 rows · 2 columns', {
    timeout: 30_000,
  });
  await openHistory(page);
  await expect(page.getByTestId('history-list')).toContainText('template', {
    timeout: 30_000,
  });

  const columns = await sheetColumns(page.request, pid, sheetId);
  expect(columns.some((column) => column.name === 'copilot_note')).toBeTruthy();
});

// The popover header has a chevron
// (copilot-collapse) next to × — collapsed = a header-only strip (thread +
// compose hidden), click again to re-expand. Inspecting a proposal COLLAPSES
// the popover instead of closing it, so the thread survives next to the
// configure-first drawer; the collapsed strip also survives clicks in that
// drawer (no light-dismiss while collapsed).
test('copilot collapses to a header strip; Inspect collapses instead of closing', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-copilot-collapse'));
  const sheetId = await importCsv(page.request, pid, 'stories.csv', CSV);
  await page.route(`**/api/projects/${pid}/copilot`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        reply: 'I can add a deterministic note column.',
        needs_import: false,
        proposals: [
          {
            kind: 'map',
            title: 'Add copilot note',
            spec: {
              action_id: 'map.template',
              scope: { kind: 'sheet_rows', sheet_id: sheetId },
              params: { template: { text: 'copilot: {{story}}' } },
              output_names: { rendered: 'copilot_note' },
            },
          },
        ],
      }),
    });
  });

  await page.goto(`/p/${pid}`);
  await page.getByTestId('chrome-copilot-toggle').click();
  const panel = page.getByTestId('copilot-panel');
  await expect(panel).toBeVisible();

  // Chevron collapse: header-only strip (thread + compose unmounted) …
  await page.getByTestId('copilot-collapse').click();
  await expect(panel).toHaveAttribute('data-collapsed', 'true');
  await expect(page.getByTestId('copilot-input')).toHaveCount(0);
  // … and the same chevron re-expands.
  await page.getByTestId('copilot-collapse').click();
  await expect(panel).not.toHaveAttribute('data-collapsed', 'true');
  await expect(page.getByTestId('copilot-input')).toBeVisible();

  // Get a proposal and Inspect it.
  await page.getByTestId('copilot-input').fill('Add a note column');
  await page.getByTestId('copilot-send').click();
  await expect(page.getByTestId('copilot-proposal')).toContainText('Add copilot note');
  await page.getByTestId('copilot-inspect').click();

  // The configure-first drawer opens; the popover COLLAPSED, not closed.
  await expect(page.getByTestId('action-drawer')).toBeVisible();
  await expect(page.getByTestId('copilot-popover')).toBeVisible();
  await expect(panel).toHaveAttribute('data-collapsed', 'true');
  await expect(page.getByTestId('copilot-input')).toHaveCount(0);

  // The collapsed strip survives interaction with the drawer (no light-dismiss
  // while collapsed) — clicking into the drawer's form leaves it in place.
  await page.getByTestId('action-form-title').click();
  await expect(page.getByTestId('copilot-popover')).toBeVisible();
  await expect(panel).toHaveAttribute('data-collapsed', 'true');

  // Re-expanding shows the same thread (per-mount state, nothing lost).
  await page.getByTestId('copilot-collapse').click();
  await expect(panel).not.toHaveAttribute('data-collapsed', 'true');
  await expect(page.getByTestId('copilot-proposal')).toContainText('Add copilot note');
  await expect(page.getByTestId('copilot-input')).toBeVisible();
});
