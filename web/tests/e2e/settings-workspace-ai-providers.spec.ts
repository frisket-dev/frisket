// "Configure AI providers" is a settings page for the whole profile/workspace,
// not a pane buried inside the ModelPicker popover. The workspace-level keys
// (<ws>/.frisket/provider_keys.json) and the Ollama URL had no settings home
// in the local tier — project.ai-providers manages per-PROJECT overrides and
// organization.ai-providers is hosted-only/disabled locally.
//
// DONE means:
// - /settings/personal/ai-providers renders the workspace provider panel:
//   per-provider key rows (add/test/save) AND the Ollama URL editor with the
//   reachability badge — the exact surface the picker's inline pane held.
// - The ModelPicker's "Configure AI providers…" entry NAVIGATES there (no
//   more inline config view), from the copilot popover included.
// - The remediation copy's "Settings → AI Providers" is thereby literal.

import { expect, test } from '@playwright/test';
import { createProject, importCsv, uniqueName } from './helpers';

const CSV = 'story\n"City council approved a paving contract."\n';

test('workspace AI-providers settings page renders keys + ollama URL editor', async ({
  page,
}) => {
  await page.goto('/settings/personal/ai-providers');

  const section = page.getByTestId('settings-section-personal-ai-providers');
  await expect(section).toBeVisible();
  // key management rows for the platform providers
  await expect(section.getByTestId('provider-status-anthropic')).toBeVisible();
  await expect(section.getByTestId('provider-status-openai')).toBeVisible();
  // the ollama URL editor with its reachability badge
  await expect(section.getByTestId('ollama-url-input')).toBeVisible();
  await expect(section.getByTestId('ollama-reachability-badge')).toBeVisible();
});

test('model picker Configure AI providers navigates to the settings page', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-provider-nav'));
  await importCsv(page.request, pid, 'stories.csv', CSV);
  await page.goto(`/p/${pid}`);

  await page.getByTestId('chrome-copilot-toggle').click();
  await expect(page.getByTestId('copilot-panel')).toBeVisible();
  await page.getByTestId('model-picker-button').click();
  await expect(page.getByTestId('model-picker-menu')).toBeVisible();

  await page.getByTestId('configure-providers').click();

  await expect(page).toHaveURL(/\/settings\/personal\/ai-providers$/);
  await expect(
    page.getByTestId('settings-section-personal-ai-providers'),
  ).toBeVisible();
  // the picker's inline config pane is gone — configuration lives here now
  await expect(page.getByTestId('provider-config')).toHaveCount(0);
});
