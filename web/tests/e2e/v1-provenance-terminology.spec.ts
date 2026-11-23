import { expect, test } from '@playwright/test';
import {
  createProject,
  importCsv,
  openProject,
  primeMapRowsReplies,
  runAndWait,
  typedActionRequest,
  uniqueName,
  openReceipts,
} from './helpers';
import { prepareOwnModelReplay, REPLAY_MODEL } from './replayModelScenario';

const CSV = `story
"Mayor met a lobbyist before the zoning vote."
`;

test('provenance drawer presents run summaries with action metadata', async ({
  page,
  request,
}) => {
  const pid = await createProject(request, uniqueName('e2e-v1-provenance-terms'));
  const sheetId = await importCsv(request, pid, 'stories.csv', CSV);
  await prepareOwnModelReplay(request, pid);
  const action = typedActionRequest('map.classify', {
    sheetId,
    params: {
      source: ['story'],
      engine: 'llm',
      model: REPLAY_MODEL,
      context: 'Classify this story. Use beat=civic.',
      fields: [{ name: 'beat', type: 'category', labels: ['civic', 'private'] }],
      include_confidence: false,
      include_justification: false,
    },
    outputNames: { beat: 'beat' },
  });
  primeMapRowsReplies(pid, action, [{ beat: 'civic' }]);
  await runAndWait(request, pid, action);

  const manifestResponse = await request.get(`/api/projects/${pid}/provenance`);
  expect(manifestResponse.ok()).toBeTruthy();
  const manifest = await manifestResponse.json();
  expect(JSON.stringify(manifest)).not.toMatch(/recipe/i);
  expect(manifest).not.toHaveProperty('recipes');
  expect(manifest).toHaveProperty('action_kinds');
  expect(manifest.action_kinds).toContainEqual(
    expect.objectContaining({
      action_kind: 'map.classify',
      action_name: 'Classify rows',
      runs: 1,
      rows: 1,
    }),
  );

  await openProject(page, pid, sheetId);
  await openReceipts(page);

  const drawer = page.getByTestId('provenance-manifest');
  await expect(drawer).toBeVisible();
  await expect(drawer.getByRole('heading', { name: 'Actions' })).toBeVisible();
  await expect(drawer.getByTestId('provenance-action-kind-table')).toContainText(
    'Classify rows',
  );
  await expect(drawer.getByTestId('provenance-action-kind-table')).toContainText(
    'map.classify',
  );
  await expect(drawer.getByRole('heading', { name: 'Recipes' })).toHaveCount(0);
  await expect(drawer.getByTestId('provenance-json-preview')).not.toContainText(/recipe/i);

  const href = await drawer.getByTestId('provenance-download-json').getAttribute('href');
  expect(href).toMatch(/^data:application\/json/);
  const encoded = href!.slice(href!.indexOf(',') + 1);
  const exportedText = decodeURIComponent(encoded);
  expect(exportedText).not.toMatch(/recipe/i);
  const exported = JSON.parse(exportedText);
  expect(JSON.stringify(exported.manifest)).not.toMatch(/recipe/i);
  expect(exported.manifest).toHaveProperty('action_kinds');
  expect(exported.manifest).not.toHaveProperty('recipes');
  expect(exported.manifest.action_kinds).toContainEqual(
    expect.objectContaining({
      action_kind: 'map.classify',
      action_name: 'Classify rows',
      runs: 1,
      rows: 1,
    }),
  );
  expect(exported.manifest.runs).toContainEqual(
    expect.objectContaining({
      action_kind: 'map.classify',
      action_name: 'Classify rows',
    }),
  );
});
