import { expect, test } from '@playwright/test';
import { createProject, importCsv, openProject, openToolbarOverflow, runAndWait, uniqueName } from './helpers';

const CSV = `story
"Mayor met a lobbyist before the zoning vote."
`;

test('project provenance drawer renders real backend model and run data', async ({
  page,
  request,
}) => {
  const pid = await createProject(request, uniqueName('e2e-provenance'));
  const sheetId = await importCsv(request, pid, 'stories.csv', CSV);
  const runId = await runAndWait(request, pid, {
    recipe: 'classify',
    model: 'gemini/gemini-2.5-flash',
    sheet_id: sheetId,
    input_columns: ['story'],
    context: 'Classify this story. Use beat=civic.',
    fields: [{ name: 'beat', type: 'category', labels: ['civic', 'private'] }],
    include_confidence: false,
    include_justification: false,
  });

  const manifestResponse = await request.get(
    `/api/projects/${pid}/provenance?runs_offset=0&runs_limit=25&receipts_offset=0&receipts_limit=25`,
  );
  expect(manifestResponse.ok()).toBeTruthy();
  const manifest = await manifestResponse.json();
  expect(manifest.runs_page).toMatchObject({
    offset: 0,
    limit: 25,
    total: 1,
    has_more: false,
  });
  expect(manifest.receipts_page).toMatchObject({
    offset: 0,
    limit: 25,
    has_more: false,
  });
  expect(manifest.models).toContainEqual(
    expect.objectContaining({
      model: 'gemini/gemini-2.5-flash',
      provider: 'gemini',
      runs: 1,
      rows: 1,
    }),
  );
  expect(JSON.stringify(manifest)).not.toMatch(/recipe/i);
  expect(manifest).not.toHaveProperty('recipes');
  expect(manifest.action_kinds).toContainEqual(
    expect.objectContaining({
      action_kind: 'map.classify',
      action_name: 'Classify rows',
      runs: 1,
      rows: 1,
    }),
  );
  expect(manifest.runs).toContainEqual(
    expect.objectContaining({
      run_id: runId,
      action_kind: 'map.classify',
      action_name: 'Classify rows',
      model: 'gemini/gemini-2.5-flash',
    }),
  );

  const provenanceQueries: string[] = [];
  page.on('request', (request) => {
    const url = new URL(request.url());
    if (url.pathname === `/api/projects/${pid}/provenance`) {
      provenanceQueries.push(url.searchParams.toString());
    }
  });

  await openProject(page, pid, sheetId);
  await openToolbarOverflow(page);
  await page.getByTestId('open-provenance-manifest').click();

  const drawer = page.getByTestId('provenance-manifest');
  await expect(drawer).toBeVisible();
  await expect.poll(() => provenanceQueries.some((query) => (
    query.includes('runs_offset=0')
      && query.includes('runs_limit=25')
      && query.includes('receipts_offset=0')
      && query.includes('receipts_limit=25')
  ))).toBeTruthy();
  await expect(drawer.getByTestId('provenance-provider-list')).toContainText('gemini');
  await expect(drawer.getByTestId('provenance-model-table')).toContainText(
    'gemini/gemini-2.5-flash',
  );
  await expect(drawer.getByTestId('provenance-model-table')).toContainText('1');
  await expect(drawer.getByTestId('provenance-action-kind-table')).toContainText(
    'Classify rows',
  );
  await expect(drawer.getByTestId('provenance-action-kind-table')).toContainText(
    'map.classify',
  );
  await expect(drawer.getByTestId('provenance-run-list')).toContainText(`run ${runId}`);
  await expect(drawer.getByTestId('provenance-json-preview')).toContainText(
    'gemini/gemini-2.5-flash',
  );

  const href = await drawer.getByTestId('provenance-download-json').getAttribute('href');
  expect(href).toMatch(/^data:application\/json/);
  const encoded = href!.slice(href!.indexOf(',') + 1);
  const exported = JSON.parse(decodeURIComponent(encoded));
  expect(exported.manifest.touched).toContain('gemini/gemini-2.5-flash');
  expect(exported.manifest.runs_page).toMatchObject({
    offset: 0,
    limit: 25,
    total: 1,
  });
  expect(exported.manifest.receipts_page).toMatchObject({
    offset: 0,
    limit: 25,
  });
  expect(exported.manifest.runs).toContainEqual(
    expect.objectContaining({
      run_id: String(runId),
      action_kind: 'map.classify',
      action_name: 'Classify rows',
    }),
  );
});
