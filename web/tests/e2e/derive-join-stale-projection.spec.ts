// Live acceptance for derive.join projection changes: a renamed column keeps
// its stable ID, while a deleted projection fails closed until replacement.

import { execFileSync } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import { clickRunButton, createProject, openAction, openProject, sheetColumns, uniqueName } from './helpers';

const REPO_ROOT = path.basename(process.cwd()) === 'web' ? path.resolve(process.cwd(), '..') : process.cwd();

async function importNamedCsv(
  request: APIRequestContext,
  pid: string,
  sheetName: string,
  csv: string,
): Promise<number> {
  const response = await request.post(`/api/projects/${pid}/import/csv?sheet_name=${sheetName}`, {
    multipart: {
      file: { name: `${sheetName}.csv`, mimeType: 'text/csv', buffer: Buffer.from(csv) },
    },
  });
  expect(response.ok(), await response.text()).toBeTruthy();
  return (await response.json()).sheet_id;
}

function mutateColumn(
  pid: string,
  sheetId: number,
  columnId: string,
  mutation: 'rename' | 'delete',
  renamedTo = '',
): void {
  const workspace = process.env.FRISKET_E2E_WS ?? path.join(os.homedir(), '.frisket/e2e-ws');
  const script = String.raw`
import sys
from pathlib import Path

from frisket.engine.store import Project

workspace, pid, sheet_id, column_id, mutation, renamed_to = sys.argv[1:]
project = Project(Path(workspace) / f"{pid}.frisket")
try:
    if mutation == "rename":
        project.db.execute(
            "UPDATE columns SET name=? WHERE sheet_id=? AND id=?",
            (renamed_to, int(sheet_id), int(column_id)),
        )
    else:
        project.db.execute(
            "DELETE FROM columns WHERE sheet_id=? AND id=?",
            (int(sheet_id), int(column_id)),
        )
    project.db.commit()
finally:
    project.close()
`;
  execFileSync(
    'uv',
    ['run', 'python', '-c', script, workspace, pid, String(sheetId), columnId, mutation, renamedTo],
    { cwd: REPO_ROOT, encoding: 'utf8', timeout: 120_000 },
  );
}

async function seedAndOpen(page: Page, request: APIRequestContext, name: string) {
  const pid = await createProject(request, uniqueName(name));
  const leftId = await importNamedCsv(
    request,
    pid,
    'Cities',
    'code,city,state\nNY,Albany,New York\nCA,Sacramento,California\n',
  );
  await importNamedCsv(request, pid, 'Zones', 'code,zone\nNY,East\nCA,West\n');
  await openProject(page, pid, leftId);
  await openAction(page, 'derive.join');
  await expect(page.getByTestId('tabular-join-form')).toBeVisible();
  await page.getByTestId('field-join_right_sheet').selectOption({ label: 'Zones' });
  await expect(page.getByTestId('field-join_key_left-0')).toHaveValue('code');
  await expect(page.getByTestId('field-join_key_right-0')).toHaveValue('code');
  await page.getByTestId('field-join_left_columns').click();
  await page
    .getByTestId('field-join_left_columns-menu')
    .getByRole('option')
    .filter({ hasText: /^city/ })
    .click();
  await page.getByTestId('field-join_how').click();
  const city = (await sheetColumns(request, pid, leftId)).find((column) => column.name === 'city');
  expect(city).toBeTruthy();
  return { pid, leftId, cityId: String(city!.id) };
}

test('refresh after a projection rename retains its stable id and posts the current wire name', async ({
  page,
  request,
}) => {
  const { pid, leftId, cityId } = await seedAndOpen(page, request, 'derive-join-projection-rename');
  let posted: Record<string, unknown> | null = null;
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    posted = route.request().postDataJSON() as Record<string, unknown>;
    await route.continue();
  });

  mutateColumn(pid, leftId, cityId, 'rename', 'municipality');
  await page.getByTestId('tabular-join-refresh-sheets').click();
  await expect(page.getByTestId('field-join_left_columns')).toContainText('municipality');
  await expect(page.getByTestId('run-button')).toBeEnabled();

  const response = page.waitForResponse((candidate) =>
    candidate.url().includes(`/api/projects/${pid}/actions/v1/run`)
      && candidate.request().method() === 'POST',
  );
  await clickRunButton(page);
  expect((await response).ok()).toBeTruthy();
  const params = posted?.params as Record<string, unknown>;
  expect(params.columns).toEqual([{ side: 'left', column: 'municipality' }]);
  expect(JSON.stringify(params.columns)).not.toContain('city');
});

test('refresh after projection deletion fails closed until explicit replacement and sends no stale POST', async ({
  page,
  request,
}) => {
  const { pid, leftId, cityId } = await seedAndOpen(page, request, 'derive-join-projection-delete');
  const posted: Array<Record<string, unknown>> = [];
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    posted.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.continue();
  });

  mutateColumn(pid, leftId, cityId, 'delete');
  await page.getByTestId('tabular-join-refresh-sheets').click();
  await expect(page.getByTestId('run-button')).toBeDisabled();
  await expect(page.getByTestId('run-disabled-reason')).toContainText(
    /(?:city|projection).*(?:missing|deleted|repair)/i,
  );
  await page.getByTestId('run-button').click({ force: true });
  await expect.poll(() => posted.length).toBe(0);

  await page.getByTestId('field-join_left_columns').click();
  await page
    .getByTestId('field-join_left_columns-menu')
    .getByRole('option')
    .filter({ hasText: /^state/ })
    .click();
  await page.getByTestId('field-join_how').click();
  await expect(page.getByTestId('run-button')).toBeEnabled();

  const response = page.waitForResponse((candidate) =>
    candidate.url().includes(`/api/projects/${pid}/actions/v1/run`)
      && candidate.request().method() === 'POST',
  );
  await clickRunButton(page);
  expect((await response).ok()).toBeTruthy();
  expect(posted).toHaveLength(1);
  const params = posted[0].params as Record<string, unknown>;
  expect(params.columns).toEqual([{ side: 'left', column: 'state' }]);
  expect(JSON.stringify(params)).not.toContain('city');
});
