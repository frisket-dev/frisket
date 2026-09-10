import { expect, test } from '@playwright/test';
import {
  clickRunButton,
  createProject,
  dblclickCell,
  importCsv,
  openAction,
  openProject,
  sheetColumns,
  uniqueName,
} from './helpers';
import {
  closeActionFormIfOpen,
  hideActionsPanelIfOpen,
  seedGeneratedCellEvidence,
  stubV1ActionRun,
} from './investigativeActionFixtures';

test('grounded extract posts citation policy on map.extract and opens evidence links', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-grounded-extract'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'source-docs.csv',
    [
      'title,pdf,notes',
      '"Procurement memo","contract.pdf","Award value is listed in the attached contract."',
      '"Hearing minutes","minutes.pdf","Board members discussed the same award."',
    ].join('\n'),
  );
  const seeded = seedGeneratedCellEvidence({
    pid,
    sheetId,
    outputColumnName: 'award_value',
    outputValue: '$1,250,000',
    producerKind: 'map.extract',
  });
  const columns = await sheetColumns(page.request, pid, sheetId);
  const posts = await stubV1ActionRun({ page, pid, runId: 9811 });

  await openProject(page, pid, sheetId);
  await openAction(page, 'map.extract');
  await page.getByTestId('text-source-column-select').selectOption({ label: 'Template' });
  await page
    .getByTestId('text-source-template-input')
    .fill('{{title}} {{pdf}} {{notes}}');
  await page
    .getByTestId('action-prompt')
    .fill('Extract only values supported by the cited source document.');
  await page.getByLabel('Field 1 name').fill('award_value');
  await page.getByLabel('Field 1 type').selectOption('text');
  await page.getByLabel('Field 1 description').fill('Contract award amount');
  // extract-form-polish-v1: the Ground-evidence + Require-citations checkbox
  // pair became one citation_mode select, in the main form body (not behind
  // Advanced options). 'Ground citations against' (renamed
  // source_document_columns) stays advanced.
  await page.getByTestId('field-citation_mode').selectOption('require');
  await page.getByTestId('advanced-params-extract').locator('summary').click();
  await page.getByTestId('field-source_document_columns').fill('pdf');
  await clickRunButton(page);

  await expect.poll(() => posts.v1Posts.length, { timeout: 5000 }).toBe(1);
  expect(posts.legacyPosts).toHaveLength(0);
  const posted = posts.v1Posts[0];
  expect(posted.kind).toBe('map.extract');
  expect(posted.kind).not.toBe('document.extract');
  expect(posted.capabilities).toEqual(['project:write', 'model:complete']);
  const params = posted.params as Record<string, unknown>;
  expect(params.sheet_id).toBe(sheetId);
  expect(params.input_columns).toEqual(['title', 'pdf', 'notes']);
  expect(params.input_template).toBe('{{title}} {{pdf}} {{notes}}');
  expect(params.grounding).toMatchObject({
    enabled: true,
    citation_required: true,
    include_stale_inputs: false,
    source_document_columns: ['pdf'],
  });
  // MapExtractEvidencePolicy (frisket/contracts/actions/schemas/maps.py) is
  // extra="forbid" and declares ONLY citation_required — an unsupported_fields
  // key here 400s the launch (the live grounding-on bug fixed by item 0).
  expect(params.evidence_policy).toMatchObject({
    citation_required: true,
  });
  expect(params.evidence_policy).not.toHaveProperty('unsupported_fields');
  expect(params.fields).toContainEqual(expect.objectContaining({ name: 'award_value' }));

  await closeActionFormIfOpen(page);
  await hideActionsPanelIfOpen(page);
  await dblclickCell(page, columns, 'award_value', 0);
  const rowDrawer = page.getByTestId('row-drawer');
  await expect(rowDrawer).toBeVisible();
  await expect(rowDrawer.getByTestId('cell-evidence-active-award_value')).toBeVisible();
  await rowDrawer.getByTestId('cell-evidence-open-award_value').click();
  const viewer = page.getByTestId('evidence-viewer');
  await expect(viewer).toBeVisible();
  await expect(viewer.getByTestId('evidence-export-ref')).toContainText(seeded.stableId);
});
