import { expect, test } from '@playwright/test';
import {
  createProject,
  importCsv,
  openAction,
  revealRibbonAction,
  uniqueName,
} from './helpers';

const routeRe = (path: string) =>
  new RegExp(`${path.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?:\\?|$)`);

test('research answer uses its ribbon label, catalog form title and canonical action route', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-research-answer'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'companies.csv',
    [
      'company,topic',
      '"Acme Corp","battery recall"',
      '"Globex","export controls"',
    ].join('\n'),
  );

  await page.goto(`/p/${pid}/s/${sheetId}`);
  // The ribbon keeps its concise display label; the form uses the catalog title.
  // Both surfaces share the canonical action identity.
  const launcherTile = await revealRibbonAction(page, 'research.answer');
  await expect(launcherTile).toContainText('Agent Research');
  const cardText = await launcherTile.innerText();
  expect(cardText).not.toMatch(/\b(recipe|legacy|internal agent)\b/i);
  await openAction(page, 'research.answer');

  await expect(page.getByTestId('generated-action-form')).toBeVisible();
  await expect(page.getByTestId('action-form-title')).toContainText(
    'Agent Research',
  );
  const formText = await page.getByTestId('generated-action-form').innerText();
  expect(formText).not.toMatch(/\b(recipe|legacy|internal agent)\b/i);

  await page.goto(`/p/${pid}/s/${sheetId}/action/research.answer`);
  await expect(page.getByTestId('generated-action-form')).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId('action-form-title')).toContainText(
    'Agent Research',
  );
  await expect(page).toHaveURL(routeRe(`/p/${pid}/s/${sheetId}/action/research.answer`));
  expect(page.url()).not.toContain('/recipe/');
});
