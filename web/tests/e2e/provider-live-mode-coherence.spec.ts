import { expect, test } from '@playwright/test';
import { mockHostedSettingsShell } from './settingsTestHelpers';

const providerCatalog = {
  schemaVersion: 'frisket.provider_catalog.v1',
  providers: [
    { id: 'anthropic', label: 'Anthropic', secret_name: 'ANTHROPIC_API_KEY', kind: 'llm', policy_fields: ['spend_cap_usd'] },
    { id: 'openai', label: 'OpenAI', secret_name: 'OPENAI_API_KEY', kind: 'llm', policy_fields: ['spend_cap_usd'] },
  ],
};

test('workspace provider key add and delete use the canonical provider route', async ({ page }) => {
  let savedPayload: { key?: string; validation_token?: string } | null = null;
  let deleted = false;
  await page.route('**/api/providers/openai/validate', async (route) => {
    const body = route.request().postDataJSON() as { key?: string };
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(
        body.key === 'sk-local-valid'
          ? { provider: 'openai', ok: true, reachable: true, status: 200, validation_token: 'local-token' }
          : { provider: 'openai', ok: false, reachable: true, status: 401, detail: 'HTTP 401: key rejected' },
      ),
    });
  });
  await page.route('**/api/providers/keys/openai', async (route) => {
    if (route.request().method() === 'PUT') {
      savedPayload = route.request().postDataJSON() as { key?: string; validation_token?: string };
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          schemaVersion: 'frisket.providers.v1',
          tier: 'local',
          providers: [
            {
              id: 'openai',
              label: 'OpenAI',
              kind: 'platform_api',
              configured: true,
              hint: '...alid',
              source: 'local_file',
              models: [{ id: 'openai/gpt-5-mini', label: 'gpt-5-mini', price: null }],
            },
          ],
        }),
      });
      return;
    }
    if (route.request().method() === 'DELETE') {
      deleted = true;
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          schemaVersion: 'frisket.providers.v1',
          tier: 'local',
          providers: [
            {
              id: 'openai',
              label: 'OpenAI',
              kind: 'platform_api',
              configured: false,
              source: null,
              models: [{ id: 'openai/gpt-5-mini', label: 'gpt-5-mini', price: null }],
            },
          ],
        }),
      });
      return;
    }
    await route.continue();
  });

  await page.goto('/settings/personal/ai-providers');
  const section = page.getByTestId('workspace-ai-providers-settings');
  await expect(section).toBeVisible();
  await section.getByRole('button', { name: 'Add new key' }).click();
  const keyForm = section.locator('form.settings-provider-key-form');
  await keyForm.getByLabel('Provider', { exact: true }).selectOption('openai');
  await keyForm.getByLabel('Provider key', { exact: true }).fill('sk-local-invalid');
  await expect(keyForm.getByRole('button', { name: 'Save', exact: true })).toBeDisabled();
  await keyForm.getByRole('button', { name: 'Test', exact: true }).click();
  await expect(keyForm.getByTestId('provider-validation-message')).toContainText(/rejected|401/i);
  expect(savedPayload).toBeNull();

  await keyForm.getByLabel('Provider key', { exact: true }).fill('sk-local-valid');
  await expect(keyForm.getByTestId('provider-validation-message')).toHaveCount(0);
  await keyForm.getByRole('button', { name: 'Test', exact: true }).click();
  await expect(keyForm.getByTestId('provider-validation-message')).toContainText(/accepted|valid/i);
  await expect(keyForm.getByRole('button', { name: 'Save', exact: true })).toBeEnabled();
  await keyForm.getByRole('button', { name: 'Save', exact: true }).click();
  expect(savedPayload).toEqual({ key: 'sk-local-valid', validation_token: 'local-token' });
  await expect(page.getByText('sk-local-valid')).toHaveCount(0);
  await section.getByTestId('provider-key-delete-openai').click();
  await expect.poll(() => deleted).toBe(true);
});

test('organization AI Providers require test before save and clear validation on edits', async ({ page }) => {
  await mockHostedSettingsShell(page);
  let savedPayload: Record<string, unknown> | null = null;
  let keys: Array<{ provider: string; hint: string; spend_cap_usd: number | null; spent_usd: number; over_cap: boolean }> = [];
  await page.route('**/api/org/provider-catalog', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(providerCatalog),
  }));
  await page.route('**/api/org/keys/validate', async (route) => {
    const body = route.request().postDataJSON() as { key?: string; provider: string };
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(
        body.key === 'sk-org-valid'
          ? { provider: body.provider, ok: true, reachable: true, status: 200, validation_token: 'org-token' }
          : { provider: body.provider, ok: false, reachable: true, status: 401, detail: 'HTTP 401: key rejected' },
      ),
    });
  });
  await page.route('**/api/org/keys', async (route) => {
    if (route.request().method() === 'GET') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(keys) });
      return;
    }
    savedPayload = route.request().postDataJSON() as Record<string, unknown>;
    keys = [{ provider: 'openai', hint: '...alid', spend_cap_usd: 8.5, spent_usd: 0, over_cap: false }];
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, provider: 'openai' }) });
  });

  await page.goto('/settings/organization/ai-providers');
  const section = page.getByTestId('organization-ai-providers-settings');
  await section.getByRole('button', { name: 'Add new key' }).click();
  await section.getByLabel('Provider', { exact: true }).selectOption('openai');
  await section.getByLabel('Provider key', { exact: true }).fill('sk-org-valid');
  await section.getByLabel('Spend cap').fill('8.5');
  await expect(section.getByRole('button', { name: 'Save', exact: true })).toBeDisabled();
  await section.getByRole('button', { name: 'Test', exact: true }).click();
  await expect(section.getByTestId('provider-validation-message')).toContainText(/accepted|valid/i);
  await expect(section.getByRole('button', { name: 'Save', exact: true })).toBeEnabled();
  await section.getByLabel('Provider key', { exact: true }).fill('sk-org-valid2');
  await expect(section.getByTestId('provider-validation-message')).toHaveCount(0);
  await expect(section.getByRole('button', { name: 'Save', exact: true })).toBeDisabled();
  await section.getByLabel('Provider key', { exact: true }).fill('sk-org-valid');
  await section.getByRole('button', { name: 'Test', exact: true }).click();
  await section.getByRole('button', { name: 'Save', exact: true }).click();
  expect(savedPayload).toEqual({
    provider: 'openai',
    key: 'sk-org-valid',
    spend_cap_usd: 8.5,
    validation_token: 'org-token',
  });
  await expect(page.getByText('sk-org-valid')).toHaveCount(0);
  await expect(section).toContainText('...alid');
});

