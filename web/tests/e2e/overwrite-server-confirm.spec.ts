import { expect, test } from '@playwright/test';
import {
  clickRunButton,
  createProject,
  importCsv,
  openAction,
  runAndWait,
  uniqueName,
} from './helpers';

// A "would overwrite existing columns" hard error must offer a confirm prompt.
// Previously the
// confirm keys on the CLIENT column list (ActionPanel.tsx's
// existingColumnByName), which was stale after a column was created behind
// the form's back. Fix: (a) a server-side output_column_exists rejection
// (map_ai.py's precheck_fn, mapped to HTTP 409) converts into the SAME
// overwrite-confirm affordance the client-detected case already offers
// (OutputColumnCollisionModal, App.tsx's WorkspaceOverlayRegion) instead of
// a dead-end toast; (b) the drawer refreshes the sheet's column list when it
// opens, closing the most common staleness window.
//
// Spec: create a column SERVER-SIDE (a direct API call, bypassing this
// browser tab's own state entirely) while the extract form is already open
// with a field that collides with it -> launch -> server 409s ->
// confirm modal appears -> accept -> run launches with overwrite:true.

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

  await page.goto(`/p/${pid}/s/${sheetId}`);
  await openAction(page, 'map.extract');
  await page.getByRole('button', { name: 'Add column' }).click();
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
    kind: string;
    output_intent?: Array<{ kind: string }>;
  };
  expect(posted.kind).toBe('map.extract');
  expect(posted.output_intent).toContainEqual({ kind: 'overwrite_existing' });

  // The resubmit actually launches (the server honors overwrite:true for the
  // prior AI-generated column) — no error toast, and the modal is gone.
  await expect(page.getByTestId('overwrite-collision-modal')).toHaveCount(0);
  await expect(page.getByTestId('error-toast')).toHaveCount(0);
});

test('the translate drawer refreshes the column list on open, so a column created before the drawer opens is already known client-side', async ({
  page,
}) => {
  // translate (unlike extract, and unlike summarize which special-cases
  // itself out of the blocking check — ActionPanel.tsx's
  // blockingNewColumnError: `actionTemplate.kind === 'summarize' ||
  // overwriteActive ? null : newColumnError`) uses the Save-To combobox with
  // a NEW_COLUMN default (initialActionFormState: newColName defaults to
  // actionTemplate.defaultFields[0].name, "translation") AND actually BLOCKS
  // the Run button on a name collision, so it exercises the pre-existing
  // CLIENT-side collision banner (newColumnConflictName/run-overwrite-
  // existing) once sheet.columns is fresh — extract hides that combobox
  // entirely (hidesTargetColumn) and has no client-side collision UI at all,
  // which is why the first test above covers extract's server-only path
  // instead.
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

  // listSheets() (workspace/refreshSheets) fetches the bare sheet list, THEN
  // each sheet's columns via .../sheets/{id}/data?offset=0&limit=0 (api/
  // real.ts) — that second call is the one that actually carries the fresh
  // column list, so wait for IT specifically (a broad "/sheets" match would
  // resolve on the first, columns-less call too early).
  const columnsRequest = page.waitForResponse((response) =>
    response.url().includes(`/api/projects/${pid}/sheets/${sheetId}/data`) &&
    response.url().includes('limit=0'),
  );
  await openAction(page, 'map.translate');
  await columnsRequest;

  // Server truth is now visible client-side without ever hitting the 409
  // path: the run-disabled reason should name the real collision.
  await expect(page.getByTestId('run-disabled-reason')).toBeVisible();
  await expect(page.getByTestId('run-overwrite-existing')).toBeVisible();
});
