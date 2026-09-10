// RED-FIRST (authored 2026-06-13): Copilot proposals should be inspectable in
// the normal action form before they are run.

import { expect, test } from '@playwright/test';
import { createProject, importCsv, uniqueName } from './helpers';

test('each copilot proposal inspection opens a fresh controller draft in the normal action form', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-copilot-inspect'));
  const sheetId = await importCsv(
    page.request,
    pid,
    'stories.csv',
    'story\n"The council approved a budget."\n',
  );
  await page.route(`**/api/projects/${pid}/copilot`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        reply: 'I can classify each story by news beat.',
        needs_import: false,
        proposals: [
          {
            kind: 'map',
            title: 'Classify by News Beat',
            spec: {
              action_id: 'map.classify',
              scope: { kind: 'sheet_rows', sheet_id: sheetId },
              output_names: { news_beat: 'news_beat', news_beat_confidence: 'news_beat_confidence', news_beat_justification: 'news_beat_justification' },
              params: {
                engine: 'llm',
                source: ['story'],
                model: 'anthropic/claude-haiku-4-5',
                context: 'Classify each row by local news beat.',
                fields: [
                  {
                    name: 'news_beat',
                    type: 'category',
                    labels: ['government', 'business', 'other'],
                  },
                ],
                include_confidence: true,
                include_justification: true,
              },
            },
          },
        ],
      }),
    });
  });

  await page.goto(`/p/${pid}`);

  // Copilot is a Focus popover: its contribution
  // frame renders once the ✧ chrome toggle summons the popover, not on load.
  await page.getByTestId('chrome-copilot-toggle').click();
  const copilotContribution = page.getByTestId('workbench-contribution-frisket-core-panel-copilot');
  await expect(copilotContribution).toBeVisible();
  await expect(copilotContribution).toHaveAttribute('data-schema-version', 'frisket.workbench.panel.v1');
  await expect(copilotContribution).toHaveAttribute('data-contribution-id', 'frisket.core.panel.copilot');
  await expect(copilotContribution).toHaveAttribute('data-host', 'leftSidebar');
  await expect(copilotContribution).toHaveAttribute('data-mode', 'panel');
  await expect(copilotContribution).toHaveAttribute('data-runtime-component-key', 'core.panels.CopilotPanel');
  await expect(copilotContribution).toHaveAttribute('data-required-capabilities', /copilot\.chat/);
  await expect(copilotContribution).toHaveAttribute(
    'data-required-capabilities',
    /action\.proposal\.inspect/,
  );
  await expect(copilotContribution).toHaveAttribute(
    'data-required-capabilities',
    /action\.proposal\.run/,
  );

  await page.getByTestId('copilot-input').fill('Classify these stories');
  await page.getByTestId('copilot-send').click();

  const proposal = page.getByTestId('copilot-proposal').first();
  await expect(proposal).toContainText('Classify by News Beat');
  await proposal.getByTestId('copilot-inspect').click();

  await expect(page.getByTestId('action-panel')).toBeVisible();
  await expect(page.getByTestId('action-form')).toBeVisible();
  await expect(page.getByTestId('action-form-title')).toContainText('Classify by News Beat');
  await expect(page.getByTestId('action-prompt')).toHaveValue(
    'Classify each row by local news beat.',
  );
  await expect(page.getByTestId('new-column-name')).toHaveValue('news_beat');

  // Dirty several independent draft choices without unmounting the drawer.
  await page.getByTestId('action-prompt').fill('dirty prompt');
  await page.getByTestId('new-column-name').fill('dirty_output');

  // Inspecting the same visible proposal again is still an explicit user
  // launch. Its new proposal seq/launchId must reset all proposal-provided
  // initial choices; an equal kind/title/spec is not a no-op rerender.
  await page.getByTestId('copilot-collapse').click();
  await page.getByTestId('copilot-input').fill('Classify these stories');
  await page.getByTestId('copilot-send').click();
  const repeatedProposal = page.getByTestId('copilot-proposal').first();
  await expect(repeatedProposal).toContainText('Classify by News Beat');
  await repeatedProposal.getByTestId('copilot-inspect').click();

  await expect(page.getByTestId('action-form-title')).toContainText('Classify by News Beat');
  await expect(page.getByTestId('action-prompt')).toHaveValue(
    'Classify each row by local news beat.',
  );
  await expect(page.getByTestId('new-column-name')).toHaveValue('news_beat');
  await expect(page.getByTestId('debug-trace-toggle')).toHaveCount(0);
});
