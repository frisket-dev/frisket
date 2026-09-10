// RED-FIRST (authored 2026-07-10) for copilot-import-needed-cta-v1: the live
// browser handoff from Copilot to the single global Import workspace.
//
// When the model decides the project needs data first it returns a strict
// `needs_import:true` reply with NO proposals (never an invented UI proposal or
// process state). The Copilot panel must surface an import call-to-action; the
// CTA runs one pure cross-store transition that closes the Copilot popover
// (without the focus-restoring dismiss path) and opens the SINGLE global
// ImportWorkspaceDialog. The dropzone no longer mounts a duplicate dialog, so
// exactly one import workspace is present after the handoff — proved from both a
// populated sheet and an empty-project surface.
//
// The Copilot reply is a deterministic wire mock, not keyword-faked model
// behavior; model reliability is covered by the separate cassette/live
// component tests/live/test_copilot_import_needed_semantics.py.

import { expect, test } from '@playwright/test';
import { createProject, importCsv, uniqueName } from './helpers';

const NEEDS_IMPORT_REPLY = {
  schema_version: 'frisket.copilot_reply.v1',
  reply: 'This project has no data yet — import a file before I can edit it.',
  needs_import: true,
  proposals: [],
  cost_usd: 0,
};

async function mockNeedsImport(page: import('@playwright/test').Page, pid: string) {
  await page.route(`**/api/projects/${pid}/copilot`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(NEEDS_IMPORT_REPLY),
    });
  });
}

async function askCopilot(page: import('@playwright/test').Page) {
  await page.getByTestId('chrome-copilot-toggle').click();
  await expect(page.getByTestId('copilot-panel')).toBeVisible();
  await page.getByTestId('copilot-input').fill('Add a summary column to every row');
  await page.getByTestId('copilot-send').click();
  await expect(page.getByText(/import a file/i)).toBeVisible();
}

test('populated surface: the needs-import CTA closes Copilot and opens the single import workspace', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-copilot-import'));
  await importCsv(page.request, pid, 'stories.csv', 'story\n"The council approved a budget."\n');
  await mockNeedsImport(page, pid);
  await page.goto(`/p/${pid}`);

  await askCopilot(page);

  // A needs_import reply must never render an invented action proposal.
  await expect(page.getByTestId('copilot-proposal')).toHaveCount(0);

  const cta = page.getByTestId('copilot-import-cta');
  await expect(cta).toBeVisible();
  await cta.click();

  // The handoff closes Copilot and opens exactly one global import workspace.
  await expect(page.getByTestId('copilot-panel')).toHaveCount(0);
  await expect(page.getByTestId('import-workspace-dialog')).toBeVisible();
  await expect(page.getByTestId('import-workspace-dialog')).toHaveCount(1);
});

test('empty-project surface: the CTA opens one import workspace, not a dropzone duplicate', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-copilot-import-empty'));
  await mockNeedsImport(page, pid);
  await page.goto(`/p/${pid}`);

  // The empty-project dropzone is present but must not own a second dialog.
  await expect(page.getByTestId('import-dropzone')).toBeVisible();

  await askCopilot(page);
  const cta = page.getByTestId('copilot-import-cta');
  await expect(cta).toBeVisible();
  await cta.click();

  await expect(page.getByTestId('copilot-panel')).toHaveCount(0);
  await expect(page.getByTestId('import-workspace-dialog')).toBeVisible();
  await expect(page.getByTestId('import-workspace-dialog')).toHaveCount(1);
});
