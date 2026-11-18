import { expect, test, type Page } from '@playwright/test';
import type { GeneratedActionParams } from '../../src/generated/actionTypes';
import { routeV1ActionRun, routeV1ActionStatus } from './actionStatusFixtures';
import {
  clickRunButton,
  createProject,
  importCsv,
  openAction,
  selectActionSourceColumns,
  uniqueName,
  type ActionRequest,
} from './helpers';
import { exposeReplayModel, prepareOwnModelReplay } from './replayModelScenario';

// What the Extract and Classify forms post when they launch. The server
// validates and estimates each draft for real; only the run itself is
// intercepted, so no model is called.

const RUN_ID = 9701;

type Posted<Id extends keyof GeneratedActionParams> = ActionRequest & {
  params: GeneratedActionParams[Id];
};

async function seedProject(page: Page, namePrefix: string): Promise<{ pid: string; sheetId: number }> {
  const pid = await createProject(page.request, uniqueName(namePrefix));
  const sheetId = await importCsv(
    page.request,
    pid,
    'meetings.csv',
    [
      'title,notes',
      '"Budget hearing","Officials discussed vendor bids and transit funds."',
      '"Safety briefing","The police chief named two road closures."',
    ].join('\n'),
  );
  await prepareOwnModelReplay(page.request, pid);
  await exposeReplayModel(page, pid);
  return { pid, sheetId };
}

async function interceptRun(
  page: Page,
  pid: string,
  actionKind: 'map.extract' | 'map.classify',
): Promise<Array<Record<string, unknown>>> {
  const { v1Posts } = await routeV1ActionRun(page, pid, {
    actionKind,
    runId: RUN_ID,
    status: 'completed',
  });
  await routeV1ActionStatus(page, pid, {
    projectId: pid,
    runId: RUN_ID,
    actionKind,
    actionName: actionKind,
    status: 'completed',
    total: 2,
    completed: 2,
  });
  return v1Posts;
}

/** Open the form on the `title` column with the replay model selected. */
async function openFormOnTitle(
  page: Page,
  pid: string,
  sheetId: number,
  actionKind: 'map.extract' | 'map.classify',
): Promise<void> {
  await page.goto(`/p/${pid}/s/${sheetId}`);
  await openAction(page, actionKind);
  await selectActionSourceColumns(page, 'title');
  const trigger = page.getByTestId('engine-selector').locator('.engine-selector__trigger');
  await trigger.click();
  await page.getByTestId('engine-selector-dialog')
    .locator('[data-engine-selector-choice="replay-engine-model"]').click();
  await expect(trigger).toContainText('Gemini 3.5 Flash-Lite');
}

async function runAndReadPost<Id extends 'map.extract' | 'map.classify'>(
  page: Page,
  posts: Array<Record<string, unknown>>,
  actionKind: Id,
): Promise<Posted<Id>> {
  await expect(page.getByTestId('generated-action-run')).toBeEnabled();
  await clickRunButton(page);
  await expect.poll(() => posts.length).toBe(1);
  const posted = posts[0] as unknown as Posted<Id>;
  expect(posted.action_id).toBe(actionKind);
  return posted;
}

test('extract runs clean with fields but no instructions', async ({ page }) => {
  const { pid, sheetId } = await seedProject(page, 'e2e-extract-optional-prompt');
  const posts = await interceptRun(page, pid, 'map.extract');
  await openFormOnTitle(page, pid, sheetId, 'map.extract');

  // The instruction follows the fields it qualifies, and may stay empty.
  const order = await page
    .locator('[data-testid="output-fields-editor"], [data-testid="field-instruction"]')
    .evaluateAll((elements) => elements.map((element) => element.getAttribute('data-testid')));
  expect(order).toEqual(['output-fields-editor', 'field-instruction']);
  await expect(page.getByTestId('field-instruction')).toHaveValue('');

  const posted = await runAndReadPost(page, posts, 'map.extract');
  expect(posted.params.instruction).toBe('');
  expect(posted.params.fields).toEqual([{ name: 'value', type: 'text', description: '' }]);
  await expect(page.getByTestId('error-toast')).toHaveCount(0);
});

test('citations default to "Cite sources": grounded, not required', async ({ page }) => {
  const { pid, sheetId } = await seedProject(page, 'e2e-extract-citation-default');
  const posts = await interceptRun(page, pid, 'map.extract');
  await openFormOnTitle(page, pid, sheetId, 'map.extract');

  // The choice sits in the main form body, outside Advanced.
  await expect(page.getByTestId('field-citation_mode')).toBeVisible();
  await expect(page.getByTestId('field-citation_mode')).toHaveValue('cite');

  const posted = await runAndReadPost(page, posts, 'map.extract');
  expect(posted.params.grounding?.enabled).toBe(true);
  expect(posted.params.grounding?.citation_required ?? false).toBe(false);
  expect(posted.params.evidence_policy?.citation_required ?? false).toBe(false);
});

