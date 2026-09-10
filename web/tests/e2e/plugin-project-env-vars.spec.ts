import { expect, test, type APIRequestContext } from '@playwright/test';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  createProject,
  importCsv,
  uniqueName,
} from './helpers';

const PLUGIN_ID = 'demo.env_settings';
const SECRET_NAME = 'DEMO_API_KEY';
const PLUGIN_CAPABILITY = 'plugin:trusted_local_backend';
const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../../..');
const PLUGIN_ROOT = resolve(ROOT, 'tests/fixtures/local_plugins/demo_env_settings');

async function installAndActivateEnvPlugin(
  request: APIRequestContext,
  projectId: string,
): Promise<void> {
  const installed = await request.post(
    `/api/projects/${projectId}/workbench/plugins/${PLUGIN_ID}/install-local`,
    {
      data: {
        source: {
          kind: 'localPath',
          value: PLUGIN_ROOT,
        },
        arbitraryPackageLoadAllowed: false,
      },
    },
  );
  expect(installed.ok()).toBeTruthy();
  const installBody = await installed.json() as {
    installState: string;
    receiptId?: string | null;
  };
  expect(installBody.installState).toBe('installed');
  expect(installBody.receiptId).toBeTruthy();

  const activated = await request.post(
    `/api/projects/${projectId}/workbench/plugins/${PLUGIN_ID}/activate`,
    {
      data: {
        receiptId: installBody.receiptId,
        trustAcknowledged: true,
        permissionsAccepted: [PLUGIN_CAPABILITY],
        arbitraryPackageLoadAllowed: false,
      },
    },
  );
  expect(activated.ok()).toBeTruthy();
}

test('project plugin secrets save through Project Secrets without org env or plaintext echo', async ({
  page,
}) => {
  const projectId = await createProject(page.request, uniqueName('plugin-env'));
  await importCsv(page.request, projectId, 'cities.csv', 'city\nPittsburgh\n');
  await installAndActivateEnvPlugin(page.request, projectId);

  let orgEnvRouteUsed = false;
  await page.route('**/api/org/env**', async (route) => {
    orgEnvRouteUsed = true;
    await route.abort();
  });

  await page.goto(`/p/${projectId}/settings/project/secrets`);
  const section = page.getByTestId('project-secrets-settings');
  await expect(section).toBeVisible();
  await expect(section).toContainText(SECRET_NAME);
  await expect(section).toContainText('Missing');

  const value = 'project-env-token-value';
  await page.getByLabel('Project secret name').fill(SECRET_NAME);
  await page.getByLabel('Project secret value').fill(value);
  await page.getByRole('button', { name: 'Save' }).click();

  await expect(page.getByLabel('Project secret value')).toHaveValue('');
  await expect(section).toContainText('...alue');
  await expect(section).toContainText('Configured');
  await expect(page.locator('body')).not.toContainText(value);

  const listed = await page.request.get(`/api/projects/${projectId}/secrets`);
  expect(listed.ok()).toBeTruthy();
  const listedText = await listed.text();
  expect(listedText).not.toContain(value);
  const listedBody = JSON.parse(listedText) as {
    secrets: Array<{ name: string; configured: boolean; hint: string | null }>;
  };
  expect(listedBody.secrets).toHaveLength(1);
  expect(listedBody.secrets[0]).toMatchObject({
    name: SECRET_NAME,
    configured: true,
    hint: '...alue',
  });
  expect(orgEnvRouteUsed).toBe(false);

  await page.getByLabel(`Delete ${SECRET_NAME} secret`).click();
  await expect(section).toContainText('Missing');
  await expect(page.getByLabel(`Delete ${SECRET_NAME} secret`)).toHaveCount(0);
});