test('project AI Providers require test before save and can test existing overrides', async ({ page }) => {
  await mockHostedSettingsShell(page);
  let savedPayload: Record<string, unknown> | null = null;
  let providers = [
    {
      id: 'openai',
      label: 'OpenAI',
      secret_name: 'OPENAI_API_KEY',
      kind: 'llm',
      policy_fields: ['spend_cap_usd'],
      configured: true,
      hint: '...old1',
      spend_cap_usd: 10,
      spent_usd: 1.5,
      updated_at: '2026-07-01T12:00:00Z',
    },
    {
      id: 'anthropic',
      label: 'Anthropic',
      secret_name: 'ANTHROPIC_API_KEY',
      kind: 'llm',
      policy_fields: ['spend_cap_usd'],
      configured: false,
      hint: null,
      spend_cap_usd: null,
      spent_usd: 0,
      updated_at: null,
    },
  ];
  const payload = () => ({
    schemaVersion: 'frisket.project_provider_keys.v1',
    projectId: 'alpha',
    providers,
  });
  await page.route('**/api/projects/alpha/provider-keys/validate', async (route) => {
    const body = route.request().postDataJSON() as { key?: string; provider: string };
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        provider: body.provider,
        ok: true,
        reachable: true,
        status: 200,
        validation_token: body.key ? 'project-token' : undefined,
      }),
    });
  });
  await page.route('**/api/projects/alpha/provider-keys', async (route) => {
    if (route.request().method() === 'GET') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(payload()) });
      return;
    }
    savedPayload = route.request().postDataJSON() as Record<string, unknown>;
    providers = providers.map((item) => item.id === 'anthropic'
      ? { ...item, configured: true, hint: '...alid', spend_cap_usd: 2.25, updated_at: '2026-07-04T12:00:00Z' }
      : item);
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(payload()) });
  });

  await page.goto('/p/alpha/settings/project/ai-providers');
  const section = page.getByTestId('project-ai-providers-settings');
  await section.getByRole('button', { name: 'Test OpenAI provider override' }).click();
  await expect(section.getByTestId('provider-existing-validation-openai')).toContainText(/accepted|valid/i);
  await section.getByRole('button', { name: 'Add new key' }).click();
  await section.getByLabel('Provider', { exact: true }).selectOption('anthropic');
  await section.getByLabel('Project provider key').fill('sk-project-valid');
  await section.getByLabel('Spend cap').fill('2.25');
  await expect(section.getByRole('button', { name: 'Save', exact: true })).toBeDisabled();
  await section.getByRole('button', { name: 'Test', exact: true }).click();
  await expect(section.getByTestId('provider-validation-message')).toContainText(/accepted|valid/i);
  await section.getByRole('button', { name: 'Save', exact: true }).click();
  expect(savedPayload).toEqual({
    provider: 'anthropic',
    key: 'sk-project-valid',
    spend_cap_usd: 2.25,
    validation_token: 'project-token',
  });
  await expect(page.getByText('sk-project-valid')).toHaveCount(0);
  await expect(section).toContainText('...alid');
});

test('replay banner acknowledges configured providers; AI call mode is explained in settings', async ({ page }) => {
  // Strict replay is the one posture where live calls are impossible
  // (live_calls_possible=false); plain replay falls through to a live call on
  // a miss and shows no banner.
  await page.route('**/api/config', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      cache_mode: 'replay_strict',
      live_calls_possible: false,
      cache_mode_editable: false,
      email_from_address: null,
      email_from_name: null,
    }),
  }));
  await page.route('**/api/providers', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      schemaVersion: 'frisket.providers.v1',
      tier: 'local',
      providers: [
        { id: 'openai', label: 'OpenAI', kind: 'platform_api', configured: true, hint: '...1234', source: 'local_file', models: [] },
        { id: 'anthropic', label: 'Anthropic', kind: 'platform_api', configured: false, hint: null, source: null, models: [] },
      ],
    }),
  }));

  await page.goto('/');
  const banner = page.getByTestId('replay-mode-banner');
  await expect(banner).toBeVisible();
  await expect(banner).toContainText(/OpenAI.*configured/i);
  await expect(banner).toContainText(/replay mode/i);
  // The mode control moved out of the banner into Settings → Preferences; with
  // providers configured the banner links to the mode explainer, not providers.
  await expect(banner.getByLabel('AI call mode')).toHaveCount(0);
  await expect(banner.getByTestId('replay-mode-banner-mode-link')).toBeVisible();

  // Preferences explains each mode and marks the server's active mode.
  await page.goto('/settings/personal/preferences');
  const modeSection = page.getByTestId('ai-call-mode-settings');
  await expect(modeSection).toBeVisible();
  await expect(modeSection).toContainText(/FRISKET_CACHE_MODE|restart/i);
  await expect(page.getByTestId('ai-call-mode-replay_strict')).toContainText(/current/i);
});
