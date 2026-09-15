import { expect, test } from '@playwright/test';
import {
  createProject,
  importCsv,
  openAction,
  setColumnType,
  sheetColumns,
  uniqueName,
} from './helpers';

// This is deliberately a real-stack form proof. It never posts a run, so it
// does not invoke a model, provider, or recorded response; the live local
// backend supplies the catalog and the typed parameter-validation response.

test('Classify keeps a fresh invalid form quiet, then places and clears the label error', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-classify-validation-ux'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'stories.csv',
    'story\n"The council approved a transit budget."\n',
  );

  await page.goto(`/p/${pid}/s/${sheetId}`);
  await openAction(page, 'map.classify');

  const source = page.getByTestId('text-source-columns');
  await expect(source.getByRole('button', { name: 'Remove story' })).toBeVisible();

  const labels = page.getByTestId('classify-labels');
  const labelsError = page.getByTestId('classify-labels-error');
  await expect(labels).toHaveValue('');
  await expect(page.getByTestId('generated-action-run')).toBeDisabled();
  await expect(labelsError).toHaveCount(0);

  // A real nested Pydantic diagnostic arrives at fields -> [0, "labels"].
  // Whitespace makes the existing control touched without becoming a label.
  await labels.fill(' ');
  await expect(labelsError).toHaveText('Add at least one category label.');
  await expect(labelsError).toHaveCount(1);

  await labels.fill('civic, transit, other');
  await expect(labelsError).toHaveCount(0);
});

test('Classify gives a clear empty-source state without repeating the server refusal', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-classify-source-empty'));
  const sheetId = await importCsv(page.request, pid, 'places.csv', 'point\n\n');
  const columns = await sheetColumns(page.request, pid, sheetId);
  const point = columns.find((column) => column.name === 'point');
  expect(point).toBeTruthy();
  await setColumnType(page.request, pid, point!.id, 'geo_point');

  await page.goto(`/p/${pid}/s/${sheetId}`);
  await openAction(page, 'map.classify');

  await expect(page.getByTestId('text-source-empty')).toHaveText('No compatible source columns.');
  await expect(page.getByTestId('field-source-error')).toHaveCount(0);
  await expect(page.getByTestId('generated-action-run')).toBeDisabled();
});
