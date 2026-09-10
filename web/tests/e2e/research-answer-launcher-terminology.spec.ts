import { expect, test, type Page } from '@playwright/test';
import {
  clickRunButton,
  createProject,
  importCsv,
  openAction,
  revealRibbonAction,
  uniqueName,
} from './helpers';

const routeRe = (path: string) =>
  new RegExp(`${path.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}(?:\\?|$)`);

async function stubResearchAnswerRun(
  page: Page,
  pid: string,
): Promise<{
  legacyPosts: Array<Record<string, unknown>>;
  v1Posts: Array<Record<string, unknown>>;
}> {
  const legacyPosts: Array<Record<string, unknown>> = [];
  const v1Posts: Array<Record<string, unknown>> = [];
  await page.route(`**/api/projects/${pid}/run`, async (route) => {
    legacyPosts.push(route.request().postDataJSON() as Record<string, unknown>);
    await route.fulfill({
      status: 500,
      contentType: 'application/json',
      body: JSON.stringify({ error: 'research.answer must use v1 action run' }),
    });
  });
  await page.route(`**/api/projects/${pid}/actions/v1/run`, async (route) => {
    const body = route.request().postDataJSON() as Record<string, unknown>;
    v1Posts.push(body);
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        schema_version: 'frisket.action_result.v1',
        action: { kind: body.action_id, action_id: 'act-research-answer' },
        status: 'completed',
        project_id: pid,
        run_id: 9961,
        receipt_id: 'receipt-research-answer',
        errors: [],
      }),
    });
  });
  await page.route(`**/api/projects/${pid}/actions/runs/9961/status`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        run_id: 9961,
        status: 'completed',
        total: 2,
        completed: 2,
        failed: 0,
        cost: 0.02,
        live: false,
      }),
    });
  });
  return { legacyPosts, v1Posts };
}

test('research answer uses its ribbon label, catalog form title and canonical run route', async ({
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
  const posts = await stubResearchAnswerRun(page, pid);

  await page.goto(`/p/${pid}/s/${sheetId}`);
  // The ribbon keeps its concise display label; the form uses the catalog title.
  // Both surfaces share the canonical action identity.
  const launcherTile = await revealRibbonAction(page, 'research.answer');
  await expect(launcherTile).toContainText('Web research');
  const cardText = await launcherTile.innerText();
  expect(cardText).not.toMatch(/\b(recipe|legacy|internal agent)\b/i);
  await openAction(page, 'research.answer');

  await expect(page.getByTestId('generated-action-form')).toBeVisible();
  await expect(page.getByTestId('action-form-title')).toContainText(
    'Research each row',
  );
  const formText = await page.getByTestId('generated-action-form').innerText();
  expect(formText).not.toMatch(/\b(recipe|legacy|internal agent)\b/i);

  await page.goto(`/p/${pid}/s/${sheetId}/action/research.answer`);
  await expect(page.getByTestId('generated-action-form')).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId('action-form-title')).toContainText(
    'Research each row',
  );
  await expect(page).toHaveURL(routeRe(`/p/${pid}/s/${sheetId}/action/research.answer`));
  expect(page.url()).not.toContain('/recipe/');

  await page
    .getByTestId('field-question')
    .fill('Answer using cited public sources for this row.');
  await page.getByTestId('field-output-answer').fill('answer');
  await clickRunButton(page);

  await expect.poll(() => posts.v1Posts.length, { timeout: 5000 }).toBe(1);
  expect(posts.legacyPosts).toHaveLength(0);
  const posted = posts.v1Posts[0];
  expect(posted.action_id).toBe('research.answer');
  expect(posted.scope).toEqual({ kind: 'sheet_rows', sheet_id: sheetId });
  expect(posted.params).toMatchObject({
    question: { text: 'Answer using cited public sources for this row.' },
  });
  expect(posted.output_names).toMatchObject({ answer: 'answer' });
  expect(posted.idempotency_key).toEqual(expect.any(String));
  expect(posted).not.toHaveProperty('kind');
  expect(posted).not.toHaveProperty('capabilities');
});
