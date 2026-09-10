import { expect, test } from '@playwright/test';
import { createProject, listSheets, openProject, openToolbarOverflow, uniqueName } from './helpers';

function importRowsAction() {
  return {
    schema_version: 'frisket.action.v2',
    kind: 'import.rows',
    capabilities: ['project:write'],
    params: {
      sheet_name: 'Receipt UI Rows',
      mode: 'create_sheet',
      columns: [
        { name: 'headline', type: 'text' },
        { name: 'source_url', type: 'url' },
      ],
      rows: [
        {
          headline: 'City hall awarded a no-bid contract.',
          source_url: 'https://example.com/story/1',
        },
        {
          headline: 'Routine road work finished early.',
          source_url: 'https://example.com/story/2',
        },
      ],
      source: {
        kind: 'inline',
        label: 'receipt inspector fixture',
        fingerprint: 'sha256:receipt-inspector',
      },
    },
    idempotency_key: 'v1-receipt-inspector@sha256:stable',
  };
}

test('provenance drawer exposes and inspects v1 executor receipts', async ({
  page,
  request,
}) => {
  const pid = await createProject(request, uniqueName('e2e-v1-receipt'));
  const runResponse = await request.post(
    `/api/projects/${pid}/actions/v1/run`,
    { data: importRowsAction() },
  );
  expect(runResponse.ok()).toBeTruthy();
  const result = await runResponse.json();
  const receiptId = result.receipt_id as string;
  expect(receiptId).toMatch(/^receipt_/);

  const provenanceResponse = await request.get(`/api/projects/${pid}/provenance`);
  expect(provenanceResponse.ok()).toBeTruthy();
  const provenance = await provenanceResponse.json();
  expect(provenance.receipts).toContainEqual(
    expect.objectContaining({
      receipt_id: receiptId,
      action_kind: 'import.rows',
      status: 'completed',
    }),
  );

  const sheet = (await listSheets(request, pid)).find((item) => item.name === 'Receipt UI Rows');
  if (!sheet) throw new Error('v1 import.rows did not create the expected sheet');
  await openProject(page, pid, sheet.id);
  await openToolbarOverflow(page);
  await page.getByTestId('open-provenance-manifest').click();

  const drawer = page.getByTestId('provenance-manifest');
  await expect(drawer).toBeVisible();
  const receiptRow = drawer.getByTestId('provenance-receipt-row').filter({
    hasText: receiptId,
  });
  await expect(receiptRow).toContainText('import.rows');
  await receiptRow.getByTestId('receipt-inspect-button').click();

  const inspector = drawer.getByTestId('receipt-inspector');
  await expect(inspector).toContainText('frisket.receipt.v1');
  await expect(inspector).toContainText(receiptId);
  await expect(inspector).toContainText('import.rows');
  await expect(inspector).toContainText('completed');
  await expect(inspector.getByTestId('receipt-outputs')).toContainText('source_rows');
  await expect(inspector.getByTestId('receipt-evidence')).toContainText('source_cell');
});
