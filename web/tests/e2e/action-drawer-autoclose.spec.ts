// The action drawer closes once an action has successfully started and rows
// begin coming back.
//
// On the earliest reliable success signal in the launch path (jobStore.startRun
// → handleRunStarted / handleQueuedActionJobStarted, i.e. the run handle
// returns or a queued job is accepted), the action drawer closes. Failures and
// confirm flows KEEP it open (the validation-details + overwrite-confirm flows
// depend on it); Preview keeps it open beside the now-uncovered grid.
// Pairs with new-columns-scroll-annotation-v1 — the scroll+annotation take over
// the feedback role the open drawer used to play.

import { expect, test } from '@playwright/test';
import {
  clickRunButton,
  createProject,
  importCsv,
  openAction,
  runAndWait,
  uniqueName,
} from './helpers';

test('a successful run auto-closes the action drawer', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-autoclose-success'));
  const sheetId = await importCsv(page.request, pid, 'stories.csv', 'story,risk\nAlpha,3\nBeta,5\n');
  await page.goto(`/p/${pid}/s/${sheetId}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  // The local `template` action starts a real run with no model call / no cost
  // gate — the cleanest "successful start" signal.
  await openAction(page, 'map.template');
  const drawer = page.getByTestId('action-drawer');
  await expect(drawer).toBeVisible();
  await drawer.getByTestId('field-template').fill('{{story}} — {{risk}}');
  await drawer.getByTestId('field-output-rendered').fill('rendered');

  const runPost = page.waitForRequest(
    (r) => r.url().includes(`/api/projects/${pid}/actions/v1/run`) && r.method() === 'POST',
  );
  await drawer.getByTestId('generated-action-run').click();
  await runPost;

  // The drawer closes on its own once the run starts.
  await expect(page.getByTestId('action-drawer')).toHaveCount(0, { timeout: 15_000 });
});

test('an accepted preview keeps the drawer open beside its visible grid columns', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-autoclose-preview'));
  const sheetId = await importCsv(page.request, pid, 'stories.csv', 'story,risk\nAlpha,3\nBeta,5\n');
  await page.goto(`/p/${pid}/s/${sheetId}`);
  await expect(page.getByTestId('grid')).toBeVisible({ timeout: 15_000 });

  await openAction(page, 'map.template');
  const drawer = page.getByTestId('action-drawer');
  await drawer.getByTestId('field-template').fill('{{story}} — {{risk}}');
  await drawer.getByTestId('field-output-rendered').fill('rendered');

  await drawer.getByTestId('generated-action-preview').click();

  await expect(page.getByTestId('preview-tab-banner')).toBeVisible();
  await expect(page.getByTestId('action-drawer')).toBeVisible();
});

test('a server output_column_exists rejection keeps the drawer open (confirm flow)', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-autoclose-collision'));
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

  await page.goto(`/p/${pid}/s/${sheetId}`);
  await openAction(page, 'map.extract');
  await page.getByRole('button', { name: 'Add column' }).click();
  await page.getByTestId('output-field-name').fill('officials');
  await expect(page.getByTestId('error-toast')).toHaveCount(0);

  // Create the colliding "officials" column server-side, behind the open form's
  // back, so the launch reaches the server and 409s (same setup as
  // overwrite-server-confirm.spec.ts).
  await runAndWait(page.request, pid, {
    action_id: 'map.regex_extract',
    scope: { kind: 'sheet_rows', sheet_id: sheetId },
    params: {
      input_columns: ['title'],
      pattern: '(.+)',
      group: 0,
    },
    output_names: { extracted: 'officials' },
    idempotency_key: 'e2e-behind-the-back@sha256:autoclose-collision',
  });

  const firstRunRequest = page.waitForResponse(
    (response) =>
      response.url().includes(`/api/projects/${pid}/actions/v1/run`) &&
      response.request().method() === 'POST',
  );
  await clickRunButton(page, { requireCostConfirmation: false });
  expect((await firstRunRequest).status()).toBe(409);

  // The confirm affordance appears AND the drawer is still open — a rejected
  // launch never fires the auto-close.
  await expect(page.getByTestId('overwrite-collision-modal')).toBeVisible();
  await expect(page.getByTestId('action-drawer')).toBeVisible();
});
