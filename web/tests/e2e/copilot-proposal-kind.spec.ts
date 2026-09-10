// Copilot proposal cards show the action KIND as a chip. Each card renders a
// small badge before the title derived from
// the proposal's action identity, humanized: strip the family prefix, replace underscores,
// sentence-case ('map.regex_extract' → 'Regex extract'). Lives in its own
// spec (not copilot.spec.ts) to keep this contract isolated.
//
// Contract (testids):
//   copilot-proposal-kind — the kind chip inside a copilot-proposal card

import { expect, test } from '@playwright/test';
import { createProject, importCsv, uniqueName } from './helpers';

const CSV =
  'location\n' +
  '"Waffle House-Duluth,GA"\n' +
  '"Waffle House-Marietta,GA"\n';

test('copilot proposal cards show a humanized action-kind chip', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-copilot-kind'));
  const sheetId = await importCsv(page.request, pid, 'wafflehouses.csv', CSV);
  await page.route(`**/api/projects/${pid}/copilot`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        reply: 'A regex handles this — no LLM needed.',
        needs_import: false,
        proposals: [
          {
            kind: 'map',
            title: 'Extract state from location',
            spec: {
              action_id: 'map.regex_extract',
              scope: { kind: 'sheet_rows', sheet_id: sheetId },
              params: {
                input_columns: ['location'],
                pattern: ',\\s*([A-Z]{2})$',
              },
              output_names: { extracted: 'state' },
            },
          },
          {
            kind: 'map',
            title: 'Classify each location',
            spec: {
              action_id: 'map.classify',
              scope: { kind: 'sheet_rows', sheet_id: sheetId },
              output_names: { region: 'region' },
              params: {
                engine: 'llm',
                source: ['location'],
                model: 'anthropic/claude-haiku-4-5',
                context: 'Classify each location.',
                fields: [
                  { name: 'region', type: 'category', labels: ['south', 'other'] },
                ],
              },
            },
          },
          {
            kind: 'map',
            title: 'Format the location',
            spec: {
              action_id: 'map.template',
              scope: { kind: 'sheet_rows', sheet_id: sheetId },
              params: { template: { text: '{{location}}' } },
              output_names: { rendered: 'formatted location' },
            },
          },
        ],
      }),
    });
  });
  await page.goto(`/p/${pid}`);

  await page.getByTestId('chrome-copilot-toggle').click();
  await expect(page.getByTestId('copilot-panel')).toBeVisible();
  await page
    .getByTestId('copilot-input')
    .fill('Extract the state initialism from location.');
  await page.getByTestId('copilot-send').click();

  const proposals = page.getByTestId('copilot-proposal');
  await expect(proposals).toHaveCount(3, { timeout: 60_000 });

  // chip precedes the title and carries the humanized kind
  const first = proposals.nth(0);
  await expect(first.getByTestId('copilot-proposal-kind')).toHaveText(
    'Regex extract',
  );
  await expect(first).toContainText('Extract state from location');

  const second = proposals.nth(1);
  await expect(second.getByTestId('copilot-proposal-kind')).toHaveText(
    'Classify',
  );

  await expect(proposals.nth(2).getByTestId('copilot-proposal-kind')).toHaveText(
    'Template',
  );
});
