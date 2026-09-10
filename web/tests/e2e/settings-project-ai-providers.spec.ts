import { expect, test } from '@playwright/test';
import { mockHostedSettingsShell } from './settingsTestHelpers';

test('Project AI Providers stores project overrides without exposing raw keys', async ({ page }) => {
  await mockHostedSettingsShell(page);

  let providers = [
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
  ];
  const payload = () => ({
    schemaVersion: 'frisket.project_provider_keys.v1',
    projectId: 'alpha',
    providers,
  });

  await page.route('**/api/projects/alpha/provider-keys', async (route) => {
    if (route.request().method() === 'GET') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(payload()) });
      return;
    }
    const body = route.request().postDataJSON() as {
      provider: string;
      key: string;
      spend_cap_usd: number | null;
      validation_token: string;
    };
    expect(body.validation_token).toBe('project-token');
    providers = providers.map((item) => item.id === body.provider
      ? {
        ...item,
        configured: true,
        hint: '...cret',
        spend_cap_usd: body.spend_cap_usd,
        updated_at: '2026-07-01T12:30:00Z',
      }
      : item);
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(payload()) });
  });
  await page.route('**/api/projects/alpha/provider-keys/*', async (route) => {
    const provider = decodeURIComponent(route.request().url().split('/').pop() ?? '');
    providers = providers.map((item) => item.id === provider
      ? { ...item, configured: false, hint: null, spend_cap_usd: null, updated_at: null }
      : item);
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true, deleted: true, provider }) });
  });
  await page.route('**/api/projects/alpha/provider-keys/validate', async (route) => {
    const body = route.request().postDataJSON() as { provider: string; key?: string };
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

  await page.goto('/p/alpha/settings/project/ai-providers');

  const section = page.getByTestId('project-ai-providers-settings');
  await expect(section).toBeVisible();
  await expect(page.getByTestId('workbench-shell')).toHaveCount(0);
  await expect(section).toContainText('OPENAI_API_KEY');
  await section.getByRole('button', { name: 'Test OpenAI provider override' }).click();
  await expect(section.getByTestId('provider-existing-validation-openai')).toContainText(/accepted|valid/i);
  await section.getByRole('button', { name: 'Add new key' }).click();
  await expect(page.getByLabel('Project provider key')).toHaveAttribute('type', 'password');
  await page.getByLabel('Project provider key').fill('sk-project-secret');
  await page.getByLabel('Spend cap').fill('3.25');
  await expect(section.getByRole('button', { name: 'Save', exact: true })).toBeDisabled();
  await section.getByRole('button', { name: 'Test', exact: true }).click();
  await expect(section.getByTestId('provider-validation-message')).toContainText(/accepted|valid/i);
  await section.getByRole('button', { name: 'Save', exact: true }).click();
  await expect(page.getByText('sk-project-secret')).toHaveCount(0);
  await expect(section).toContainText('...cret');
  await expect(section).toContainText('$3.25');
});
