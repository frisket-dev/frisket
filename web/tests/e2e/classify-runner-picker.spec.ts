import { expect, test, type Page } from '@playwright/test';
import { createProject, importCsv, openAction, uniqueName } from './helpers';

async function exposeClassifyRunners(page: Page): Promise<void> {
  await page.route('**/actions/v1/catalog', async (route) => {
    const response = await route.fetch();
    const catalog = await response.json();
    const classify = (catalog.actions ?? []).find(
      (action: { kind?: string }) => action.kind === 'map.classify',
    );
    for (const engine of classify?.ui_hints?.engines ?? []) {
      if (engine.id === 'local_semantic' || engine.id === 'llm') {
        engine.available = true;
        delete engine.error;
      }
    }
    await route.fulfill({
      status: response.status(),
      contentType: 'application/json',
      body: JSON.stringify(catalog),
    });
  });
  await page.route('**/api/providers', (route) => route.fulfill({
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
          source: 'env',
          hint: null,
          models: [{
            id: 'openai/gpt-5-mini',
            label: 'GPT-5 mini — fast/cheap',
            price: null,
          }],
        },
        {
          endpoint_id: 'desktop',
          label: 'Ollama — desktop',
          kind: 'local_http',
          read_only: false,
          models: [{
            id: 'ollama/@desktop/qwen3:0.6b',
            label: 'qwen3:0.6b',
            price: null,
            local: true,
          }],
          reachable: true,
          origin: 'http://127.0.0.1:11434',
          authority: 'instance',
          source: 'stored',
          detail: null,
          installed_models: ['qwen3:0.6b'],
          protocol: 'ollama_native',
          auth_status: 'ok',
          token_configured: false,
          provisioning_token_configured: false,
          edge_auth: false,
          pull_enabled: true,
        },
      ],
    }),
  }));
}

test('classify presents local engines, local-server models, and hosted models in one picker', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('e2e-classify-runner'));
  await importCsv(page.request, pid, 'stories.csv', 'headline\n"Transit service expanded"\n');
  await exposeClassifyRunners(page);
  await page.goto(`/p/${pid}`);
  await openAction(page, 'map.classify');

  const runner = page.getByTestId('field-engine-model-choice');
  await expect(runner.getByText('Classifier', { exact: true })).toBeVisible();
  await expect(runner.getByTestId('model-picker-button')).toContainText('Local semantic');
  await expect(page.getByTestId('engine-picker-button')).toHaveCount(0);

  const [labelBox, buttonBox, formContentWidth] = await Promise.all([
    runner.getByText('Classifier', { exact: true }).boundingBox(),
    runner.getByTestId('model-picker-button').boundingBox(),
    page.getByTestId('generated-action-form').evaluate((form) => {
      const style = getComputedStyle(form);
      return form.clientWidth - Number.parseFloat(style.paddingLeft) - Number.parseFloat(style.paddingRight);
    }),
  ]);
  expect(labelBox).not.toBeNull();
  expect(buttonBox).not.toBeNull();
  expect(labelBox!.y + labelBox!.height).toBeLessThanOrEqual(buttonBox!.y);
  expect(Math.abs(buttonBox!.width - formContentWidth)).toBeLessThanOrEqual(2);

  await runner.getByTestId('model-picker-button').click();
  await expect(page.getByTestId('model-provider-group-choice-fixed-engines')).toContainText('Classification engines');
  await expect(page.getByTestId('model-provider-group-desktop')).toContainText('Ollama');
  await expect(page.getByTestId('model-provider-group-openai')).toContainText('OpenAI');

  await page.getByTestId('model-provider-group-desktop').click();
  await page.getByTestId('model-option-ollama-desktop-qwen3-0-6b').click();
  await expect(runner.getByTestId('model-picker-button')).toContainText('qwen3:0.6b');

  await runner.getByTestId('model-picker-button').click();
  await page.getByTestId('model-provider-group-openai').click();
  await page.getByTestId('model-option-openai-gpt-5-mini').click();
  await expect(runner.getByTestId('model-picker-button')).toContainText('GPT-5 mini');
  await expect(page.getByTestId('engine-select')).toHaveCount(0);
  await expect(page.getByTestId('model-select')).toHaveCount(0);
});
