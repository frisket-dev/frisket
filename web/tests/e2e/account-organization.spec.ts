import { expect, test } from '@playwright/test';

test('organization settings manage provider keys and secrets without leaking values', async ({ page }) => {
  let keys = [
    {
      provider: 'anthropic',
      hint: '...old1',
      spend_cap_usd: 5,
      spent_usd: 1.25,
      over_cap: false,
    },
  ];
  let envVars = [{ name: 'EXA_API_KEY', hint: '...demo', created_at: '2026-06-13T00:00:00Z' }];

  await page.route('**/api/me', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ email: 'owner@example.com', display_name: 'Owner' }),
  }));
  await page.route('**/api/column-types', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: '[]',
  }));
  await page.route('**/api/org/provider-catalog', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      schemaVersion: 'frisket.provider_catalog.v1',
      providers: [
        { id: 'anthropic', label: 'Anthropic', secret_name: 'ANTHROPIC_API_KEY', kind: 'llm', policy_fields: ['spend_cap_usd'] },
        { id: 'openai', label: 'OpenAI', secret_name: 'OPENAI_API_KEY', kind: 'llm', policy_fields: ['spend_cap_usd'] },
      ],
    }),
  }));
  await page.route('**/api/org/keys', async (route) => {
    if (route.request().method() === 'GET') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(keys) });
      return;
    }
    const body = route.request().postDataJSON() as { provider: string; key: string };
    keys = [
      ...keys.filter((key) => key.provider !== body.provider),
      { provider: body.provider, hint: '...cret', spend_cap_usd: null, spent_usd: 0, over_cap: false },
    ];
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, provider: body.provider }) });
  });
  await page.route('**/api/org/keys/*', async (route) => {
    const provider = route.request().url().split('/').pop() ?? '';
    keys = keys.filter((key) => key.provider !== provider);
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, deleted: true }) });
  });
  await page.route('**/api/org/env', async (route) => {
    if (route.request().method() === 'GET') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(envVars) });
      return;
    }
    const body = route.request().postDataJSON() as { name: string; value: string };
    envVars = [
      ...envVars.filter((item) => item.name !== body.name.toUpperCase()),
      { name: body.name.toUpperCase(), hint: '...cret', created_at: '2026-06-13T00:00:00Z' },
    ];
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, name: body.name.toUpperCase() }) });
  });
  await page.route('**/api/org/env/*', async (route) => {
    const name = decodeURIComponent(route.request().url().split('/').pop() ?? '');
    envVars = envVars.filter((item) => item.name !== name);
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, deleted: true }) });
  });

  await page.goto('/settings/organization/ai-providers');

  const orgProviders = page.getByTestId('organization-ai-providers-settings');
  await expect(orgProviders).toContainText('anthropic');
  // The key form sits behind the explicit 'Add new key' affordance now, and
  // Save is validation-gated: a successful Test (POST /api/org/keys/validate,
  // validation_token) must precede Save (settings revamp:
  // OrganizationAiProvidersSettings form.adding + providerFormCanSave).
  await page.route('**/api/org/keys/validate', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ ok: true, provider: 'anthropic', validation_token: 'tok-e2e', message: 'Key OK' }),
  }));
  await orgProviders.getByRole('button', { name: 'Add new key' }).click();
  await orgProviders.getByLabel('Provider key', { exact: true }).fill('sk-new-secret');
  await orgProviders.getByRole('button', { name: 'Test', exact: true }).click();
  await orgProviders.getByRole('button', { name: 'Save' }).click();
  await expect(page.getByText('sk-new-secret')).toHaveCount(0);
  await expect(orgProviders).toContainText('...cret');

  await page.goto('/settings/organization/env-vars');
  await page.getByLabel('Secret name').fill('serp_api_key');
  await page.getByLabel('Secret value').fill('env-secret');
  await page.getByRole('button', { name: 'Save' }).click();
  await expect(page.getByText('env-secret')).toHaveCount(0);
  await expect(page.getByTestId('organization-secrets-settings')).toContainText('SERP_API_KEY');
});
