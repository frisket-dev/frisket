import type { Page } from '@playwright/test';

export type SettingsProjectFixture = {
  id: string;
  name: string;
  description?: string;
  sensitive?: boolean;
  role?: string;
};

export async function mockHostedSettingsShell(
  page: Page,
  projects: SettingsProjectFixture[] = [
    {
      id: 'alpha',
      name: 'Alpha Project',
      description: 'Field reporting workspace',
      sensitive: false,
      role: 'owner',
    },
  ],
) {
  await page.route('**/api/me', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      email: 'owner@example.org',
      display_name: 'Owner Person',
      avatar_seed: 'user:owner@example.org',
    }),
  }));
  await page.route('**/api/column-types', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: '[]',
  }));
  await page.route('**/api/projects', (route) => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(projects.map((project) => ({
      id: project.id,
      name: project.name,
      description: project.description ?? '',
      sensitive: project.sensitive ?? false,
      updated_at: null,
      pending_review_count: 0,
      starred: false,
      archived: false,
      role: project.role ?? 'owner',
    }))),
  }));
}
