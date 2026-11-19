import { expect, test } from '@playwright/test';
import {
  clickRunButton,
  createProject,
  importCsv,
  openAction,
  selectActionSourceColumns,
  runAndWait,
  uniqueName,
} from './helpers';
import { exposeReplayModel, prepareOwnModelReplay } from './replayModelScenario';

// A run that would write into an existing generated column must offer a
// confirm prompt, even when the form's column list is stale: the server's
// output_column_exists rejection (HTTP 409) opens the same replace-confirm
// modal the form uses, and confirming resubmits with replace_existing. The
// drawer also refreshes the sheet's columns when it opens.
//
// The first spec creates the column through the API while the extract form
// is open, so only the server knows about it.

test('a server-side output_column_exists rejection converts into an overwrite-confirm modal, and accepting relaunches with overwrite:true', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-overwrite-server-confirm'));
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

  await page.goto(`/p/${pid}/s/${sheetId}`);
  await openAction(page, 'map.extract');
  await selectActionSourceColumns(page, 'notes');
  const engine = page.getByTestId('engine-selector').locator('.engine-selector__trigger');
  await engine.click();
  await page.getByTestId('engine-selector-dialog')
    .locator('[data-engine-selector-choice="replay-engine-model"]').click();
  await expect(engine).toContainText('Gemini 3.5 Flash-Lite');
  // The extract form opens with one output field; name it.
  await page.getByTestId('output-field-name').fill('officials');
  await expect(page.getByTestId('error-toast')).toHaveCount(0);

  // Behind the form's back: a direct API call (not this tab's own run
  // lifecycle) creates an AI-generated "officials" column server-side. The
  // already-open form's `sheet.columns` snapshot never observes this.
  await runAndWait(page.request, pid, {
    action_id: 'map.regex_extract',
    scope: { kind: 'sheet_rows', sheet_id: sheetId },
    params: {
      input_columns: ['title'],
      pattern: '(.+)',
      group: 0,
    },
    output_names: { extracted: 'officials' },
    idempotency_key: 'e2e-behind-the-back@sha256:overwrite-server-confirm',
  });

  // The client-side collision check never fires (its sheet.columns snapshot
  // is stale) — the launch proceeds to the server, which 409s.
  const firstRunRequest = page.waitForResponse(
    (response) =>
      response.url().includes(`/api/projects/${pid}/actions/v1/run`) &&
      response.request().method() === 'POST',
  );
  await clickRunButton(page, { requireCostConfirmation: false });
  const firstResponse = await firstRunRequest;
  expect(firstResponse.status()).toBe(409);

  // The dead-end toast is gone — the SAME overwrite-confirm affordance
  // appears instead.
  const modal = page.getByTestId('overwrite-collision-modal');
  await expect(modal).toBeVisible();
  await expect(modal).toContainText('officials');
  await expect(page.getByTestId('error-toast')).toHaveCount(0);

  const secondRunRequest = page.waitForRequest(
    (request) =>
      request.url().includes(`/api/projects/${pid}/actions/v1/run`) &&
      request.method() === 'POST',
  );
  await page.getByTestId('overwrite-collision-confirm').click();
  const posted = (await secondRunRequest).postDataJSON() as {
    action_id: string;
    replace_existing?: boolean;
  };
  expect(posted.action_id).toBe('map.extract');
  expect(posted.replace_existing).toBe(true);

  // The resubmit actually launches (the server honors overwrite:true for the
  // prior AI-generated column) — no error toast, and the modal is gone.
  await expect(page.getByTestId('overwrite-collision-modal')).toHaveCount(0);
  await expect(page.getByTestId('error-toast')).toHaveCount(0);
});

test('the translate drawer refreshes the column list on open, so a column created before the drawer opens is already known client-side', async ({
  page,
}) => {
  // Translate's Result defaults to "translation". When the form already knows
  // a generated column of that name, it saves to a fresh name instead and says
  // so, without a round trip to the server.
  const pid = await createProject(page.request, uniqueName('e2e-overwrite-drawer-refresh'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'meetings.csv',
    ['title,notes', '"Budget hearing","Officials discussed vendor bids and transit funds."'].join(
      '\n',
    ),
  );

  await page.goto(`/p/${pid}/s/${sheetId}`);
  // A column materializes AFTER the initial page load but BEFORE the drawer
  // opens — the drawer's own refresh-on-open (not the initial page load)
  // is what must pick this up.
  await runAndWait(page.request, pid, {
    action_id: 'map.regex_extract',
    scope: { kind: 'sheet_rows', sheet_id: sheetId },
    params: {
      input_columns: ['title'],
      pattern: '(.+)',
      group: 0,
    },
    output_names: { extracted: 'translation' },
    idempotency_key: 'e2e-behind-the-back@sha256:overwrite-drawer-refresh',
  });

  // The drawer's refresh re-reads the sheet list, which carries each sheet's
  // columns.
  const columnsRequest = page.waitForResponse((response) =>
    new URL(response.url()).pathname === `/api/projects/${pid}/sheets`
      && response.request().method() === 'GET',
  );
  await openAction(page, 'map.translate');
  await columnsRequest;
  await selectActionSourceColumns(page, 'title');

  // Server truth is visible client-side without the 409 path: the default
  // name steps around the existing column.
  await expect(page.getByRole('combobox', { name: 'Result' })).toHaveValue('translation_2');
  await expect(page.locator('[data-testid^="default-output-name-collision-"]')).toContainText(
    'A "translation" column already exists',
  );
});