test('citations "Off" turns grounding off', async ({ page }) => {
  const { pid, sheetId } = await seedProject(page, 'e2e-extract-citation-none');
  const posts = await interceptRun(page, pid, 'map.extract');
  await openFormOnTitle(page, pid, sheetId, 'map.extract');
  await page.getByTestId('field-citation_mode').selectOption('none');

  const posted = await runAndReadPost(page, posts, 'map.extract');
  expect(posted.params.grounding).toMatchObject({ enabled: false, citation_required: false });
  expect(posted.params.evidence_policy).toEqual({ citation_required: false });
});

test('citations "Require citations" grounds, requires, and names the columns to check', async ({
  page,
}) => {
  const { pid, sheetId } = await seedProject(page, 'e2e-extract-citation-require');
  const posts = await interceptRun(page, pid, 'map.extract');
  await openFormOnTitle(page, pid, sheetId, 'map.extract');
  await page.getByTestId('field-citation_mode').selectOption('require');

  const form = page.getByTestId('generated-action-form');
  await form.locator('details.action-advanced > summary').click();
  const sources = page.getByTestId('field-source_document_columns');
  await expect(sources).toBeVisible();
  await sources.click();
  await page.getByTestId('field-source_document_columns-menu')
    .getByRole('option').filter({ hasText: 'notes' }).click();
  await page.keyboard.press('Escape');

  const posted = await runAndReadPost(page, posts, 'map.extract');
  expect(posted.params.grounding).toMatchObject({ enabled: true, citation_required: true });
  // The evidence policy model forbids undeclared keys: exactly this one.
  expect(posted.params.evidence_policy).toEqual({ citation_required: true });
  expect(posted.params.source_document_columns).toEqual(['notes']);
  await expect(page.getByTestId('error-toast')).toHaveCount(0);
});

test('a rejected launch names the field that failed, not just the summary', async ({ page }) => {
  const { pid, sheetId } = await seedProject(page, 'e2e-extract-validation-details');
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    await route.fulfill({
      status: 400,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.action_result.v1',
        action: { kind: 'map.extract', action_id: 'extract-form-polish-error' },
        status: 'error',
        project_id: pid,
        errors: [
          {
            code: 'invalid_params',
            message: 'map.extract params did not validate',
            field: null,
            details: {
              errors: [
                {
                  loc: ['fields', 0, 'type'],
                  msg: 'Value error, invalid_extract_field',
                  type: 'value_error',
                },
              ],
            },
          },
        ],
      }),
    });
  });
  await openFormOnTitle(page, pid, sheetId, 'map.extract');
  await expect(page.getByTestId('generated-action-run')).toBeEnabled();
  await clickRunButton(page);

  const toast = page.getByTestId('error-toast');
  await expect(toast).toBeVisible();
  await expect(toast).toContainText('map.extract params did not validate');
  const details = page.getByTestId('error-toast-details');
  await expect(details).toBeVisible();
  await expect(details).toContainText('fields.0.type');
  await expect(details).toContainText('invalid_extract_field');
});

test('run menu items left-align whether or not they carry an icon', async ({ page }) => {
  const { pid, sheetId } = await seedProject(page, 'e2e-extract-menu-align');
  await openFormOnTitle(page, pid, sheetId, 'map.extract');
  await page.getByTestId('generated-action-run-scope-menu-button').click();
  const menu = page.getByTestId('generated-action-run-scope-menu');
  await expect(menu).toBeVisible();

  const lefts: number[] = [];
  for (const testId of [
    'generated-action-row-scope-all',
    'generated-action-menu-compare',
    'generated-action-row-scope-backfill',
  ]) {
    const box = await menu.getByTestId(testId).locator('> span').first().boundingBox();
    if (!box) throw new Error(`${testId} label is not visible`);
    lefts.push(box.x);
  }
  for (const left of lefts.slice(1)) {
    expect(Math.abs(left - lefts[0])).toBeLessThanOrEqual(1);
  }
});

test('a fresh extract launch leaves confidence off and offers no justification', async ({ page }) => {
  const { pid, sheetId } = await seedProject(page, 'e2e-extract-confidence-default');
  const posts = await interceptRun(page, pid, 'map.extract');
  await openFormOnTitle(page, pid, sheetId, 'map.extract');
  await expect(page.getByTestId('field-include_confidence')).not.toBeChecked();
  await expect(page.getByTestId('field-include_justification')).toHaveCount(0);

  const posted = await runAndReadPost(page, posts, 'map.extract');
  expect(posted.params.include_confidence).toBe(false);
  expect(posted.params).not.toHaveProperty('include_justification');
});

test('a fresh classify launch leaves confidence and justification off', async ({ page }) => {
  const { pid, sheetId } = await seedProject(page, 'e2e-classify-confidence-default');
  const posts = await interceptRun(page, pid, 'map.classify');
  await openFormOnTitle(page, pid, sheetId, 'map.classify');
  await page.getByTestId('classify-labels').fill('budget\nsafety');
  await expect(page.getByTestId('field-context')).toHaveValue('');
  await expect(page.getByTestId('field-include_confidence')).not.toBeChecked();
  await expect(page.getByTestId('field-include_justification')).not.toBeChecked();

  const posted = await runAndReadPost(page, posts, 'map.classify');
  expect(posted.params.context).toBe('');
  expect(posted.params.include_confidence).toBe(false);
  expect(posted.params.include_justification).toBe(false);
});
