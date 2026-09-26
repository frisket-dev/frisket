// "Configure AI providers" is a settings page for the whole profile/workspace,
// not a pane buried inside a model selector. The workspace-level keys
// (<ws>/.frisket/provider_keys.json) and the Ollama URL had no settings home
// in the local tier — project.ai-providers manages per-PROJECT overrides and
// organization.ai-providers is hosted-only/disabled locally.
//
// DONE means:
// - /settings/personal/ai-providers renders the workspace provider panel:
//   per-provider key rows (add/test/save) AND the Ollama URL editor with the
//   reachability badge — the exact surface the picker's inline pane held.
// - Ask's model selector is an authoritative project-scoped dialog, not an
//   inline provider configuration view.
// - The remediation copy's "Settings → AI Providers" is thereby literal.

import { expect, test } from '@playwright/test';
import type {
  HttpSelectorChoicesQuery,
  HttpSelectorChoicesResponse,
  SelectorSubject,
} from '../../src/api/selectorChoices';
import { createProject, importCsv, uniqueName } from './helpers';

const CSV = 'story\n"City council approved a paving contract."\n';

function askSelectorResponse(
  pid: string,
  subject: Extract<SelectorSubject, { kind: 'project_ask' }>,
): HttpSelectorChoicesResponse {
  const current = subject.model === 'ollama/qwen' ? 'ollama-qwen' : 'local-semantic';
  return {
    schema_version: 'frisket.selector_choices.v1',
    project_id: pid,
    subject: { kind: 'project_ask' },
    depends_on: [],
    current_choice_id: current,
    default_choice_id: 'local-semantic',
    groups: [{
      group_id: 'local', kind: 'local', label: 'Local', status: 'ready', choices: [{
        choice_id: 'local-semantic', label: 'Local semantic', summary: 'On this computer', description: '',
        model_card_url: null, authored_selection: { kind: 'model', model: 'local/semantic' },
        resolved_target: null, processing_destination: { kind: 'local', label: 'On this computer' },
        facts: [], status: 'ready', can_author: true, can_run: true, blocker: null, setup: null,
        active_operation: null, is_default: true, is_current: current === 'local-semantic',
      }],
    }, {
      group_id: 'ollama', kind: 'local', label: 'Ollama', status: 'ready', choices: [{
        choice_id: 'ollama-qwen', label: 'Qwen', summary: 'On this computer', description: '',
        model_card_url: null, authored_selection: { kind: 'model', model: 'ollama/qwen' },
        resolved_target: null, processing_destination: { kind: 'local', label: 'On this computer' },
        facts: [], status: 'ready', can_author: true, can_run: true, blocker: null, setup: null,
        active_operation: null, is_default: false, is_current: current === 'ollama-qwen',
      }],
    }],
    orphaned_current: null,
  };
}

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

test('ask model selector opens an authoritative dialog without an inline configuration pane', async ({
  page,
}) => {
  const pid = await createProject(page.request, uniqueName('e2e-provider-nav'));
  await importCsv(page.request, pid, 'stories.csv', CSV);
  await page.route(`**/api/projects/${pid}/selector-choices`, async (route) => {
    const body = route.request().postDataJSON() as HttpSelectorChoicesQuery;
    if (body.subject.kind !== 'ask') {
      await route.fallback();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(askSelectorResponse(pid, body.subject)),
    });
  });
  await page.goto(`/p/${pid}`);

  await page.getByTestId('chrome-ask-toggle').click();
  const panel = page.getByTestId('ask-panel');
  await expect(panel).toBeVisible();
  const trigger = panel.locator('.engine-selector__trigger');
  await trigger.click();
  const dialog = page.getByTestId('engine-selector-dialog');
  await expect(dialog.getByRole('searchbox', { name: 'Search Model' })).toBeFocused();
  await dialog.getByRole('searchbox', { name: 'Search Model' }).fill('Qwen');
  await dialog.locator('[data-engine-selector-choice="ollama-qwen"]').click();
  await expect(trigger).toContainText('Qwen');
  await expect(panel).toBeVisible();
  // Provider setup does not leak into the chat popover.
  await expect(page.getByTestId('provider-config')).toHaveCount(0);
});
