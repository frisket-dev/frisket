import { expect, test } from '@playwright/test';
import { createProject, importCsv, uniqueName } from './helpers';
import { selectorChoicesResponse, stubSelectorChoices } from './selectorChoicesFixture';

// Unlike ordinary workbench tests, exercise a genuinely fresh browser profile.
test.use({ storageState: { cookies: [], origins: [] } });

test('optional welcome and Ask setup stay in context', async ({ page }) => {
  const pid = await createProject(page.request, uniqueName('cloud-welcome'));
  const sheet = await importCsv(page.request, pid, 'notes.csv', 'notes\nCouncil meeting\n');
  await page.addInitScript(() => localStorage.setItem('frisket:product-telemetry:v1',
    JSON.stringify({ schema_version: 1, state: 'disabled' })));
  await page.route('**/api/cloud-connection', route => route.fulfill({ json: {
    state: 'disconnected', identity: null, accounts: [], expires_at: null,
    capabilities: { can_connect: true, can_reconnect: false, can_disconnect: false },
  } }));
  await page.route('**/api/providers?*', route => route.fulfill({ json: {
    schema_version: 'frisket.local_provider_catalog.v1', tier: 'local', providers: [],
  } }));
  await stubSelectorChoices(page, pid, subject => ({
    ...selectorChoicesResponse({ projectId: pid, subject, currentChoiceId: 'gemini',
      groups: [{ id: 'gemini', label: 'Google', choices: [{ choiceId: 'gemini', label: 'Gemini Flash',
        authoredSelection: { kind: 'model', model: 'gemini/gemini-3.5-flash-lite' },
        status: 'needs_setup', canRun: false }] }] }),
    cloud_setup: { kind: 'cloud', state: 'connect' },
  }));
  await page.goto(`/p/${pid}/s/${sheet}`);
  const welcome = page.getByRole('dialog', { name: 'Try AI with Frisket Cloud' });
  await expect(welcome).toBeVisible();
  await expect(welcome.getByRole('button', { name: 'Sign in with browser' })).toBeEnabled();
  await page.screenshot({ path: test.info().outputPath('cloud-first-launch.png') });
  await welcome.getByRole('button', { name: 'Continue without signing in' }).click();
  await expect(welcome).toHaveCount(0);
  await page.reload();
  await expect(page.getByTestId('grid')).toBeVisible();
  await expect(welcome).toHaveCount(0);
  await page.getByTestId('chrome-ask-toggle').click();
  const dock = page.getByTestId('ask-dock');
  await expect(dock.getByText('Sign in to Frisket Cloud or set up a model to start chatting.')).toBeVisible();
  const question = dock.getByRole('textbox', { name: 'Question', exact: true });
  await question.fill('Keep my question while I set up a model');
  await page.setViewportSize({ width: 1280, height: 720 });
  await expect(dock.getByRole('button', { name: 'Sign in to Frisket Cloud', exact: true })).toBeInViewport();
  const send = dock.getByRole('button', { name: 'Send', exact: true });
  await expect(send).toBeInViewport({ ratio: 1 });
  // A viewport intersection alone misses controls covered by the Jobs pane.
  expect(await send.evaluate((button) => {
    const rect = button.getBoundingClientRect();
    return button.contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2));
  })).toBe(true);
  await page.screenshot({ path: test.info().outputPath('ask-model-setup.png') });
  await dock.getByRole('button', { name: 'Sign in to Frisket Cloud', exact: true }).click();
  const browserSignIn = dock.getByRole('button', { name: 'Sign in with browser' });
  await browserSignIn.scrollIntoViewIfNeeded();
  await expect(browserSignIn).toBeInViewport();
  expect(await send.evaluate((button) => {
    const rect = button.getBoundingClientRect();
    return button.contains(document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2));
  })).toBe(true);
  await dock.getByRole('button', { name: 'Set up a model' }).click();
  await expect(page.getByTestId('engine-selector-dialog')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(question).toHaveValue('Keep my question while I set up a model');
});
