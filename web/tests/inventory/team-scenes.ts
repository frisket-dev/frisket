import { expect, type BrowserContext, type Page } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { teamServerCommands } from '../../playwright/inventory-team';
import { TEAM_EDITION_DESCRIPTOR } from '../../src/editions/posture';
import { settingsSectionsFor } from '../../src/settings/openSettingsRegistry';
import { TEAM_MODELS_GATEWAY_SECTION } from '../../src/settings/teamModelsGatewaySection';

type Shot = (page: Page, family: string, name: string, source: string) => Promise<void>;

/** Team-only settings use the actual Team shell and a disposable owner session. */
export async function captureTeamSettingsScenes(page: Page, shot: Shot) {
  const baseURL = process.env.FRISKET_INVENTORY_TEAM_URL;
  const backendURL = process.env.FRISKET_INVENTORY_TEAM_BACKEND_URL;
  const workspace = process.env.FRISKET_INVENTORY_WORKSPACE;
  if (!baseURL || !backendURL || !workspace) throw new Error('Inventory Team server coordinates are required.');
  const { seed } = teamServerCommands({
    workspace, baseURL, backendURL,
    python: process.env.FRISKET_INVENTORY_PYTHON ?? path.resolve('../.venv/bin/python'),
  });
  execFileSync(seed.command, seed.args, { env: seed.env, timeout: 60_000, stdio: 'pipe' });
  const state = JSON.parse(readFileSync(seed.storageState, 'utf8')) as Awaited<ReturnType<BrowserContext['storageState']>>;
  await page.context().addCookies(state.cookies);

  const response = await page.request.post(`${baseURL}/api/projects`, { data: { name: 'Team settings inventory' } });
  expect(response.ok(), await response.text()).toBeTruthy();
  const project = await response.json() as { id: string };
  const sections = settingsSectionsFor(TEAM_EDITION_DESCRIPTOR, [TEAM_MODELS_GATEWAY_SECTION])
    .filter(section => section.scope === 'organization' || section.id === 'project.access');
  await page.goto(`${baseURL}${sections[0].routePattern}`);
  const onboarding = page.getByTestId('cost-preapproval-setup');
  await expect(onboarding).toBeVisible();
  await shot(page, 'Team settings', 'cost-preapproval-setup', 'src/components/CostPreapprovalSetupModal.tsx; first-login setup');
  await page.getByTestId('cost-preapproval-setup-save').click();
  await expect(onboarding).toHaveCount(0);
  const content = page.getByTestId('settings-content');
  for (const section of sections) {
    await page.goto(`${baseURL}${section.routePattern.replace('{projectId}', project.id)}`);
    await expect(content.getByRole('heading', { name: section.title, exact: true }).first()).toBeVisible();
    await expect(content.locator('.settings-stack, [data-testid="models-gateway-settings"]').first()).toBeVisible();
    await expect(content.locator('.settings-inline-status').filter({ hasText: /^Loading/ })).toHaveCount(0);
    await expect(content.getByText('Loading models gateway configuration…', { exact: true })).toHaveCount(0);
    if (section.section === 'skills') await expect(page.getByTestId('skills-editor')).toBeEnabled();
    await expect(onboarding).toHaveCount(0);
    await shot(page, 'Team settings', `${section.scope}-${section.section}`, 'src/settings/SettingsWorkspace.tsx; SettingsSections.tsx; real Team edition');
    const { height, total } = await content.evaluate(element => ({ height: element.clientHeight, total: element.scrollHeight }));
    for (let offset = height * 0.8, part = 2; offset < total - 40; offset += height * 0.8, part += 1) {
      await content.evaluate((element, top) => { element.scrollTop = top; }, offset);
      await shot(page, 'Team settings', `${section.scope}-${section.section}-part-${part}`, 'src/settings/SettingsWorkspace.tsx; SettingsSections.tsx; real Team edition');
      if (offset + height >= total) break;
    }
  }
}
